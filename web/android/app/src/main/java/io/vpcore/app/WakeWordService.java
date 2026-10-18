package io.vpcore.app;

import android.annotation.SuppressLint;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.ToneGenerator;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.util.Log;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.app.ServiceCompat;
import androidx.core.content.ContextCompat;

import com.k2fsa.sherpa.onnx.EndpointConfig;
import com.k2fsa.sherpa.onnx.EndpointRule;
import com.k2fsa.sherpa.onnx.FeatureConfig;
import com.k2fsa.sherpa.onnx.OnlineModelConfig;
import com.k2fsa.sherpa.onnx.OnlineRecognizer;
import com.k2fsa.sherpa.onnx.OnlineRecognizerConfig;
import com.k2fsa.sherpa.onnx.OnlineStream;
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Голосовая активация по имени: «Коннор, какая сегодня погода?» — и персона
 * отвечает голосом, даже при выключенном экране.
 *
 * Служба переднего плана (тип microphone) держит микрофон и частичную
 * блокировку сна, а речь распознаёт sherpa-onnx на телефоне (малая русская
 * потоковая модель, assets/wake-ru) — в сеть уходит только реплика с именем.
 * Имена — features.trigger_words персон (GET /api/personas), сравнение —
 * WakeMatch (тот же алгоритм, что у ядра). Они же — горячие слова
 * распознавателя: модель тянет «коннер» к «коннор».
 *
 * Состояния (как у слушателя ядра на компьютере):
 *   IDLE  — ждём имя; имя в промежуточном тексте — короткий сигнал;
 *   конец фразы с именем: есть команда — BUSY, нет — AWAIT (8 с);
 *   AWAIT — следующая фраза — команда той же персоне (или новое имя);
 *   BUSY  — POST /api/chat и озвучка ответа; звук микрофона выбрасываем,
 *   чтобы служба не слышала сама себя. Ответ кончился вопросом — ещё 6 с
 *   ждём ответа без имени.
 *
 * Команды телефону («Коннор, включи фонарик», «поставь таймер на 5 минут»)
 * — как в чате: тот же разбор (PhoneVoiceJs), исполняет сама служба
 * (PhoneVoiceOps), ядру уходит итог phone_action — персона отвечает по нему.
 *
 * Ядро недоступно: итог команды телефону служба говорит сама; обычная
 * реплика — как в чате без связи: приложение на экране — отвечает оно
 * (облако или модель телефона, плагин WakeWord → initWakeOffline в
 * web/src/wakeWord.ts), реплика и ответ ложатся в его переписку; свёрнуто
 * или закрыто — реплика ждёт его открытия (KEY_PENDING).
 *
 * Android 14+ не даёт запускать службу с микрофоном из фона — её включает
 * приложение (плагин WakeWord) и перезапускает MainActivity, когда
 * приложение снова на экране; при загрузке телефона она не стартует.
 * Голосовой чат в самом приложении (NativeSpeechPlugin) на время забирает
 * микрофон — служба его отпускает.
 *
 * Связь — только по адресу и токену (как у фоновых уведомлений): канал VPC
 * Link живёт в вебе, а не в Java.
 */
public class WakeWordService extends Service {

    private static final String TAG = "WakeWord";

    static final String PREFS = "vpc_wake";
    static final String KEY_ENABLED = "enabled";
    static final String KEY_BASE_URL = "baseUrl";
    static final String KEY_TOKEN = "token";
    // Последний список персон ядра: ядро недоступно — имена всё равно знаем
    private static final String KEY_PERSONAS = "personas";
    // Реплики без ядра и без окна приложения — ему, когда его откроют
    static final String KEY_PENDING = "pending";
    static final Object PENDING_LOCK = new Object();
    private static final int PENDING_MAX = 20;

    // «Выключить» в шторке и выключение из приложения
    static final String ACTION_STOP = "io.vpcore.app.WAKE_STOP";
    // Только отладочная сборка: подать WAV вместо микрофона (проверка на эмуляторе)
    static final String ACTION_DEBUG_WAV = "io.vpcore.app.WAKE_DEBUG_WAV";

    private static final String CHANNEL = "vpc_wake";
    private static final int ONGOING_ID = 2;

    private static final int SAMPLE_RATE = 16000;
    // Порция звука в распознаватель — 100 мс
    private static final int CHUNK = SAMPLE_RATE / 10;
    private static final long AWAIT_MS = 8_000;
    private static final long FOLLOW_UP_MS = 6_000;
    // Пока человек говорит, ожидание команды не кончается
    private static final long SPEAKING_GRACE_MS = 2_000;
    // После озвучки — пауза: хвост своего голоса не должен попасть в распознаватель
    private static final long AFTER_SPEECH_MS = 300;
    private static final long PERSONAS_REFRESH_MS = 10 * 60_000L;
    private static final long PERSONAS_RETRY_MS = 60_000L;
    // Ответ модели ядра может идти минуту и дольше
    private static final int CHAT_READ_TIMEOUT_MS = 180_000;
    // Без ядра: модель телефона отвечает медленнее облака
    private static final long OFFLINE_ANSWER_MS = 90_000;
    private static final float HOTWORD_SCORE = 2.0f;
    // Голосовой чат приложения отпустил микрофон — забираем не сразу: следующая
    // его сессия обычно начинается сразу после озвучки ответа
    private static final long RESUME_DELAY_MS = 1_500;
    // Список приложений для «открой X»: свежий раз в минуту, как у чата
    private static final long APPS_TTL_MS = 60_000;

    private static final String MODEL_DIR = "wake-ru";
    private static final String[] MODEL_FILES = {
            "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt", "unigram_500.vocab",
    };

    private enum Mode { IDLE, WOKE, AWAIT, BUSY }

    // ── Состояние для плагина (status) ────────────────────────────────
    static volatile boolean running = false;
    // off | loading | idle | listening | thinking | speaking | paused | no_names | mic | model
    static volatile String state = "off";
    static volatile List<String> activeNames = Collections.emptyList();
    static volatile String lastError = null;

    // ── Микрофон занят голосовым чатом приложения ─────────────────────
    private static final Object MIC_LOCK = new Object();
    // До каких пор (uptimeMillis) микрофон у приложения. Срок, а не флаг:
    // пропущенный «конец» не оставит службу глухой навсегда
    private static long appListenUntil = 0;
    private static long appSpeakUntil = 0;
    private static long resumeAt = 0;
    private static boolean micOpen = false;

    private final Handler main = new Handler(Looper.getMainLooper());
    private ScheduledExecutorService net;
    // Следующее обновление имён (только в потоке net)
    private ScheduledFuture<?> nextRefresh;
    private ExecutorService chat;
    private Thread audioThread;
    private volatile boolean destroyed = false;
    private PowerManager.WakeLock wakeLock;
    private ToneGenerator tones;
    // Тише основного: «принято» звучит, пока модель думает
    private ToneGenerator softTones;
    private BroadcastReceiver debugReceiver;
    private boolean debuggable;

    // Настройки (меняются в onStartCommand)
    private volatile String baseUrl = "";
    private volatile String token = "";

    // Имена и горячие слова — снимки, заменяются целиком
    private volatile List<WakeMatch.Phrase> phrases = Collections.emptyList();
    private volatile String hotwords = "";
    // Обращения к персоне для разбора команд телефону: имя и trigger_words
    private volatile Map<String, List<String>> personaNames = Collections.emptyMap();

    // Команды телефону: разбор и список приложений — только в потоке chat
    private PhoneVoiceJs phoneJs;
    private List<PhoneActionsCore.App> apps = null;
    private long appsAt = 0;

    private volatile Mode mode = Mode.IDLE;
    private volatile String awaitPersona = null;
    private volatile long awaitDeadline = 0;
    // Перед следующей порцией звука — новый поток распознавания
    private volatile boolean resetPending = false;
    // Сигнал «услышала имя» уже прозвучал в этой фразе
    private boolean chimed = false;
    private String lastPartial = "";

    // Отладка: звук из WAV вместо микрофона (по порциям в аудиопотоке)
    private final ConcurrentLinkedQueue<short[]> debugAudio = new ConcurrentLinkedQueue<>();
    private short[] debugCurrent = null;
    private volatile boolean debugMuteMic = false;
    private int debugPos = 0;

    // Озвучка: поля — только в главном потоке
    private TextToSpeech tts;
    private boolean ttsReady = false;
    private String speechId = null;
    private String speechLastChunk = null;
    private Runnable afterSpeech = null;
    private int speechSeq = 0;

    private final Object notifyLock = new Object();
    private String shownText = null;

    // ── Запуск и остановка (из плагина и MainActivity) ────────────────

    static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static boolean micGranted(Context ctx) {
        return ContextCompat.checkSelfPermission(ctx, android.Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED;
    }

    /** Запустить или перечитать настройки. Только когда приложение на экране. */
    static void start(Context ctx) {
        Intent i = new Intent(ctx, WakeWordService.class);
        if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i);
        else ctx.startService(i);
    }

    static void stop(Context ctx) {
        if (!running) return;
        try {
            ctx.startService(new Intent(ctx, WakeWordService.class).setAction(ACTION_STOP));
        } catch (RuntimeException e) {
            ctx.stopService(new Intent(ctx, WakeWordService.class));
        }
    }

    /** Приложение вышло на экран: включено, а служба не работает (её убила
     * система или телефон перезагрузился) — запустить снова. */
    static void startIfEnabled(Context ctx) {
        if (running) return;
        SharedPreferences p = prefs(ctx);
        if (!p.getBoolean(KEY_ENABLED, false) || p.getString(KEY_BASE_URL, "").isEmpty()) return;
        if (!micGranted(ctx)) return;
        try {
            start(ctx);
        } catch (RuntimeException e) {
            Log.w(TAG, "не запустилась", e);
        }
    }

    // ── Микрофон для голосового чата приложения (NativeSpeechPlugin) ──

    /** Голосовой чат приложения слушает (true) или закончил (false). */
    static void appListening(boolean on) {
        synchronized (MIC_LOCK) {
            long now = SystemClock.uptimeMillis();
            if (on) {
                appListenUntil = now + 60_000;
            } else if (appListenUntil > 0) {
                appListenUntil = 0;
                resumeAt = now + RESUME_DELAY_MS;
            }
        }
    }

    /** Приложение озвучивает ответ: служба не должна принять его за речь. */
    static void appSpeaking(boolean on) {
        synchronized (MIC_LOCK) {
            long now = SystemClock.uptimeMillis();
            if (on) {
                appSpeakUntil = now + 5 * 60_000;
            } else if (appSpeakUntil > 0) {
                appSpeakUntil = 0;
                resumeAt = now + RESUME_DELAY_MS;
            }
        }
    }

    /** Дождаться, пока служба отпустит микрофон (не дольше ms). Не из главного потока. */
    static void awaitMicReleased(long ms) {
        synchronized (MIC_LOCK) {
            long until = SystemClock.uptimeMillis() + ms;
            while (micOpen) {
                long left = until - SystemClock.uptimeMillis();
                if (left <= 0) return;
                try {
                    MIC_LOCK.wait(left);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
        }
    }

    private static boolean appHoldsMic() {
        synchronized (MIC_LOCK) {
            long now = SystemClock.uptimeMillis();
            return now < appListenUntil || now < appSpeakUntil || now < resumeAt;
        }
    }

    private static void setMicOpen(boolean open) {
        synchronized (MIC_LOCK) {
            micOpen = open;
            MIC_LOCK.notifyAll();
        }
    }

    // ── Жизненный цикл ────────────────────────────────────────────────

    @Override
    public void onCreate() {
        super.onCreate();
        debuggable = (getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
        createChannel(this);
        net = Executors.newSingleThreadScheduledExecutor();
        chat = Executors.newSingleThreadExecutor();
        phoneJs = new PhoneVoiceJs(this);
        // Процессор не спит, пока служба работает: иначе с погасшим экраном
        // распознавание встанет. Это и есть цена фичи в батарее
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "vpc:wake-word");
            wakeLock.setReferenceCounted(false);
        }
        try {
            tones = new ToneGenerator(AudioManager.STREAM_MUSIC, 70);
            softTones = new ToneGenerator(AudioManager.STREAM_MUSIC, 35);
        } catch (RuntimeException e) {
            // звуковая система занята — обойдёмся без сигналов
        }
        initTts();
        if (debuggable) {
            debugReceiver = new BroadcastReceiver() {
                @Override
                public void onReceive(Context context, Intent intent) {
                    // muteMic: микрофон эмулятора иногда отдаёт громкий мусор
                    // (RMS ~23000) — для повторяемой проверки вместо него тишина
                    if (intent.hasExtra("muteMic")) {
                        debugMuteMic = intent.getBooleanExtra("muteMic", false);
                        Log.i(TAG, "отладка: микрофон " + (debugMuteMic ? "заглушён" : "включён"));
                    }
                    String path = intent.getStringExtra("path");
                    if (path != null) net.execute(() -> queueDebugWav(path));
                }
            };
            ContextCompat.registerReceiver(this, debugReceiver, new IntentFilter(ACTION_DEBUG_WAV),
                    ContextCompat.RECEIVER_EXPORTED);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            // «Выключить» в шторке — выбор пользователя, как и в приложении
            prefs(this).edit().putBoolean(KEY_ENABLED, false).apply();
            halt(startId);
            return START_NOT_STICKY;
        }
        SharedPreferences p = prefs(this);
        String url = p.getString(KEY_BASE_URL, "");
        boolean want = p.getBoolean(KEY_ENABLED, false) && !url.isEmpty() && micGranted(this);
        // startForeground — сразу и всегда: после startForegroundService
        // остановка без него роняет приложение
        try {
            int type = Build.VERSION.SDK_INT >= 30 ? ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE : 0;
            ServiceCompat.startForeground(this, ONGOING_ID, buildNotification(), type);
        } catch (RuntimeException e) {
            // Android 14+: служба с микрофоном не стартует из фона
            Log.w(TAG, "startForeground не удался", e);
            lastError = "start: " + e.getClass().getSimpleName();
            halt(startId);
            return START_NOT_STICKY;
        }
        if (!want) {
            halt(startId);
            return START_NOT_STICKY;
        }
        baseUrl = url.replaceAll("/+$", "");
        token = p.getString(KEY_TOKEN, "");
        running = true;
        lastError = null;
        if (wakeLock != null && !wakeLock.isHeld()) wakeLock.acquire();
        boolean first = audioThread == null;
        if (first) setState("loading");
        if (phrases.isEmpty()) applyPersonas(p.getString(KEY_PERSONAS, ""), false);
        // Имена — сразу (новый адрес или токен), дальше раз в 10 минут; одна
        // цепочка обновлений, сколько бы раз службу ни перезапускали
        net.execute(() -> {
            if (nextRefresh != null) nextRefresh.cancel(false);
            refreshPersonas();
        });
        if (first) {
            audioThread = new Thread(this::audioLoop, "vpc-wake-audio");
            audioThread.start();
            // Движок разбора команд и список приложений — заранее, пока ждём имя
            chat.execute(() -> {
                phoneJs.warmUp();
                installedApps();
            });
        }
        // Система убила процесс — сама служба с микрофоном из фона всё равно
        // не поднимется (Android 14+); её запустит приложение
        return START_NOT_STICKY;
    }

    private void halt(int startId) {
        running = false;
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
        stopSelf(startId);
    }

    @Override
    public void onDestroy() {
        synchronized (notifyLock) {
            destroyed = true;
            NotificationManagerCompat.from(this).cancel(ONGOING_ID);
        }
        running = false;
        state = "off";
        activeNames = Collections.emptyList();
        if (debugReceiver != null) {
            try {
                unregisterReceiver(debugReceiver);
            } catch (RuntimeException ignored) {
                // не зарегистрирован
            }
        }
        // Распознаватель и микрофон освобождает сам аудиопоток, выходя из цикла
        if (audioThread != null) audioThread.interrupt();
        net.shutdownNow();
        chat.shutdownNow();
        phoneJs.close();
        main.removeCallbacksAndMessages(null);
        if (tts != null) {
            try {
                tts.stop();
                tts.shutdown();
            } catch (RuntimeException ignored) {
                // движок уже отвязан
            }
            tts = null;
        }
        if (tones != null) tones.release();
        if (softTones != null) softTones.release();
        try {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        } catch (RuntimeException ignored) {
            // уже снята
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ── Имена персон ──────────────────────────────────────────────────

    private void refreshPersonas() {
        if (destroyed) return;
        long next = PERSONAS_REFRESH_MS;
        try {
            String body = httpGet(baseUrl + "/api/personas");
            applyPersonas(body, true);
        } catch (Exception e) {
            Log.w(TAG, "имена не обновились: " + e.getMessage());
            if (phrases.isEmpty()) {
                lastError = e.getMessage();
                next = PERSONAS_RETRY_MS;
                if (mode == Mode.IDLE) setState("no_names");
            }
        }
        if (!destroyed) {
            try {
                nextRefresh = net.schedule(this::refreshPersonas, next, TimeUnit.MILLISECONDS);
            } catch (RuntimeException ignored) {
                // служба остановлена
            }
        }
    }

    private void applyPersonas(String json, boolean save) {
        if (json == null || json.isEmpty()) return;
        try {
            JSONArray arr = new JSONArray(json);
            List<WakeMatch.Phrase> list = WakeMatch.wakePhrases(arr);
            phrases = Collections.unmodifiableList(list);
            hotwords = WakeMatch.hotwords(list, HOTWORD_SCORE);
            personaNames = namesByPersona(arr);
            activeNames = WakeMatch.displayNames(arr, list);
            if (save) prefs(this).edit().putString(KEY_PERSONAS, json).apply();
            if (debuggable) Log.i(TAG, "имена: " + activeNames + " горячие слова: " + hotwords);
            if (mode == Mode.IDLE && !"loading".equals(state)) setState(phrases.isEmpty() ? "no_names" : "idle");
            else refreshNotification();
        } catch (Exception e) {
            Log.w(TAG, "список персон не разобран", e);
        }
    }

    // Имя персоны и её trigger_words — чтобы разбор команды срезал обращение
    private static Map<String, List<String>> namesByPersona(JSONArray arr) {
        Map<String, List<String>> out = new HashMap<>();
        for (int i = 0; i < arr.length(); i++) {
            JSONObject p = arr.optJSONObject(i);
            if (p == null) continue;
            String id = p.optString("id", "");
            if (id.isEmpty()) continue;
            List<String> names = new ArrayList<>();
            String name = p.optString("name", "").trim();
            if (!name.isEmpty()) names.add(name);
            JSONObject features = p.optJSONObject("features");
            JSONArray words = features == null ? null : features.optJSONArray("trigger_words");
            for (int j = 0; words != null && j < words.length(); j++) {
                Object w = words.opt(j);
                if (w instanceof String && !((String) w).trim().isEmpty() && !names.contains(((String) w).trim())) {
                    names.add(((String) w).trim());
                }
            }
            out.put(id, Collections.unmodifiableList(names));
        }
        return Collections.unmodifiableMap(out);
    }

    // ── Распознавание (аудиопоток) ────────────────────────────────────

    private void audioLoop() {
        OnlineRecognizer recognizer;
        try {
            File dir = installModel();
            recognizer = new OnlineRecognizer(null, recognizerConfig(dir));
        } catch (Throwable e) {
            Log.e(TAG, "распознаватель не загрузился", e);
            lastError = "model: " + e.getMessage();
            setState("model");
            return;
        }
        if (!destroyed) toIdle();
        OnlineStream stream = null;
        String streamHotwords = null;
        AudioRecord rec = null;
        short[] pcm = new short[CHUNK];
        float[] samples = new float[CHUNK];
        long decodeNs = 0;
        double micEnergy = 0;
        long micSamples = 0;
        int decodeChunks = 0;
        try {
            while (!destroyed && !Thread.currentThread().isInterrupted()) {
                if (appHoldsMic()) {
                    // Голосовой чат приложения слушает — микрофон его
                    if (rec != null) {
                        rec = closeMic(rec);
                        setState("paused");
                    }
                    SystemClock.sleep(100);
                    continue;
                }
                if (rec == null) {
                    rec = openMic();
                    if (rec == null) {
                        setState("mic");
                        SystemClock.sleep(2_000);
                        continue;
                    }
                    if ("paused".equals(state) || "mic".equals(state)) toIdle();
                }
                if (!readChunk(rec, pcm)) {
                    rec = closeMic(rec); // сбой записи — откроем заново
                    SystemClock.sleep(500);
                    continue;
                }
                if (debuggable) {
                    for (short v : pcm) micEnergy += (double) v * v;
                    micSamples += pcm.length;
                }
                feedDebugAudio(pcm);
                List<WakeMatch.Phrase> names = phrases;
                // Отвечаем — свой голос не слушаем; имён нет — и слушать нечего
                if (mode == Mode.BUSY || names.isEmpty()) continue;
                String hw = hotwords;
                // Новый поток: после ответа (старый звук выброшен) или сменились
                // имена — но не посреди фразы
                if (stream == null || resetPending
                        || (!hw.equals(streamHotwords) && mode == Mode.IDLE && lastPartial.isEmpty())) {
                    if (stream != null) stream.release();
                    stream = recognizer.createStream(hw);
                    if (debuggable) Log.d(TAG, "новый поток распознавания: " + hw);
                    streamHotwords = hw;
                    resetPending = false;
                    lastPartial = "";
                    chimed = false;
                }
                for (int i = 0; i < CHUNK; i++) samples[i] = pcm[i] / 32768f;
                long t0 = System.nanoTime();
                stream.acceptWaveform(samples, SAMPLE_RATE);
                while (recognizer.isReady(stream)) recognizer.decode(stream);
                String text = recognizer.getResult(stream).getText().trim();
                decodeNs += System.nanoTime() - t0;
                if (++decodeChunks == 300) {
                    // Сколько процессора уходит на 100 мс звука (должно быть ≪ 100)
                    if (debuggable) {
                        Log.i(TAG, String.format(Locale.ROOT, "распознавание: %.1f мс на 100 мс звука, микрофон: RMS %.0f",
                                decodeNs / 1e6 / decodeChunks, Math.sqrt(micEnergy / Math.max(1, micSamples))));
                    }
                    micEnergy = 0;
                    micSamples = 0;
                    decodeNs = 0;
                    decodeChunks = 0;
                }
                onPartial(text, names);
                if (recognizer.isEndpoint(stream)) {
                    // Конец фразы — новый поток, а не reset: reset переносит
                    // в следующую фразу контекст прошлой (и шума перед ней),
                    // а имя в начале фразы должно распознаваться с чистого листа
                    resetPending = true;
                    lastPartial = "";
                    onFinal(text, names);
                }
                checkAwaitTimeout();
            }
        } catch (Throwable e) {
            Log.e(TAG, "аудиопоток упал", e);
            lastError = "audio: " + e.getMessage();
            setState("model");
        } finally {
            if (rec != null) closeMic(rec);
            if (stream != null) stream.release();
            recognizer.release();
        }
    }

    private OnlineRecognizerConfig recognizerConfig(File dir) {
        OnlineTransducerModelConfig t = new OnlineTransducerModelConfig();
        t.setEncoder(new File(dir, "encoder.int8.onnx").getAbsolutePath());
        t.setDecoder(new File(dir, "decoder.int8.onnx").getAbsolutePath());
        t.setJoiner(new File(dir, "joiner.int8.onnx").getAbsolutePath());
        OnlineModelConfig m = new OnlineModelConfig();
        m.setTransducer(t);
        m.setTokens(new File(dir, "tokens.txt").getAbsolutePath());
        // Один поток: модель малая, а батарею бережём
        m.setNumThreads(1);
        m.setProvider("cpu");
        m.setDebug(false);
        // Горячие слова (имена) кодируются BPE-словарём модели
        m.setModelingUnit("bpe");
        m.setBpeVocab(new File(dir, "unigram_500.vocab").getAbsolutePath());
        OnlineRecognizerConfig c = new OnlineRecognizerConfig();
        c.setFeatConfig(new FeatureConfig(SAMPLE_RATE, 80, 0f));
        c.setModelConfig(m);
        c.setEnableEndpoint(true);
        // Конец фразы: 2.4 с тишины без речи, 0.8 с — после речи, не длиннее 20 с
        c.setEndpointConfig(new EndpointConfig(
                new EndpointRule(false, 2.4f, 0f),
                new EndpointRule(true, 0.8f, 0f),
                new EndpointRule(false, 0f, 20f)));
        c.setDecodingMethod("modified_beam_search");
        c.setMaxActivePaths(4);
        c.setHotwordsScore(HOTWORD_SCORE);
        return c;
    }

    /** Модель из APK — в файлы приложения (нативный код читает файлы с
     * диска). Копируется заново после обновления приложения. */
    private File installModel() throws Exception {
        File dir = new File(getNoBackupFilesDir(), MODEL_DIR);
        String stamp = String.valueOf(getPackageManager().getPackageInfo(getPackageName(), 0).lastUpdateTime);
        File marker = new File(dir, ".stamp");
        if (marker.isFile() && stamp.equals(readFile(marker))) return dir;
        if (!dir.isDirectory() && !dir.mkdirs()) throw new IOException("mkdir " + dir);
        byte[] buf = new byte[1 << 16];
        for (String name : MODEL_FILES) {
            File tmp = new File(dir, name + ".tmp");
            try (InputStream in = getAssets().open(MODEL_DIR + "/" + name);
                 OutputStream out = new FileOutputStream(tmp)) {
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            }
            if (!tmp.renameTo(new File(dir, name))) throw new IOException("rename " + name);
        }
        try (OutputStream out = new FileOutputStream(marker)) {
            out.write(stamp.getBytes(StandardCharsets.UTF_8));
        }
        return dir;
    }

    @SuppressLint("MissingPermission") // проверено micGranted перед запуском службы
    private AudioRecord openMic() {
        if (!micGranted(this)) return null;
        try {
            int min = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT);
            AudioRecord r = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min, SAMPLE_RATE * 2));
            if (r.getState() != AudioRecord.STATE_INITIALIZED) {
                r.release();
                return null;
            }
            r.startRecording();
            setMicOpen(true);
            return r;
        } catch (RuntimeException e) {
            Log.w(TAG, "микрофон не открылся", e);
            return null;
        }
    }

    private AudioRecord closeMic(AudioRecord r) {
        try {
            r.stop();
        } catch (RuntimeException ignored) {
            // уже остановлен
        }
        r.release();
        setMicOpen(false);
        return null;
    }

    private static boolean readChunk(AudioRecord r, short[] pcm) {
        int got = 0;
        while (got < pcm.length) {
            int n = r.read(pcm, got, pcm.length - got);
            if (n < 0) return false;
            if (n == 0) SystemClock.sleep(10);
            got += n;
        }
        return true;
    }

    // ── Машина состояний (аудиопоток) ─────────────────────────────────

    private void onPartial(String text, List<WakeMatch.Phrase> names) {
        if (text.equals(lastPartial)) return;
        lastPartial = text;
        if (text.isEmpty()) return;
        if (debuggable) Log.d(TAG, "… " + text);
        if (mode == Mode.AWAIT) {
            // Человек говорит — ждём конца фразы, а не таймера
            awaitDeadline = Math.max(awaitDeadline, SystemClock.uptimeMillis() + SPEAKING_GRACE_MS);
        }
        if (mode == Mode.IDLE && !chimed && WakeMatch.match(text, names) != null) {
            chimed = true;
            mode = Mode.WOKE;
            tone(ToneGenerator.TONE_PROP_BEEP, 120);
            setState("listening");
            Log.i(TAG, "имя услышано");
        }
    }

    private void onFinal(String text, List<WakeMatch.Phrase> names) {
        boolean wasChimed = chimed;
        chimed = false;
        if (debuggable && !text.isEmpty()) Log.i(TAG, "фраза: «" + text + "» (" + mode + ")");
        Mode m = mode;
        if (m == Mode.IDLE || m == Mode.WOKE) {
            WakeMatch.Hit hit = WakeMatch.match(text, names);
            if (hit == null) {
                if (m == Mode.WOKE) toIdle();
                return;
            }
            if (!wasChimed) tone(ToneGenerator.TONE_PROP_BEEP, 120);
            if (hit.command.isEmpty()) startAwait(hit.persona, AWAIT_MS);
            else send(hit.persona, hit.command);
        } else if (m == Mode.AWAIT) {
            if (text.isEmpty()) return;
            WakeMatch.Hit hit = WakeMatch.match(text, names);
            if (hit != null) {
                // Снова имя (то же или другой персоны): без команды — ждём дальше
                if (hit.command.isEmpty()) startAwait(hit.persona, AWAIT_MS);
                else send(hit.persona, hit.command);
            } else {
                send(awaitPersona, text);
            }
        }
    }

    private void checkAwaitTimeout() {
        if (mode == Mode.AWAIT && SystemClock.uptimeMillis() > awaitDeadline) {
            Log.i(TAG, "команды не было — жду имя");
            tone(ToneGenerator.TONE_PROP_NACK, 150);
            toIdle();
        }
    }

    private void startAwait(String persona, long ms) {
        awaitPersona = persona;
        awaitDeadline = SystemClock.uptimeMillis() + ms;
        mode = Mode.AWAIT;
        setState("listening");
        Log.i(TAG, "жду команду для " + persona);
    }

    private void toIdle() {
        mode = Mode.IDLE;
        awaitPersona = null;
        setState(phrases.isEmpty() ? "no_names" : "idle");
    }

    // ── Отправка и озвучка ────────────────────────────────────────────

    private void send(String persona, String message) {
        mode = Mode.BUSY;
        // «Принято» — другой тон, чем у пробуждения: ответ модели бывает долгим
        tone(softTones, ToneGenerator.TONE_PROP_ACK, 100);
        setState("thinking");
        Log.i(TAG, "→ " + persona + (debuggable ? ": «" + message + "»" : ""));
        try {
            chat.execute(() -> {
                String say;
                boolean followUp = false;
                // Команда телефону — исполнена здесь (и без ядра), ядру только итог
                JSONObject action = phoneAction(persona, message);
                try {
                    JSONObject body = new JSONObject()
                            .put("persona", persona)
                            .put("message", message)
                            // Тот же чат, что у приложения: ответ виден и в ленте
                            .put("user_id", "web_user")
                            .put("chat_id", "web_user")
                            .put("client", "app")
                            // Голосом: ядро гасит ту же фразу, услышанную и
                            // компьютером, и телефоном (ответ — duplicate)
                            .put("voice", true);
                    if (action != null) body.put("phone_action", action);
                    JSONObject r = new JSONObject(httpPostJson(baseUrl + "/api/chat", body.toString()));
                    if (r.optBoolean("duplicate", false)) {
                        // Эту просьбу уже выполняет другой слушатель — молчим
                        Log.i(TAG, "← повтор: просьбу уже выполняет другой слушатель");
                        main.post(() -> {
                            if (destroyed) return;
                            resetPending = true;
                            toIdle();
                        });
                        return;
                    }
                    StringBuilder all = new StringBuilder(r.optString("reply", ""));
                    JSONArray extra = r.optJSONArray("extra_messages");
                    for (int i = 0; extra != null && i < extra.length(); i++) {
                        String part = extra.optString(i, "");
                        if (!part.trim().isEmpty()) all.append(' ').append(part);
                    }
                    say = WakeMatch.forSpeech(all.toString());
                    if (say.isEmpty()) say = getString(R.string.wake_say_empty);
                    else followUp = say.endsWith("?");
                    Log.i(TAG, "← ответ " + say.length() + " симв." + (followUp ? ", вопрос" : ""));
                } catch (HttpError e) {
                    Log.w(TAG, "ядро: HTTP " + e.code);
                    lastError = "HTTP " + e.code;
                    say = getString(e.code == 401 || e.code == 403 ? R.string.wake_say_denied : R.string.wake_say_error);
                } catch (Exception e) {
                    Log.w(TAG, "ядро недоступно: " + e);
                    lastError = e.getMessage();
                    say = answerWithoutCore(persona, message, action);
                    followUp = say.endsWith("?");
                }
                final boolean ask = followUp;
                speak(say, () -> main.postDelayed(() -> {
                    if (destroyed) return;
                    // Новый поток распознавания — до смены режима (см. audioLoop)
                    resetPending = true;
                    if (ask) {
                        tone(ToneGenerator.TONE_PROP_BEEP, 120);
                        startAwait(persona, FOLLOW_UP_MS);
                    } else {
                        toIdle();
                    }
                }, AFTER_SPEECH_MS));
            });
        } catch (RuntimeException e) {
            toIdle(); // служба остановлена
        }
    }

    /** Ядро недоступно: ответ приложения без него (как на реплику в чате) —
     * что сказать. Только в потоке chat. */
    private String answerWithoutCore(String persona, String message, JSONObject action) {
        // Команда телефону уже исполнена — итог своими словами, сразу
        if (action != null) return PhoneVoiceOps.say(this, action);
        // Свёрнутое приложение Android WebView замораживает (JS стоит, пока
        // его не откроют) — спрашиваем, только когда оно на экране; иначе
        // реплика ждёт его открытия (takePending), а служба слушает дальше
        String reply = BackgroundInboxService.appVisible
                ? WakeWordPlugin.askOffline(persona, message, null, OFFLINE_ANSWER_MS) : null;
        if (reply == null) {
            keepForApp(persona, message);
            return getString(R.string.wake_say_offline_later);
        }
        if (WakeWordPlugin.TIMEOUT.equals(reply)) return getString(R.string.wake_say_offline_slow);
        String say = WakeMatch.forSpeech(reply);
        if (!say.isEmpty()) return say;
        // Ответить некому (ни облака, ни модели телефона) — реплика ждёт ядро в чате
        return getString(R.string.wake_say_offline_queued);
    }

    private void keepForApp(String persona, String message) {
        synchronized (PENDING_LOCK) {
            SharedPreferences p = prefs(this);
            JSONArray list;
            try {
                list = new JSONArray(p.getString(KEY_PENDING, "[]"));
            } catch (Exception e) {
                list = new JSONArray();
            }
            try {
                list.put(new JSONObject()
                        .put("persona", persona)
                        .put("message", message)
                        .put("ts", System.currentTimeMillis() / 1000.0));
            } catch (Exception ignored) {
                return; // put строк не падает
            }
            while (list.length() > PENDING_MAX) list.remove(0);
            p.edit().putString(KEY_PENDING, list.toString()).apply();
        }
    }

    /** Реплика — команда телефону? Исполнить и вернуть итог (PhoneActionReport);
     * null — обычная реплика или разбор не удался (уйдёт как текст).
     * Только в потоке chat. */
    private JSONObject phoneAction(String persona, String message) {
        JSONObject plan;
        try {
            List<String> names = personaNames.get(persona);
            plan = phoneJs.plan(persona, message, names == null ? Collections.emptyList() : names, installedApps());
        } catch (Throwable e) {
            Log.w(TAG, "команда телефону: разбор упал", e);
            return null;
        }
        JSONObject report = plan == null ? null : plan.optJSONObject("report");
        if (report == null) return null;
        if (debuggable) Log.i(TAG, "команда телефону: " + plan);
        try {
            String status = PhoneVoiceOps.run(this, plan.optJSONArray("ops"), report);
            if (status != null) report.put("status", status);
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

    private List<PhoneActionsCore.App> installedApps() {
        long now = SystemClock.uptimeMillis();
        if (apps == null || now - appsAt > APPS_TTL_MS) {
            try {
                apps = PhoneActionsCore.listApps(this);
                appsAt = now;
            } catch (RuntimeException e) {
                if (apps == null) return Collections.emptyList();
            }
        }
        return apps;
    }

    private void initTts() {
        try {
            tts = new TextToSpeech(this, status -> main.post(() -> {
                if (tts == null) return;
                ttsReady = status == TextToSpeech.SUCCESS;
                if (!ttsReady) {
                    Log.w(TAG, "озвучка недоступна: " + status);
                    return;
                }
                // Голос ассистента: громкость и маршрут — как у голосового помощника
                tts.setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build());
            }));
        } catch (RuntimeException e) {
            tts = null;
            return;
        }
        tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
            @Override
            public void onStart(String utteranceId) {}

            @Override
            public void onDone(String utteranceId) {
                main.post(() -> {
                    if (utteranceId != null && utteranceId.equals(speechLastChunk)) finishSpeech(speechId);
                });
            }

            @Override
            @Deprecated
            public void onError(String utteranceId) {
                onError(utteranceId, -1);
            }

            @Override
            public void onError(String utteranceId, int errorCode) {
                Log.w(TAG, "озвучка: ошибка " + errorCode);
                main.post(() -> {
                    String id = speechId;
                    if (id == null || utteranceId == null || !utteranceId.startsWith(id + "-")) return;
                    if (tts != null) tts.stop();
                    finishSpeech(id);
                });
            }

            @Override
            public void onStop(String utteranceId, boolean interrupted) {
                main.post(() -> {
                    String id = speechId;
                    if (id != null && utteranceId != null && utteranceId.startsWith(id + "-")) finishSpeech(id);
                });
            }
        });
    }

    /** Произнести и потом выполнить then (в главном потоке). Озвучки нет или
     * она сорвалась — then всё равно выполнится: служба не должна застрять. */
    private void speak(String text, Runnable then) {
        main.post(() -> {
            if (destroyed) return;
            if (tts == null || !ttsReady || text.isEmpty()) {
                main.postDelayed(then, 200);
                return;
            }
            setState("speaking");
            String id = "wake" + (++speechSeq);
            speechId = id;
            afterSpeech = then;
            boolean cyr = text.matches("(?s).*[а-яА-ЯёЁ].*");
            try {
                int lang = tts.setLanguage(cyr ? new Locale("ru", "RU") : Locale.US);
                if (lang < 0) Log.w(TAG, "голоса для языка нет: " + lang);
            } catch (RuntimeException ignored) {
                // движок не принял язык — говорим голосом по умолчанию
            }
            List<String> chunks = NativeSpeechPlugin.splitForTts(text,
                    Math.max(200, TextToSpeech.getMaxSpeechInputLength() - 100));
            speechLastChunk = id + "-" + (chunks.size() - 1);
            for (int i = 0; i < chunks.size(); i++) {
                int res = tts.speak(chunks.get(i), i == 0 ? TextToSpeech.QUEUE_FLUSH : TextToSpeech.QUEUE_ADD,
                        null, id + "-" + i);
                if (res != TextToSpeech.SUCCESS) {
                    Log.w(TAG, "озвучка не принята: " + res);
                    tts.stop();
                    finishSpeech(id);
                    return;
                }
            }
            // Страховка: движок так и не сказал «готово»
            long guard = Math.min(10_000 + text.length() * 120L, 5 * 60_000L);
            main.postDelayed(() -> finishSpeech(id), guard);
        });
    }

    // Только из главного потока
    private void finishSpeech(String id) {
        if (id == null || !id.equals(speechId)) return;
        speechId = null;
        speechLastChunk = null;
        Runnable r = afterSpeech;
        afterSpeech = null;
        if (r != null) r.run();
    }

    private void tone(int type, int ms) {
        tone(tones, type, ms);
    }

    private static void tone(ToneGenerator t, int type, int ms) {
        if (t == null) return;
        try {
            t.startTone(type, ms);
        } catch (RuntimeException ignored) {
            // звуковая система занята
        }
    }

    // ── HTTP ──────────────────────────────────────────────────────────

    private static final class HttpError extends Exception {
        final int code;

        HttpError(int code) {
            super("HTTP " + code);
            this.code = code;
        }
    }

    private String httpGet(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        try {
            c.setConnectTimeout(10_000);
            c.setReadTimeout(20_000);
            c.setUseCaches(false);
            c.setRequestProperty("Accept", "application/json");
            String tok = token;
            if (!tok.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + tok);
            int code = c.getResponseCode();
            if (code != 200) throw new HttpError(code);
            return readAll(c.getInputStream());
        } finally {
            c.disconnect();
        }
    }

    private String httpPostJson(String url, String json) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        try {
            c.setConnectTimeout(10_000);
            c.setReadTimeout(CHAT_READ_TIMEOUT_MS);
            c.setUseCaches(false);
            c.setDoOutput(true);
            c.setRequestMethod("POST");
            c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            c.setRequestProperty("Accept", "application/json");
            String tok = token;
            if (!tok.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + tok);
            byte[] data = json.getBytes(StandardCharsets.UTF_8);
            c.setFixedLengthStreamingMode(data.length);
            try (OutputStream out = c.getOutputStream()) {
                out.write(data);
            }
            int code = c.getResponseCode();
            if (code != 200) throw new HttpError(code);
            return readAll(c.getInputStream());
        } finally {
            c.disconnect();
        }
    }

    private static String readAll(InputStream in) throws IOException {
        try (InputStream s = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = s.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString("UTF-8");
        }
    }

    private static String readFile(File f) {
        try {
            return readAll(new FileInputStream(f)).trim();
        } catch (IOException e) {
            return "";
        }
    }

    // ── Отладка: WAV вместо микрофона ─────────────────────────────────

    /** adb shell am broadcast -a io.vpcore.app.WAKE_DEBUG_WAV -p io.vpcore.app
     * --es path x.wav — файл (16 кГц, моно, 16 бит) из
     * /sdcard/Android/data/io.vpcore.app/files/ идёт в распознаватель вместо
     * микрофона, тем же путём; --ez muteMic true — между файлами тишина, а не
     * микрофон. Только в отладочной сборке. */
    private void queueDebugWav(String path) {
        File f = path.startsWith("/") ? new File(path) : new File(getExternalFilesDir(null), path);
        try {
            byte[] all;
            try (InputStream in = new FileInputStream(f)) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                all = out.toByteArray();
            }
            ByteBuffer b = ByteBuffer.wrap(all).order(ByteOrder.LITTLE_ENDIAN);
            if (all.length < 12 || b.getInt(0) != 0x46464952 || b.getInt(8) != 0x45564157) {
                throw new IOException("не WAV");
            }
            int pos = 12;
            int channels = 0;
            int rate = 0;
            int bits = 0;
            while (pos + 8 <= all.length) {
                int id = b.getInt(pos);
                int size = b.getInt(pos + 4);
                int bodyAt = pos + 8;
                if (id == 0x20746d66) { // "fmt "
                    channels = b.getShort(bodyAt + 2);
                    rate = b.getInt(bodyAt + 4);
                    bits = b.getShort(bodyAt + 14);
                } else if (id == 0x61746164) { // "data"
                    if (channels != 1 || rate != SAMPLE_RATE || bits != 16) {
                        throw new IOException("нужен 16 кГц моно 16 бит, а не " + rate + "/" + channels + "/" + bits);
                    }
                    int n = Math.min(size, all.length - bodyAt) / 2;
                    short[] s = new short[n];
                    for (int i = 0; i < n; i++) s[i] = b.getShort(bodyAt + i * 2);
                    debugAudio.add(s);
                    Log.i(TAG, "отладка: WAV " + f.getName() + " — " + (n * 1000L / SAMPLE_RATE) + " мс в очереди");
                    return;
                }
                pos = bodyAt + size + (size & 1);
            }
            throw new IOException("нет данных");
        } catch (Exception e) {
            Log.w(TAG, "отладка: WAV " + f + " не прочитан: " + e.getMessage());
        }
    }

    // Аудиопоток: очередной кусок WAV заменяет кусок с микрофона
    private void feedDebugAudio(short[] pcm) {
        if (debugMuteMic) java.util.Arrays.fill(pcm, (short) 0);
        if (debugCurrent == null) {
            debugCurrent = debugAudio.poll();
            debugPos = 0;
            if (debugCurrent == null) return;
        }
        int n = Math.min(pcm.length, debugCurrent.length - debugPos);
        System.arraycopy(debugCurrent, debugPos, pcm, 0, n);
        for (int i = n; i < pcm.length; i++) pcm[i] = 0;
        debugPos += n;
        if (debugPos >= debugCurrent.length) debugCurrent = null;
    }

    // ── Уведомление ───────────────────────────────────────────────────

    private void setState(String s) {
        boolean changed = !s.equals(state);
        state = s;
        if (changed) {
            if (debuggable) Log.i(TAG, "состояние: " + s);
            refreshNotification();
        }
    }

    private void refreshNotification() {
        synchronized (notifyLock) {
            if (destroyed || !running) return;
            Notification n = buildNotification();
            String key = n.extras.getString(Notification.EXTRA_TITLE) + "|" + n.extras.getString(Notification.EXTRA_TEXT);
            if (key.equals(shownText)) return;
            shownText = key;
            try {
                NotificationManagerCompat.from(this).notify(ONGOING_ID, n);
            } catch (SecurityException e) {
                // Нет разрешения на уведомления — служба работает без значка
            }
        }
    }

    private Notification buildNotification() {
        List<String> names = activeNames;
        String title = names.isEmpty()
                ? getString(R.string.wake_title_none)
                : getString(R.string.wake_title, String.join(", ", names));
        Intent open = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0x3a7e, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent off = PendingIntent.getService(this, 0x3a7f,
                new Intent(this, WakeWordService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new NotificationCompat.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_stat_vpc)
                .setContentTitle(title)
                .setContentText(stateText(state))
                .setContentIntent(pi)
                .addAction(0, getString(R.string.wake_stop), off)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setShowWhen(false)
                .setSilent(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .setCategory(NotificationCompat.CATEGORY_SERVICE)
                .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
                .build();
    }

    private String stateText(String s) {
        switch (s) {
            case "loading":
                return getString(R.string.wake_state_loading);
            case "listening":
                return getString(R.string.wake_state_listening);
            case "thinking":
                return getString(R.string.wake_state_thinking);
            case "speaking":
                return getString(R.string.wake_state_speaking);
            case "paused":
                return getString(R.string.wake_state_paused);
            case "no_names":
                return getString(R.string.wake_state_no_names);
            case "mic":
                return getString(R.string.wake_state_mic);
            case "model":
                return getString(R.string.wake_state_model);
            default:
                return getString(R.string.wake_state_idle);
        }
    }

    static void createChannel(Context ctx) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        NotificationChannel ch = new NotificationChannel(CHANNEL,
                ctx.getString(R.string.wake_channel), NotificationManager.IMPORTANCE_LOW);
        ch.setDescription(ctx.getString(R.string.wake_channel_desc));
        ch.setShowBadge(false);
        nm.createNotificationChannel(ch);
    }

    /** Имена для настроек приложения (status плагина). */
    static List<String> names() {
        return new ArrayList<>(activeNames);
    }
}
