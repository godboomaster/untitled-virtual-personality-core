package io.vpcore.app;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.text.InputType;
import android.util.Log;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.ContextCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import java.net.SocketTimeoutException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Окно «Написать» виджета «Живой портрет» (карандаш в углу, PortraitWidget):
 * поле над клавиатурой поверх рабочего стола, без приложения. Отправили —
 * окно закрывается, портрет кивает, читает сообщение в облачке сбоку и
 * думает, ответ — облачком над головой,
 * как всякая реплика персоны; в переписке приложения — та же пара, что из
 * чата.
 *
 * Отправка — POST /api/chat по адресу и токену фоновой службы, как у
 * голосовой активации (WakeWordService.send): тот же чат web_user, client
 * app. Ответ виджету приносит лента реплик (PortraitSaidFeed), если она
 * идёт; не идёт (служба выключена) — кивок с облачком, конец хода и ответ
 * отдаём сами, по ответу запроса. Дважды одно и то же портрет не скажет
 * (PortraitWidget.spoke).
 *
 * Пока окно открыто, ядро проверяется лёгким запросом (GET /api/said без
 * ожидания): не пускает (токен) — сказать здесь же, текст остаётся; не
 * отвечает — как чат без связи: реплика уходит приложению (очередь
 * WakeWordService.KEY_PENDING), оно открывается на чате персоны и отвечает
 * без ядра (облако или модель телефона) или отправит её, когда ядро появится.
 *
 * Команда телефону («поставь будильник на 7») — как в чате и голосом: тот же
 * разбор (PhoneVoiceJs) и исполнение (PhoneVoiceOps), пока окно на экране
 * (открывать «Часы» из фона Android не даёт); ядру — итог, персона отвечает
 * по нему.
 *
 * Микрофон — надиктовать вместо набора: распознавание телефона
 * (SpeechRecognizer, как голосовой режим приложения), текст ложится в поле,
 * отправляете сами. Закрыли, не отправив, — черновик ждёт следующего раза;
 * не дошло (ядро ответило ошибкой) — текст тоже возвращается черновиком, с
 * пометкой: всплывающее сообщение без разрешения на уведомления не видно.
 */
public class PortraitInputActivity extends AppCompatActivity {

    private static final String TAG = "PortraitInput";

    static final String EXTRA_PERSONA = "io.vpcore.app.INPUT_PERSONA";
    // Имя персоны (аватар поверх приложений: у его персоны может не быть виджета)
    static final String EXTRA_NAME = "io.vpcore.app.INPUT_NAME";

    // Окно на экране: команда телефону может открыть чужой экран (PhoneVoiceOps)
    static volatile boolean onScreen = false;

    // Черновики: d.<персона>; f.<персона> — черновик — неотправленная реплика
    private static final String PREFS = "vpc_portrait_input";
    // Тот же чат, что у приложения; свой client_id — пока ответа нет, реплику
    // показывают другие устройства (и приложение на этом телефоне)
    private static final String CHAT_ID = "web_user";
    private static final String CLIENT_ID = "phone-widget";
    private static final int MAX_CHARS = 4000;
    // Проверка связи — пока окно открыто, долго ждать нельзя
    private static final int PROBE_CONNECT_MS = 3_000;
    private static final int PROBE_READ_MS = 4_000;
    // Ответ модели бывает долгим (как у голосовой активации); дольше — его
    // принесёт лента реплик
    private static final int CHAT_READ_MS = 180_000;
    // Разбор команд телефону: один изолят V8 на процесс; без дела столько — закрыть
    private static final long JS_IDLE_MS = 2 * 60_000L;
    private static final int REQ_MIC = 1;

    // Отправка и разбор команд — по очереди в одном потоке: он же держит изолят
    private static final ScheduledExecutorService SEND = Executors.newSingleThreadScheduledExecutor();
    // Только в потоке SEND
    private static PhoneVoiceJs phoneJs;
    private static ScheduledFuture<?> jsClose;

    private final Handler main = new Handler(Looper.getMainLooper());
    private String persona;
    private String name;
    private EditText text;
    private ImageButton mic;
    private ImageButton send;
    private TextView status;
    // Отправлено (или отправляется) — черновик не сохранять
    private boolean sending = false;
    private boolean sent = false;
    // Верх панели на экране (для аватара поверх приложений), -1 — ещё не знаем
    private int panelTop = -1;
    // Распознавание (только в главном потоке)
    private SpeechRecognizer recognizer;
    private String beforeDictation = "";

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        persona = getIntent().getStringExtra(EXTRA_PERSONA);
        if (persona == null || persona.isEmpty()) {
            finish();
            return;
        }
        name = getIntent().getStringExtra(EXTRA_NAME);
        if (name == null || name.isEmpty()) name = PortraitWidget.prefs(this).getString("n." + persona, persona);
        setContentView(R.layout.portrait_input);
        // Панель во всю ширину у нижнего края; тап мимо — закрыть
        getWindow().setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        getWindow().setGravity(Gravity.BOTTOM);
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_VISIBLE
                | WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        setFinishOnTouchOutside(true);
        // Аватар поверх приложений этой персоны садится к полю (как поле у
        // головы на компьютере): где верх панели — после каждой раскладки
        // (клавиатура поднимает её не сразу)
        View decor = getWindow().getDecorView();
        decor.getViewTreeObserver().addOnGlobalLayoutListener(() -> {
            int[] loc = new int[2];
            decor.getLocationOnScreen(loc);
            if (loc[1] != panelTop) {
                panelTop = loc[1];
                AvatarOverlayService.inputPanel(persona, panelTop);
            }
        });

        ((TextView) findViewById(R.id.input_title)).setText(name);
        text = findViewById(R.id.input_text);
        mic = findViewById(R.id.input_mic);
        send = findViewById(R.id.input_send);
        status = findViewById(R.id.input_status);
        // Строки переносятся, а Enter клавиатуры — «Отправить»
        text.setRawInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
        text.setOnEditorActionListener((v, actionId, event) -> {
            boolean enter = event != null && event.getKeyCode() == KeyEvent.KEYCODE_ENTER
                    && event.getAction() == KeyEvent.ACTION_DOWN;
            if (actionId == EditorInfo.IME_ACTION_SEND || enter) {
                send();
                return true;
            }
            return false;
        });
        SharedPreferences dp = prefs(this);
        String draft = dp.getString("d." + persona, "");
        if (!draft.isEmpty()) {
            text.setText(draft);
            text.setSelection(draft.length());
        }
        if (dp.getBoolean("f." + persona, false)) {
            say(getString(R.string.portrait_in_failed_last));
            dp.edit().remove("f." + persona).apply();
        }
        text.requestFocus();
        send.setOnClickListener(v -> send());
        mic.setOnClickListener(v -> toggleMic());
        if (!SpeechRecognizer.isRecognitionAvailable(this)) mic.setVisibility(View.GONE);
        // Разбор команд телефону поднять, пока человек пишет: отправка не ждёт V8
        Context app = getApplicationContext();
        if (PhoneVoiceJs.isSupported()) SEND.execute(() -> warmJs(app));
    }

    @Override
    protected void onResume() {
        super.onResume();
        onScreen = true;
    }

    @Override
    protected void onPause() {
        onScreen = false;
        // Аватар — обратно на своё место
        AvatarOverlayService.inputClosed();
        panelTop = -1;
        stopMic();
        if (!sending && !sent && text != null) {
            String draft = text.getText().toString();
            SharedPreferences.Editor e = prefs(this).edit();
            if (draft.trim().isEmpty()) e.remove("d." + persona);
            else e.putString("d." + persona, draft);
            e.apply();
        }
        super.onPause();
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    // ── Отправка ──────────────────────────────────────────────────────

    private void send() {
        if (sending) return;
        String message = text.getText().toString().trim();
        if (message.isEmpty()) return;
        if (message.length() > MAX_CHARS) message = message.substring(0, MAX_CHARS);
        stopMic();
        sending = true;
        text.setEnabled(false);
        send.setEnabled(false);
        mic.setEnabled(false);
        say(getString(R.string.portrait_in_sending));
        final String msg = message;
        final Context app = getApplicationContext();
        SEND.execute(() -> {
            String link = probe(app);
            // Ядро не пускает — текст остаётся, команду не исполняем (повторят — исполнится дважды)
            JSONObject action = "denied".equals(link) ? null : phoneAction(app, persona, name, msg);
            main.post(() -> afterProbe(app, link, msg, action));
        });
    }

    // Главный поток: связь проверена, команда телефону исполнена
    private void afterProbe(Context app, String link, String message, JSONObject action) {
        if ("denied".equals(link)) {
            sending = false;
            text.setEnabled(true);
            send.setEnabled(true);
            mic.setEnabled(true);
            say(getString(R.string.portrait_in_denied));
            return;
        }
        sent = true;
        sending = false;
        prefs(app).edit().remove("d." + persona).apply();
        if ("ok".equals(link)) {
            // Кивнуть и прочесть сразу, не дожидаясь ленты (её может и не быть)
            PortraitWidget.said(app, persona, "heard", message);
            String p = persona;
            SEND.execute(() -> deliver(app, p, message, action));
        } else {
            // Ядро не отвечает — как чат без связи: ответит приложение
            WakeWordService.keepForApp(app, persona, message, action);
            Intent open = new Intent(app, MainActivity.class)
                    .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                    .putExtra(BackgroundInboxService.EXTRA_PERSONA, persona);
            // Окно уже закрыли (назад, пока проверяли связь) — открыть от приложения
            (isDestroyed() ? app : this).startActivity(open);
        }
        finish();
    }

    /** Связь с ядром: ok, denied (токен) или offline. Только в потоке SEND. */
    private static String probe(Context app) {
        try {
            RoomWidget.request(app, "GET", "/api/said?chat_id=" + CHAT_ID + "&wait=0", null,
                    PROBE_CONNECT_MS, PROBE_READ_MS);
            return "ok";
        } catch (RoomWidget.HttpError e) {
            if (e.code == 401 || e.code == 403) return "denied";
            // Ядро старее ленты (404) или ей нехорошо — оно отвечает, отправка покажет
            return "ok";
        } catch (Exception e) {
            Log.i(TAG, "ядро не отвечает: " + e.getMessage());
            return "offline";
        }
    }

    /** Реплика ядру; ответ — портрету. Только в потоке SEND (окно уже закрыто). */
    private static void deliver(Context app, String persona, String message, JSONObject action) {
        boolean settled = true;
        try {
            JSONObject body = new JSONObject()
                    .put("persona", persona)
                    .put("message", message)
                    .put("user_id", CHAT_ID)
                    .put("chat_id", CHAT_ID)
                    .put("client", "app")
                    .put("client_id", CLIENT_ID);
            if (action != null) body.put("phone_action", action);
            JSONObject r = new JSONObject(RoomWidget.request(app, "POST", "/api/chat", body.toString(),
                    10_000, CHAT_READ_MS));
            // Ответ бывает из нескольких сообщений — портрет говорит их по очереди.
            // Строки хода агента задач — в карточке задачи, не в облачке; ответ
            // из одних них — ход просто кончился
            String reply = BackgroundInboxService.withoutTaskLines(r, r.optString("reply", ""));
            if (reply.isEmpty() && !r.optString("reply", "").trim().isEmpty()) {
                PortraitWidget.said(app, persona, "settled", "");
            } else {
                PortraitWidget.said(app, persona, "reply", reply);
            }
            JSONArray extra = r.optJSONArray("extra_messages");
            for (int i = 0; extra != null && i < extra.length(); i++) {
                PortraitWidget.said(app, persona, "reply",
                        BackgroundInboxService.withoutTaskLines(r, extra.optString(i, "")));
            }
        } catch (SocketTimeoutException e) {
            // Ядро ещё думает — ответ принесёт лента; не принесёт — портрет
            // сам перестанет думать (THINK_MAX_MS)
            Log.i(TAG, "ответ дольше " + CHAT_READ_MS / 1000 + " с");
            settled = false;
        } catch (Exception e) {
            Log.w(TAG, "не отправлено: " + e.getMessage());
            // Текст не пропадает: следующее окно откроется с ним (если там пусто)
            SharedPreferences dp = prefs(app);
            if (dp.getString("d." + persona, "").trim().isEmpty()) {
                dp.edit().putString("d." + persona, message).putBoolean("f." + persona, true).apply();
            }
            String who = PortraitWidget.prefs(app).getString("n." + persona, persona);
            new Handler(Looper.getMainLooper()).post(() ->
                    Toast.makeText(app, app.getString(R.string.portrait_in_failed, who), Toast.LENGTH_LONG).show());
        } finally {
            if (settled) PortraitWidget.said(app, persona, "settled", "");
        }
    }

    // ── Команды телефону (поток SEND) ─────────────────────────────────

    private static void warmJs(Context app) {
        if (phoneJs == null) phoneJs = new PhoneVoiceJs(app);
        phoneJs.warmUp();
        closeJsLater();
    }

    /** Команда телефону? Исполнить и вернуть итог для ядра; null — обычная реплика. */
    private static JSONObject phoneAction(Context app, String persona, String name, String message) {
        if (!PhoneVoiceJs.isSupported()) return null;
        if (phoneJs == null) phoneJs = new PhoneVoiceJs(app);
        closeJsLater();
        JSONObject plan;
        try {
            List<PhoneActionsCore.App> apps;
            try {
                apps = PhoneActionsCore.listApps(app);
            } catch (RuntimeException e) {
                apps = Collections.emptyList();
            }
            List<String> names = new ArrayList<>();
            if (name != null && !name.isEmpty()) names.add(name);
            plan = phoneJs.plan(persona, message, names, apps);
        } catch (Throwable e) {
            Log.w(TAG, "команда телефону: разбор упал", e);
            return null;
        }
        JSONObject report = plan == null ? null : plan.optJSONObject("report");
        if (report == null) return null;
        try {
            String st = PhoneVoiceOps.run(app, plan.optJSONArray("ops"), report);
            if (st != null) report.put("status", st);
        } catch (Throwable e) {
            // Разобрано, но исполнение упало: персона не должна сказать «готово»
            Log.w(TAG, "команда телефону: исполнение упало", e);
            try {
                report.put("status", "failed");
            } catch (Exception ignored) {
                // put строки не падает
            }
        }
        Log.i(TAG, "команда телефону: " + report.optString("kind") + " → " + report.optString("status"));
        return report;
    }

    private static void closeJsLater() {
        if (jsClose != null) jsClose.cancel(false);
        jsClose = SEND.schedule(() -> {
            if (phoneJs != null) phoneJs.close();
            phoneJs = null;
            jsClose = null;
        }, JS_IDLE_MS, TimeUnit.MILLISECONDS);
    }

    // ── Надиктовать ───────────────────────────────────────────────────

    private void toggleMic() {
        if (recognizer != null) {
            // Второй тап — хватит слушать: итог ещё придёт (onResults)
            try {
                recognizer.stopListening();
            } catch (RuntimeException e) {
                stopMic();
            }
            return;
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
            return;
        }
        startMic();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code != REQ_MIC) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) startMic();
        else say(getString(R.string.portrait_in_no_mic));
    }

    private void startMic() {
        // Слушать по имени (WakeWordService) держит микрофон — пусть отпустит
        WakeWordService.appListening(true);
        new Thread(() -> {
            WakeWordService.awaitMicReleased(600);
            main.post(this::startRecognizer);
        }, "portrait-input-mic").start();
    }

    private void startRecognizer() {
        if (isFinishing() || sending || recognizer != null) {
            if (recognizer == null) WakeWordService.appListening(false);
            return;
        }
        try {
            recognizer = SpeechRecognizer.createSpeechRecognizer(this);
        } catch (RuntimeException e) {
            recognizer = null;
            WakeWordService.appListening(false);
            say(getString(R.string.portrait_in_no_speech));
            return;
        }
        beforeDictation = text.getText().toString().trim();
        recognizer.setRecognitionListener(new RecognitionListener() {
            @Override
            public void onReadyForSpeech(Bundle params) {
                say(getString(R.string.portrait_in_listening));
            }

            @Override
            public void onBeginningOfSpeech() {
            }

            @Override
            public void onRmsChanged(float rmsdB) {
            }

            @Override
            public void onBufferReceived(byte[] buffer) {
            }

            @Override
            public void onEndOfSpeech() {
            }

            @Override
            public void onError(int error) {
                stopMic();
                boolean notHeard = error == SpeechRecognizer.ERROR_NO_MATCH
                        || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT;
                say(getString(error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS ? R.string.portrait_in_no_mic
                        : notHeard ? R.string.portrait_in_not_heard : R.string.portrait_in_mic_error));
            }

            @Override
            public void onResults(Bundle results) {
                dictated(results);
                stopMic();
                say(null);
            }

            @Override
            public void onPartialResults(Bundle partial) {
                dictated(partial);
            }

            @Override
            public void onEvent(int eventType, Bundle params) {
            }
        });
        Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
                .putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, getPackageName());
        mic.setImageTintList(ColorStateList.valueOf(getColor(R.color.input_mic_on)));
        mic.setSelected(true);
        try {
            recognizer.startListening(intent);
        } catch (RuntimeException e) {
            stopMic();
            say(getString(R.string.portrait_in_mic_error));
        }
    }

    // Распознанное — в поле, после того, что там уже было
    private void dictated(Bundle b) {
        ArrayList<String> list = b == null ? null : b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
        String heard = list == null || list.isEmpty() || list.get(0) == null ? "" : list.get(0).trim();
        if (heard.isEmpty()) return;
        String all = beforeDictation.isEmpty() ? heard : beforeDictation + " " + heard;
        text.setText(all);
        text.setSelection(all.length());
    }

    private void stopMic() {
        if (recognizer == null) return;
        try {
            recognizer.destroy();
        } catch (RuntimeException ignored) {
            // распознаватель уже отвязан от службы
        }
        recognizer = null;
        // Микрофон свободен — голосовая активация снова слушает
        WakeWordService.appListening(false);
        if (mic != null) {
            mic.setSelected(false);
            mic.setImageTintList(ColorStateList.valueOf(getColor(R.color.widget_text_dim)));
        }
    }

    // Строка состояния под полем; null — убрать
    private void say(String line) {
        if (status == null) return;
        status.setVisibility(line == null ? View.GONE : View.VISIBLE);
        status.setText(line == null ? "" : line);
    }
}
