package io.vpcore.app;

import android.content.Context;
import android.os.Build;
import android.util.Log;

import androidx.javascriptengine.EvaluationFailedException;
import androidx.javascriptengine.IsolateStartupParameters;
import androidx.javascriptengine.JavaScriptIsolate;
import androidx.javascriptengine.JavaScriptSandbox;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.FileNotFoundException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.TimeZone;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;

/**
 * Разбор команды телефону для голоса: тот же код, что у чата (web/src/phone,
 * бандл assets/phone-voice.js — npm run phone-voice), в V8 из WebView
 * (JavaScriptSandbox), без окна — служба работает и с выключенным экраном.
 *
 * vpcPhone.plan(persona, text, names, nowMs, apps) → {report, ops}: report —
 * итог для персоны (null — не команда), ops — что вызвать в PhoneActionsCore.
 * Переспросы («на какое время?») живут в изоляте — он один на всю жизнь
 * службы (пересоздаётся после сбоя).
 *
 * Только из одного рабочего потока службы. Любой сбой (старый WebView, нет
 * бандла, таймаут) — null: служба отправит реплику как обычно.
 */
final class PhoneVoiceJs {

    private static final String TAG = "WakeWord";
    private static final String ASSET = "phone-voice.js";
    // Первый запуск V8 в отдельном процессе — дольше, чем разбор фразы
    private static final long CONNECT_TIMEOUT_MS = 10_000;
    private static final long EVAL_TIMEOUT_MS = 5_000;
    // Без возврата промиса: итог читается из vpcPhone.last; каждая оценка
    // прогоняет микрозадачи, так что хватает пары попыток
    private static final int LAST_POLLS = 5;

    // Для status плагина WakeWord: off | ready | unsupported | no_bundle | error
    static volatile String engineState = "off";

    private final Context ctx;
    private volatile JavaScriptSandbox sandbox;
    private volatile JavaScriptIsolate isolate;
    private boolean promiseReturn;
    // Бандла нет в сборке — не пытаемся снова до перезапуска службы
    private boolean noBundle = false;

    PhoneVoiceJs(Context ctx) {
        this.ctx = ctx.getApplicationContext();
    }

    /** WebView этого телефона умеет JavaScriptSandbox (Android 8+, свежий WebView). */
    static boolean isSupported() {
        if (Build.VERSION.SDK_INT < 26) return false;
        try {
            return JavaScriptSandbox.isSupported();
        } catch (Throwable e) {
            return false;
        }
    }

    /** Поднять движок заранее: первая команда не ждёт запуска V8. */
    void warmUp() {
        try {
            ensure();
        } catch (Throwable e) {
            Log.w(TAG, "движок команд телефону не поднялся: " + e);
            engineState = "error";
            close();
        }
    }

    /**
     * Итог разбора {report, ops} или null (не команда или сбой). names —
     * обращения к персоне, apps — приложения телефона для «открой X».
     */
    JSONObject plan(String persona, String text, List<String> names, List<PhoneActionsCore.App> apps) {
        // Две попытки: процесс V8 мог умереть, пока служба ждала имя (система
        // убивает его при нехватке памяти) — тогда заново, с новым изолятом
        for (int attempt = 0; attempt < 2; attempt++) {
            boolean fresh = isolate == null;
            try {
                JavaScriptIsolate iso = ensure();
                if (iso == null) return null;
                return run(iso, persona, text, names, apps);
            } catch (ExecutionException e) {
                if (e.getCause() instanceof EvaluationFailedException) {
                    // Ошибка в самом разборе — изолят жив, переспросы в нём целы
                    Log.w(TAG, "разбор команды телефону: " + e.getCause().getMessage());
                    return null;
                }
                Log.w(TAG, "движок команд телефону: " + e.getCause());
                engineState = "error";
                close();
                if (fresh) return null;
            } catch (Throwable e) {
                // Таймаут или сбой связи с процессом V8 — в следующий раз
                // заново (переспрос, если висел, теряется)
                Log.w(TAG, "разбор команды телефону не удался: " + e);
                engineState = "error";
                close();
                if (fresh) return null;
            }
        }
        return null;
    }

    private JSONObject run(JavaScriptIsolate iso, String persona, String text, List<String> names,
                           List<PhoneActionsCore.App> apps) throws Exception {
        JSONArray appsJson = new JSONArray();
        for (PhoneActionsCore.App a : apps) {
            appsJson.put(new JSONObject().put("label", a.label).put("package", a.pkg));
        }
        long now = System.currentTimeMillis();
        // Часовой пояс у процесса V8 может быть другим (UTC): сдвигаем
        // «сейчас» так, чтобы его местные часы совпали с часами телефона —
        // иначе «разбуди через 20 минут» поставит не тот час
        long phoneOffset = TimeZone.getDefault().getOffset(now);
        String nowExpr = "(" + now + "+" + phoneOffset + "+new Date(" + now + ").getTimezoneOffset()*60000)";
        String call = "vpcPhone.plan(" + JSONObject.quote(persona) + "," + JSONObject.quote(text) + ","
                + new JSONArray(names) + "," + nowExpr + "," + appsJson + ")";
        String out;
        if (promiseReturn) {
            out = eval(iso, call);
        } else {
            eval(iso, call + ";''");
            out = "";
            for (int i = 0; i < LAST_POLLS && out.isEmpty(); i++) out = eval(iso, "vpcPhone.last");
            if (out.isEmpty()) {
                Log.w(TAG, "команда телефону: разбор не успел");
                return null;
            }
        }
        return new JSONObject(out);
    }

    private String eval(JavaScriptIsolate iso, String code) throws Exception {
        String r = iso.evaluateJavaScriptAsync(code).get(EVAL_TIMEOUT_MS, TimeUnit.MILLISECONDS);
        return r == null ? "" : r;
    }

    private JavaScriptIsolate ensure() throws Exception {
        if (isolate != null) return isolate;
        if (noBundle) return null;
        if (!isSupported()) {
            if (!"unsupported".equals(engineState)) {
                Log.w(TAG, "WebView без JavaScriptSandbox — команды телефону голосом уйдут обычным текстом");
            }
            engineState = "unsupported";
            return null;
        }
        String code;
        try {
            code = readAsset();
        } catch (FileNotFoundException e) {
            noBundle = true;
            engineState = "no_bundle";
            Log.w(TAG, "нет " + ASSET + " в сборке — команды телефону голосом выключены (npm run phone-voice)");
            return null;
        }
        long t0 = System.currentTimeMillis();
        JavaScriptSandbox sb = sandbox;
        if (sb == null) {
            sb = JavaScriptSandbox.createConnectedInstanceAsync(ctx).get(CONNECT_TIMEOUT_MS, TimeUnit.MILLISECONDS);
            sandbox = sb;
        }
        IsolateStartupParameters params = new IsolateStartupParameters();
        if (sb.isFeatureSupported(JavaScriptSandbox.JS_FEATURE_ISOLATE_MAX_HEAP_SIZE)) {
            // Разбору фразы хватает мегабайт; потолок — чтобы сбой не съел память
            params.setMaxHeapSizeBytes(32L * 1024 * 1024);
        }
        JavaScriptIsolate iso = sb.createIsolate(params);
        promiseReturn = sb.isFeatureSupported(JavaScriptSandbox.JS_FEATURE_PROMISE_RETURN);
        String type;
        try {
            type = iso.evaluateJavaScriptAsync(code + "\n;typeof vpcPhone")
                    .get(CONNECT_TIMEOUT_MS, TimeUnit.MILLISECONDS);
        } catch (Exception e) {
            iso.close();
            throw e;
        }
        if (!"object".equals(type)) {
            iso.close();
            throw new IllegalStateException("бандл не задал vpcPhone: " + type);
        }
        isolate = iso;
        engineState = "ready";
        Log.i(TAG, "движок команд телефону готов за " + (System.currentTimeMillis() - t0) + " мс"
                + (promiseReturn ? "" : " (без возврата промиса)"));
        return iso;
    }

    private String readAsset() throws Exception {
        try (InputStream in = ctx.getAssets().open(ASSET)) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString(StandardCharsets.UTF_8.name());
        }
    }

    /** Закрыть изолят и движок; можно из любого потока. */
    void close() {
        JavaScriptIsolate iso = isolate;
        isolate = null;
        JavaScriptSandbox sb = sandbox;
        sandbox = null;
        try {
            if (iso != null) iso.close();
        } catch (Throwable ignored) {
            // уже мёртв
        }
        try {
            if (sb != null) sb.close();
        } catch (Throwable ignored) {
            // уже отвязан
        }
    }
}
