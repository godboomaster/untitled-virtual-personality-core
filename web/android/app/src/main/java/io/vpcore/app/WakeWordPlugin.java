package io.vpcore.app;

import android.Manifest;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;

/**
 * Плагин Capacitor «WakeWord»: веб включает и выключает голосовую активацию
 * по имени (WakeWordService) и показывает её состояние.
 *
 * JS: start({baseUrl, token}) → {started, reason?: 'mic-denied'} (спрашивает
 * микрофон и, Android 13+, уведомления), stop(), configure({baseUrl, token})
 * — свежие адрес и токен (пустой адрес — служба останавливается, выбор
 * «включено» остаётся), status() → {enabled, running, state, names,
 * lastError, micGranted, overlayAllowed, phoneEngine}, openOverlaySettings() — экран
 * права «Поверх других приложений» (команды телефону голосом из фона).
 *
 * Ядро недоступно — служба просит ответа приложение: событие offlineAsk
 * {id, persona, message, action?} → offlineReply({id, text}) (пустой text —
 * ответить некому). takePending() → {items} — реплики, услышанные, пока окна
 * приложения не было.
 *
 * Приложение одной персоны (Арродес, dev.arrodes.app) передаёт в start и
 * configure ещё: persona — откликаться только на её имена; chatId, userId,
 * userName — в чей чат идут реплики (по умолчанию web_user, как у приложения
 * ядра); appAsks — пока окно на экране, вопрос задаёт само приложение
 * (событие voiceAsk {id, persona, message, action?} → offlineReply({id,
 * text}): text — что произнести, пустой — приложение не взялось, и служба
 * спросит ядро сама). Не переданы — как было.
 */
@CapacitorPlugin(
        name = "WakeWord",
        permissions = {
                @Permission(strings = { Manifest.permission.RECORD_AUDIO }, alias = "microphone"),
                @Permission(strings = { Manifest.permission.POST_NOTIFICATIONS }, alias = "notifications")
        }
)
public class WakeWordPlugin extends Plugin {

    /** askOffline: приложение не ответило за отведённое время. */
    static final String TIMEOUT = "\u0000timeout";
    // Вопросы службы приложению: ядро недоступно — ответ без него;
    // appAsks — вопрос, пока окно на экране, задаёт само приложение
    static final String EVENT_OFFLINE = "offlineAsk";
    static final String EVENT_VOICE = "voiceAsk";

    // Плагин окна приложения (окна нет — null) и ждущие ответа вопросы службы
    private static volatile WakeWordPlugin active;
    private static final Map<String, BlockingQueue<String>> asks = new ConcurrentHashMap<>();

    @Override
    public void load() {
        active = this;
    }

    @Override
    protected void handleOnDestroy() {
        if (active == this) active = null;
    }

    /** Из службы, не из главного потока: ответ приложения без ядра. null —
     * окна приложения нет (или оно не слушает), "" — ответить некому,
     * TIMEOUT — не дождались, иначе текст ответа. */
    static String askOffline(String persona, String message, JSONObject action, long timeoutMs) {
        return askApp(EVENT_OFFLINE, persona, message, action, timeoutMs);
    }

    /** То же для любого вопроса службы (EVENT_OFFLINE, EVENT_VOICE): null —
     * окна нет или оно это событие не слушает, TIMEOUT — не дождались. */
    static String askApp(String event, String persona, String message, JSONObject action, long timeoutMs) {
        WakeWordPlugin p = active;
        if (p == null || !p.hasListeners(event)) return null;
        String id = UUID.randomUUID().toString();
        BlockingQueue<String> q = new ArrayBlockingQueue<>(1);
        asks.put(id, q);
        try {
            JSObject data = new JSObject();
            data.put("id", id);
            data.put("persona", persona);
            data.put("message", message);
            if (action != null) data.put("action", JSObject.fromJSONObject(action));
            p.notifyListeners(event, data);
            String r = q.poll(timeoutMs, TimeUnit.MILLISECONDS);
            return r == null ? TIMEOUT : r;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return TIMEOUT;
        } catch (Exception e) {
            return null;
        } finally {
            asks.remove(id);
        }
    }

    @PluginMethod
    public void offlineReply(PluginCall call) {
        BlockingQueue<String> q = asks.get(call.getString("id", ""));
        if (q != null) q.offer(call.getString("text", ""));
        call.resolve();
    }

    @PluginMethod
    public void takePending(PluginCall call) {
        JSONArray list;
        synchronized (WakeWordService.PENDING_LOCK) {
            SharedPreferences p = WakeWordService.prefs(getContext());
            try {
                list = new JSONArray(p.getString(WakeWordService.KEY_PENDING, "[]"));
            } catch (Exception e) {
                list = new JSONArray();
            }
            p.edit().remove(WakeWordService.KEY_PENDING).apply();
        }
        JSObject ret = new JSObject();
        ret.put("items", list);
        call.resolve(ret);
    }

    private boolean saveConnection(PluginCall call) {
        String baseUrl = call.getString("baseUrl", "");
        String token = call.getString("token", "");
        if (baseUrl == null || !baseUrl.matches("(?i)^https?://.+")) {
            call.reject("baseUrl: нужен адрес http(s)://…", "no-address");
            return false;
        }
        SharedPreferences.Editor e = WakeWordService.prefs(getContext()).edit()
                .putString(WakeWordService.KEY_BASE_URL, baseUrl)
                .putString(WakeWordService.KEY_TOKEN, token == null ? "" : token);
        // Приложение одной персоны: её имена и её чат (не переданы — прежние)
        JSObject d = call.getData();
        for (String k : new String[] {WakeWordService.KEY_ONLY_PERSONA, WakeWordService.KEY_CHAT_ID,
                WakeWordService.KEY_USER_ID, WakeWordService.KEY_USER_NAME}) {
            if (!d.has(k)) continue;
            Object v = d.opt(k);
            e.putString(k, v instanceof String ? ((String) v).trim() : "");
        }
        if (d.has(WakeWordService.KEY_APP_ASKS)) {
            e.putBoolean(WakeWordService.KEY_APP_ASKS, d.optBoolean(WakeWordService.KEY_APP_ASKS, false));
        }
        e.apply();
        return true;
    }

    @PluginMethod
    public void start(PluginCall call) {
        if (!saveConnection(call)) return;
        if (getPermissionState("microphone") != PermissionState.GRANTED) {
            requestPermissionForAlias("microphone", call, "micCallback");
            return;
        }
        afterMic(call);
    }

    @PermissionCallback
    private void micCallback(PluginCall call) {
        if (getPermissionState("microphone") != PermissionState.GRANTED) {
            JSObject ret = new JSObject();
            ret.put("started", false);
            ret.put("reason", "mic-denied");
            call.resolve(ret);
            return;
        }
        afterMic(call);
    }

    // Уведомление службы — не обязательно (служба работает и без него), но
    // без него не видно, что телефон слушает, и не выключить из шторки
    private void afterMic(PluginCall call) {
        if (Build.VERSION.SDK_INT >= 33 && getPermissionState("notifications") != PermissionState.GRANTED) {
            requestPermissionForAlias("notifications", call, "notificationsCallback");
            return;
        }
        launch(call);
    }

    @PermissionCallback
    private void notificationsCallback(PluginCall call) {
        launch(call);
    }

    private void launch(PluginCall call) {
        WakeWordService.prefs(getContext()).edit().putBoolean(WakeWordService.KEY_ENABLED, true).apply();
        try {
            WakeWordService.start(getContext());
        } catch (RuntimeException e) {
            call.reject("Не удалось запустить службу: " + e.getMessage(), "start");
            return;
        }
        JSObject ret = new JSObject();
        ret.put("started", true);
        call.resolve(ret);
    }

    @PluginMethod
    public void stop(PluginCall call) {
        WakeWordService.prefs(getContext()).edit().putBoolean(WakeWordService.KEY_ENABLED, false).apply();
        WakeWordService.stop(getContext());
        call.resolve();
    }

    @PluginMethod
    public void configure(PluginCall call) {
        String baseUrl = call.getString("baseUrl", "");
        SharedPreferences p = WakeWordService.prefs(getContext());
        if (baseUrl == null || baseUrl.isEmpty()) {
            // Сервер сменили или отвязали: прежнее ядро больше не спрашиваем
            p.edit().remove(WakeWordService.KEY_BASE_URL).remove(WakeWordService.KEY_TOKEN).apply();
            WakeWordService.stop(getContext());
            call.resolve();
            return;
        }
        if (!saveConnection(call)) return;
        if (p.getBoolean(WakeWordService.KEY_ENABLED, false) && WakeWordService.micGranted(getContext())) {
            try {
                WakeWordService.start(getContext());
            } catch (RuntimeException e) {
                call.reject("Не удалось запустить службу: " + e.getMessage(), "start");
                return;
            }
        }
        call.resolve();
    }

    @PluginMethod
    public void status(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("enabled", WakeWordService.prefs(getContext()).getBoolean(WakeWordService.KEY_ENABLED, false));
        ret.put("running", WakeWordService.running);
        ret.put("state", WakeWordService.state);
        JSArray names = new JSArray();
        for (String n : WakeWordService.names()) names.put(n);
        ret.put("names", names);
        ret.put("lastError", WakeWordService.lastError);
        ret.put("micGranted", WakeWordService.micGranted(getContext()));
        // Можно ли голосом открыть «Часы» или приложение, пока наше свёрнуто
        ret.put("overlayAllowed", Build.VERSION.SDK_INT < 29 || Settings.canDrawOverlays(getContext()));
        // Разбор команд телефону голосом (PhoneVoiceJs): ready | unsupported | no_bundle | error | off
        ret.put("phoneEngine", PhoneVoiceJs.engineState);
        call.resolve(ret);
    }

    /** Настройки права «Поверх других приложений» для этого приложения; не
     * открылись (прошивка без такого экрана) — общий список, затем «О приложении». */
    @PluginMethod
    public void openOverlaySettings(PluginCall call) {
        String pkg = getContext().getPackageName();
        Intent[] tries = {
                new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:" + pkg)),
                new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION),
                new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + pkg)),
        };
        for (Intent i : tries) {
            try {
                if (getActivity() != null) getActivity().startActivity(i);
                else getContext().startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
                call.resolve();
                return;
            } catch (ActivityNotFoundException | SecurityException e) {
                // следующий вариант
            }
        }
        call.reject("Нет экрана настроек", "no_handler");
    }
}
