package io.vpcore.app;

import android.annotation.SuppressLint;
import android.app.AlarmManager;
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
import android.content.pm.ServiceInfo;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;
import android.service.notification.StatusBarNotification;
import android.text.format.DateFormat;
import android.util.Log;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.app.ServiceCompat;
import androidx.core.content.ContextCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.Date;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Фоновые сообщения персон, пока приложение свёрнуто или закрыто.
 *
 * WebView в фоне не работает, а Notification API в нём нет — поэтому ядро
 * (GET /api/inbox — новые сообщения сразу по всем персонам) опрашивает эта
 * служба переднего плана. Опрос долгий: запрос висит на ядре, пока не
 * появится сообщение (или до LONG_WAIT_SEC), и сразу за ответом уходит
 * следующий. Телефон тем временем спит: блокировка сна — только на отправку
 * запроса и разбор ответа, а сам ответ будит телефон приходом по сети
 * (службе переднего плана Android не отключает сеть и в глубоком сне). Так
 * сообщение приходит за секунды, а холостое пробуждение — раз в LONG_WAIT_SEC.
 * Раньше служба спрашивала ядро каждые 20 с, а во сне её будил будильник —
 * не чаще раза в ~10 минут, и сообщения опаздывали на столько же.
 *
 * Ядро присылает курсор (до какого сообщения клиент получил всё) и id своего
 * запуска; служба возвращает их в следующем запросе. Ответ, потерянный по
 * дороге (телефон ушёл из Wi-Fi, пока ждал), ядро отдаст ещё раз.
 *
 * Будильник AlarmManager остаётся: во время долгого опроса — сторож (таймаут
 * чтения во сне не идёт, а соединение может умереть молча — ноутбук уснул),
 * без связи — пауза до следующей попытки (до 60 с; в глубоком сне система
 * будит реже). Ядро старой версии отвечает сразу и без курсора — тогда служба
 * спрашивает его, как раньше, раз в 20 с.
 * Новое сообщение — уведомление «Сообщения персон» (заголовок — имя
 * персоны), тап открывает чат. Пока приложение на экране, уведомлений нет:
 * сообщения показывает сам веб, а опрос идёт дальше — курсор службы
 * двигается, и после сворачивания старое не всплывает.
 *
 * У службы свой client_id (clientId веба + "-bg") — свой курсор на ядре:
 * что забрала служба, веб получит своим опросом как обычно.
 *
 * Адрес, токен и clientId лежат в SharedPreferences: служба переживает
 * выгрузку приложения и перезапуск системой (START_STICKY).
 */
public class BackgroundInboxService extends Service {

    private static final String TAG = "BackgroundInbox";

    static final String PREFS = "vpc_bg_inbox";
    static final String KEY_ENABLED = "enabled";
    static final String KEY_BASE_URL = "baseUrl";
    static final String KEY_TOKEN = "token";
    static final String KEY_CLIENT_ID = "clientId";
    // Курсор долгого опроса и id запуска ядра, к которому он относится
    static final String KEY_CURSOR = "cursor";
    static final String KEY_EPOCH = "epoch";

    // Действие интента службы: остановиться (выключено в приложении)
    static final String ACTION_STOP = "io.vpcore.app.BG_INBOX_STOP";
    // Будильник AlarmManager: пора опросить (рассылка только внутри приложения)
    private static final String ACTION_ALARM = "io.vpcore.app.BG_INBOX_POLL";

    // Экстра интента MainActivity: id персоны из тапнутого уведомления
    static final String EXTRA_PERSONA = "io.vpcore.app.PERSONA";

    private static final String CHANNEL_LINK = "vpc_link";
    private static final String CHANNEL_MESSAGES = "vpc_messages";
    private static final int ONGOING_ID = 1;
    // Метки уведомлений сообщений: по ним их снимают, когда приложение на экране
    private static final String TAG_MESSAGE = "vpc-msg:";
    private static final String TAG_SUMMARY = "vpc-sum:";
    private static final String TAG_TEST = "vpc-test";
    private static final String GROUP_PREFIX = "io.vpcore.app.persona.";

    // Ожидание ответа на ядре (там же предел — MAX_WAIT_SEC в app/api/inbox.py)
    private static final int LONG_WAIT_SEC = 300;
    // Таймаут чтения — с запасом сверх ожидания на ядре
    private static final int READ_TIMEOUT_MS = (LONG_WAIT_SEC + 30) * 1000;
    // Сторож: столько ответа нет — соединение мертво (во сне таймаут чтения стоит)
    private static final long WATCHDOG_MS = (LONG_WAIT_SEC + 60) * 1000L;
    // Ядро без долгого опроса (старая версия) — спрашиваем, как раньше
    private static final long POLL_MS = 20_000;
    private static final long MAX_BACKOFF_MS = 60_000;
    // Блокировка сна на отправку запроса: дальше ответ ждём без неё
    private static final long SEND_WAKE_MS = 3_000;
    // Блокировка сна на разбор ответа или обычный опрос: соединение 10 с + чтение 15 с + запас
    private static final long WAKE_MS = 40_000;

    // Приложение на экране (MainActivity onResume/onPause) — уведомлений не показываем
    static volatile boolean appVisible = false;

    // Состояние для status() плагина
    static volatile boolean running = false;
    static volatile long lastOk = 0;
    static volatile String lastError = null;
    // Запуск запрошен (startForegroundService), а onStartCommand ещё не было —
    // плагину stop() тогда нельзя просто stopService (см. stop())
    static volatile boolean startPending = false;

    private ScheduledExecutorService executor;
    private ScheduledFuture<?> next;
    private PowerManager.WakeLock wakeLock;
    private AlarmManager alarms;
    private PendingIntent alarmIntent;
    private BroadcastReceiver alarmReceiver;
    // Служба остановлена: запрос, который ещё идёт (HttpURLConnection на
    // прерывание потока не реагирует), не должен потом ничего показать.
    // Проверка и notify — под NOTIFY_LOCK, onDestroy ставит флаг под ним же
    private volatile boolean destroyed = false;
    private static final Object NOTIFY_LOCK = new Object();
    // Остановка началась (stop() из приложения или halt()) — значок больше не
    // обновляем. Ставится под NOTIFY_LOCK ДО stopForeground/stopService: notify
    // с тем же id после них Android 12+ считает «новее» отмены и отмену
    // пропускает, а уведомление наследует флаг службы переднего плана —
    // приложение его уже не снимет, и в шторке навсегда висит «Подключено»
    private static boolean stopping = false;
    // Текущий запрос — onDestroy, сторож и перезапуск его обрывают
    private volatile HttpURLConnection current;
    // Идёт запрос (у долгого опроса — минуты): будильник тогда — сторож
    private volatile boolean waiting = false;
    private volatile long waitDeadline = 0;
    // Сторож оборвал молчащее соединение — это сбой связи
    private volatile boolean watchdogFired = false;
    // Пришли новые настройки (onStartCommand): опрос обрывается не как сбой,
    // следующий запустит задача с новыми настройками
    private volatile boolean restarting = false;
    // Ядро уже ответило после запуска или сбоя. До того запрос — без
    // ожидания: значок «Подключено» и курсор появляются сразу, а не через минуты
    private boolean primed = false;
    // Когда должен быть следующий опрос (elapsedRealtime): опоздавший
    // будильник после свежего опроса таймером пропускаем
    private volatile long dueAt = 0;

    // Настройки текущего запуска (меняются только в потоке executor)
    private String baseUrl = "";
    private String token = "";
    private String clientId = "";
    private long cursor = -1; // -1 — курсора нет: ядро начнёт со своего
    private String epoch = "";
    private long backoffMs = POLL_MS;
    // Текст значка в шторке: обновляем, только когда он меняется
    private String shownStatus = null;
    // Номера уведомлений сообщений: от времени запуска — после перезапуска
    // службы новые не перетирают ещё висящие в шторке
    private int messageSeq = (int) ((System.currentTimeMillis() / 1000) & 0x3fffffff);

    /** Запустить (или перечитать настройки и опросить сразу). */
    static void start(Context ctx) {
        Intent i = new Intent(ctx, BackgroundInboxService.class);
        startPending = true;
        try {
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i);
            else ctx.startService(i);
        } catch (RuntimeException e) {
            startPending = false;
            throw e;
        }
    }

    /**
     * Остановить. Не stopService: если startForegroundService уже вызван, а
     * onStartCommand ещё не было, остановка до startForeground роняет
     * приложение (Android 9+). Поэтому — команда ACTION_STOP: служба сама
     * сделает startForeground и тут же уйдёт.
     */
    static void stop(Context ctx) {
        if (!running && !startPending) return; // и так не работает
        synchronized (NOTIFY_LOCK) {
            stopping = true;
        }
        try {
            ctx.startService(new Intent(ctx, BackgroundInboxService.class).setAction(ACTION_STOP));
        } catch (RuntimeException e) {
            // Приложение в фоне (Android 8+ не даёт startService) — запуска в
            // полёте тогда быть не может, обычная остановка безопасна
            ctx.stopService(new Intent(ctx, BackgroundInboxService.class));
        }
    }

    static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createChannels(this);
        executor = Executors.newSingleThreadScheduledExecutor();
        // Частичная блокировка сна (только процессор, не экран) — лишь на время
        // запроса: таймер executor стоит, пока телефон спит, а запрос, начатый
        // перед сном, без неё замрёт на середине
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "vpc:background-inbox");
            wakeLock.setReferenceCounted(false);
        }
        // Уснувший телефон будит будильник. Рассылка — приёмнику, а не
        // запуск службы: пока идёт onReceive, AlarmManager держит процессор,
        // и мы успеваем взять свою блокировку
        alarms = (AlarmManager) getSystemService(Context.ALARM_SERVICE);
        alarmIntent = PendingIntent.getBroadcast(this, 0,
                new Intent(ACTION_ALARM).setPackage(getPackageName()),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        alarmReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                if (destroyed) return;
                if (waiting) {
                    // Идёт долгий опрос — будильник здесь сторож. Ответа нет
                    // дольше положенного: соединение умерло молча (ноутбук
                    // уснул, телефон сменил сеть) — обрываем, опрос повторится
                    if (SystemClock.elapsedRealtime() >= waitDeadline) {
                        holdAwake(WAKE_MS);
                        watchdogFired = true;
                        abortCurrent();
                    } else {
                        setAlarm(waitDeadline);
                    }
                    return;
                }
                holdAwake(WAKE_MS);
                try {
                    executor.execute(() -> {
                        // Запускаем опрос, который ждёт своего часа и дождался
                        // (таймер executor во сне стоит). Его нет — он уже
                        // прошёл; час не настал — таймер успел раньше
                        if (next == null || next.isDone() || SystemClock.elapsedRealtime() + 2_000 < dueAt) {
                            releaseAwake();
                            return;
                        }
                        next.cancel(false);
                        pollOnce();
                    });
                } catch (RuntimeException e) {
                    releaseAwake(); // executor уже остановлен
                }
            }
        };
        ContextCompat.registerReceiver(this, alarmReceiver, new IntentFilter(ACTION_ALARM),
                ContextCompat.RECEIVER_NOT_EXPORTED);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startPending = false;
        SharedPreferences p = prefs(this);
        final String url = p.getString(KEY_BASE_URL, "");
        boolean stopAsked = intent != null && ACTION_STOP.equals(intent.getAction());
        boolean want = !stopAsked && p.getBoolean(KEY_ENABLED, false) && !url.isEmpty();
        // startForeground — сразу и ВСЕГДА, даже если сейчас остановимся:
        // после startForegroundService остановка без него роняет приложение
        // (а запуск мог быть в полёте, когда пользователь выключил)
        try {
            Notification n = ongoingNotification(getString(R.string.bg_connecting, hostOf(url)), want);
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(ONGOING_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else {
                startForeground(ONGOING_ID, n);
            }
        } catch (RuntimeException e) {
            // Android 12+ запрещает запуск службы переднего плана из фона
            // (например, перезапуск системой в неудачный момент) — следующий
            // запуск приложения включит её снова
            Log.w(TAG, "startForeground не удался", e);
            if (want) lastError = "start: " + e.getClass().getSimpleName();
            halt(startId);
            return START_NOT_STICKY;
        }
        if (!want) {
            // Выключено в приложении (или перезапуск системой после stop) — не держимся
            halt(startId);
            return START_NOT_STICKY;
        }
        running = true;
        synchronized (NOTIFY_LOCK) {
            stopping = false; // новый запуск после остановки
        }
        final String tok = p.getString(KEY_TOKEN, "");
        final String cid = p.getString(KEY_CLIENT_ID, "");
        final long cur = p.getLong(KEY_CURSOR, -1);
        final String ep = p.getString(KEY_EPOCH, "");
        // Долгий опрос держит поток executor до ответа (минуты) — обрываем его,
        // чтобы новые адрес и токен вступили в силу сразу
        restarting = true;
        abortCurrent();
        executor.execute(() -> {
            restarting = false;
            baseUrl = url.replaceAll("/+$", "");
            token = tok;
            clientId = cid;
            cursor = cur;
            epoch = ep;
            primed = false;
            shownStatus = null;
            backoffMs = POLL_MS;
            schedule(0);
        });
        return START_STICKY;
    }

    /** Остановиться по команде startId. stopSelf(startId), а не stopSelf():
     * если за ней уже пришёл новый запуск, служба доживёт до его команды. */
    private void halt(int startId) {
        if (executor != null && !executor.isShutdown()) {
            try {
                executor.execute(() -> {
                    if (next != null) next.cancel(false);
                });
            } catch (RuntimeException ignored) {
                // executor остановлен — опросов и так не будет
            }
        }
        if (alarms != null) alarms.cancel(alarmIntent);
        synchronized (NOTIFY_LOCK) {
            stopping = true;
        }
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
        stopSelf(startId);
    }

    @Override
    public void onDestroy() {
        synchronized (NOTIFY_LOCK) {
            destroyed = true;
            // Значок «связь с ядром» больше не наш — убрать, чтобы не остался
            // неснимаемым уведомлением
            NotificationManagerCompat.from(this).cancel(ONGOING_ID);
        }
        running = false;
        if (alarms != null) alarms.cancel(alarmIntent);
        if (alarmReceiver != null) {
            try {
                unregisterReceiver(alarmReceiver);
            } catch (RuntimeException ignored) {
                // не зарегистрирован
            }
        }
        if (executor != null) executor.shutdownNow();
        abortCurrent();
        releaseAwake();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ── Опрос ─────────────────────────────────────────────────────────

    /** Следующий опрос через delayMs: таймер executor (точный, пока процессор
     * не спит) и будильник (разбудит уснувший телефон) — что раньше. */
    private void schedule(long delayMs) {
        if (next != null) next.cancel(false);
        if (destroyed || executor.isShutdown()) return;
        dueAt = SystemClock.elapsedRealtime() + delayMs;
        next = executor.schedule(this::pollOnce, delayMs, TimeUnit.MILLISECONDS);
        setAlarm(dueAt);
    }

    // Точный будильник — только после проверки canScheduleExactAlarms (или
    // до Android 12, где разрешения нет): lint этой проверки не видит
    @SuppressLint("MissingPermission")
    private void setAlarm(long at) {
        if (alarms == null) return;
        try {
            // Точный будильник — только где он разрешён без особого
            // разрешения (до Android 12); иначе система сдвигает его сама
            if (Build.VERSION.SDK_INT < 31 || alarms.canScheduleExactAlarms()) {
                alarms.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, alarmIntent);
            } else {
                alarms.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, alarmIntent);
            }
        } catch (RuntimeException e) {
            Log.w(TAG, "будильник не поставлен", e);
        }
    }

    /** Не держать процессор дольше ms (повторный вызов — новый срок). */
    private void holdAwake(long ms) {
        if (wakeLock != null) wakeLock.acquire(ms);
    }

    /** Оборвать идущий запрос: прерывание потока HttpURLConnection не слышит,
     * а disconnect может ждать сеть — не в вызывающем потоке. */
    private void abortCurrent() {
        HttpURLConnection c = current;
        if (c != null) new Thread(c::disconnect).start();
    }

    private void releaseAwake() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        } catch (RuntimeException ignored) {
            // тайм-аут уже снял блокировку
        }
    }

    private void pollOnce() {
        if (destroyed || restarting) return;
        // Блокировка сна — на отправку запроса; ответа долгий опрос ждёт без
        // неё: телефон спит, а пришедший ответ его будит
        holdAwake(SEND_WAKE_MS);
        boolean keepAwake = false;
        try {
            Reply reply = null;
            String err = null;
            try {
                reply = fetchInbox();
            } catch (Exception e) {
                err = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
            }
            holdAwake(WAKE_MS); // ответ (или обрыв) пришёл — разобрать, не засыпая
            boolean watchdog = watchdogFired;
            watchdogFired = false;
            // Пока шёл запрос, службу остановили — ничего не показываем
            if (destroyed || executor.isShutdown()) return;
            if (err == null) {
                lastOk = System.currentTimeMillis();
                lastError = null;
                backoffMs = POLL_MS;
                for (int i = 0; i < reply.messages.length(); i++) {
                    JSONObject m = reply.messages.optJSONObject(i);
                    if (m != null && !appVisible) showMessage(m);
                }
                // Курсор — после показа: если процесс умрёт посередине, ответ
                // придёт ещё раз (повтор уведомления лучше пропажи)
                if (reply.longPoll) saveCursor(reply.cursor, reply.epoch);
                primed = true;
                showStatus(true);
                if (restarting) return; // новые настройки — опрос запустит их задача
                // Долгий опрос — следующий сразу (блокировку сна не снимаем:
                // его отправка тут же возьмёт свою); ядро старой версии
                // отвечает без ожидания — тогда раз в POLL_MS
                keepAwake = reply.longPoll;
                schedule(reply.longPoll ? 0 : POLL_MS);
            } else {
                if (restarting) return; // оборвали ради новых настроек — не сбой
                lastError = watchdog ? "timeout" : err;
                primed = false;
                showStatus(false);
                // Нет связи — пауза растёт до минуты: ноутбук спит или телефон вне дома
                backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
                schedule(backoffMs);
            }
        } finally {
            if (!keepAwake) releaseAwake();
        }
    }

    /** Ответ ядра: сообщения и, у долгого опроса, курсор с id запуска ядра. */
    private static final class Reply {
        JSONArray messages = new JSONArray();
        boolean longPoll = false;
        long cursor = -1;
        String epoch = "";
    }

    private Reply fetchInbox() throws Exception {
        // epoch — всегда (пустой — курсора ещё нет): по нему ядро понимает,
        // что клиент умеет долгий опрос, и отвечает с курсором
        StringBuilder q = new StringBuilder("?chat_id=web_user&client_id=")
                .append(URLEncoder.encode(clientId + "-bg", "UTF-8"))
                .append("&wait=").append(primed ? LONG_WAIT_SEC : 0)
                .append("&epoch=").append(URLEncoder.encode(epoch, "UTF-8"));
        if (cursor >= 0 && !epoch.isEmpty()) q.append("&since=").append(cursor);
        HttpURLConnection c = (HttpURLConnection) new URL(baseUrl + "/api/inbox" + q).openConnection();
        current = c;
        try {
            c.setConnectTimeout(10_000);
            c.setReadTimeout(READ_TIMEOUT_MS);
            c.setUseCaches(false);
            c.setRequestProperty("Accept", "application/json");
            if (!token.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + token);
            waitDeadline = SystemClock.elapsedRealtime() + WATCHDOG_MS;
            waiting = true;
            setAlarm(waitDeadline);
            int code = c.getResponseCode();
            if (code != 200) throw new Exception("HTTP " + code);
            String t = readAll(c.getInputStream()).trim();
            Reply r = new Reply();
            if (t.startsWith("{")) {
                JSONObject o = new JSONObject(t);
                JSONArray arr = o.optJSONArray("messages");
                if (arr != null) r.messages = arr;
                if (o.has("cursor")) {
                    r.longPoll = true;
                    r.cursor = o.optLong("cursor", -1);
                    r.epoch = o.optString("epoch", "");
                }
            } else {
                r.messages = new JSONArray(t); // ядро старой версии — просто список
            }
            return r;
        } finally {
            waiting = false;
            current = null;
            c.disconnect();
        }
    }

    private void saveCursor(long cur, String ep) {
        if (cur < 0) return;
        String e = ep == null ? "" : ep;
        if (cur == cursor && e.equals(epoch)) return;
        cursor = cur;
        epoch = e;
        prefs(this).edit().putLong(KEY_CURSOR, cur).putString(KEY_EPOCH, e).apply();
    }

    private static String readAll(InputStream in) throws Exception {
        try (InputStream s = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = s.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString("UTF-8");
        }
    }

    // ── Уведомления ───────────────────────────────────────────────────

    /** Значок в шторке: «Подключено к … · ответ ядра в 23:58» — по времени
     * видно, жива ли связь (долгий опрос отвечает не реже раза в
     * LONG_WAIT_SEC), — или «нет связи». */
    private void showStatus(boolean ok) {
        String text = ok
                ? getString(R.string.bg_connected_at, hostOf(baseUrl),
                        DateFormat.getTimeFormat(this).format(new Date(lastOk)))
                : getString(R.string.bg_offline);
        if (text.equals(shownStatus)) return;
        shownStatus = text;
        synchronized (NOTIFY_LOCK) {
            if (destroyed || stopping) return;
            try {
                NotificationManagerCompat.from(this).notify(ONGOING_ID, ongoingNotification(text, true));
            } catch (SecurityException e) {
                // Нет разрешения на уведомления — служба работает без значка в шторке
            }
        }
    }

    /** immediate=false — значок для мгновенной остановки: система его
     * обычно и не успевает показать. */
    private Notification ongoingNotification(String text, boolean immediate) {
        Intent open = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new NotificationCompat.Builder(this, CHANNEL_LINK)
                .setSmallIcon(R.drawable.ic_stat_vpc)
                .setContentTitle(getString(R.string.bg_ongoing_title))
                .setContentText(text)
                .setContentIntent(pi)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setShowWhen(false)
                .setSilent(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .setCategory(NotificationCompat.CATEGORY_SERVICE)
                .setForegroundServiceBehavior(immediate
                        ? NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE
                        : NotificationCompat.FOREGROUND_SERVICE_DEFERRED)
                .build();
    }

    private void showMessage(JSONObject m) {
        String persona = m.optString("persona", "");
        if (persona.isEmpty()) return;
        String name = m.optString("name", persona);
        String text = m.optString("text", "");
        long when = (long) (m.optDouble("ts", System.currentTimeMillis() / 1000.0) * 1000);

        // Тап — MainActivity с id персоны (requestCode по персоне: у каждой
        // свой PendingIntent, экстра не перетирается чужой)
        Intent open = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra(EXTRA_PERSONA, persona);
        PendingIntent pi = PendingIntent.getActivity(this, persona.hashCode(), open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        String group = GROUP_PREFIX + persona;

        Notification msg = new NotificationCompat.Builder(this, CHANNEL_MESSAGES)
                .setSmallIcon(R.drawable.ic_stat_vpc)
                .setContentTitle(name)
                .setContentText(text)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(text))
                .setWhen(when)
                .setShowWhen(true)
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setGroup(group)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                .build();
        // Сводка группы: сообщения одной персоны — одной стопкой в шторке
        Notification summary = new NotificationCompat.Builder(this, CHANNEL_MESSAGES)
                .setSmallIcon(R.drawable.ic_stat_vpc)
                .setContentTitle(name)
                .setContentText(text)
                .setWhen(when)
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setGroup(group)
                .setGroupSummary(true)
                .setGroupAlertBehavior(NotificationCompat.GROUP_ALERT_CHILDREN)
                .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                .build();
        synchronized (NOTIFY_LOCK) {
            if (destroyed || stopping) return; // уведомления выключили, пока шёл запрос
            try {
                NotificationManagerCompat nm = NotificationManagerCompat.from(this);
                nm.notify(TAG_MESSAGE + persona, ++messageSeq, msg);
                nm.notify(TAG_SUMMARY + persona, 0, summary);
            } catch (SecurityException e) {
                // Разрешение на уведомления отозвано — молча пропускаем
            }
        }
    }

    /** «Проверить» в настройках приложения: уведомление в канал «Сообщения
     * персон» — тем же путём, что настоящее. → null — показано, иначе
     * причина: "denied" — уведомления приложению запрещены, "channel" —
     * выключен канал «Сообщения персон». */
    static String showTest(Context ctx) {
        createChannels(ctx);
        NotificationManagerCompat nm = NotificationManagerCompat.from(ctx);
        if (!nm.areNotificationsEnabled()) return "denied";
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager sys = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
            NotificationChannel ch = sys == null ? null : sys.getNotificationChannel(CHANNEL_MESSAGES);
            if (ch != null && ch.getImportance() == NotificationManager.IMPORTANCE_NONE) return "channel";
        }
        Intent open = new Intent(ctx, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(ctx, 0x7e57, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification n = new NotificationCompat.Builder(ctx, CHANNEL_MESSAGES)
                .setSmallIcon(R.drawable.ic_stat_vpc)
                .setContentTitle(ctx.getString(R.string.bg_test_title))
                .setContentText(ctx.getString(R.string.bg_test_text))
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                .build();
        try {
            nm.notify(TAG_TEST, 0, n);
        } catch (SecurityException e) {
            return "denied";
        }
        return null;
    }

    /** Приложение вышло на экран: сообщения из шторки показывает уже сам чат. */
    static void clearMessageNotifications(Context ctx) {
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        try {
            for (StatusBarNotification sbn : nm.getActiveNotifications()) {
                String tag = sbn.getTag();
                if (tag != null && (tag.startsWith(TAG_MESSAGE) || tag.startsWith(TAG_SUMMARY))) {
                    nm.cancel(tag, sbn.getId());
                }
            }
        } catch (RuntimeException e) {
            // не критично
        }
    }

    static void createChannels(Context ctx) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        NotificationChannel link = new NotificationChannel(CHANNEL_LINK,
                ctx.getString(R.string.bg_channel_link), NotificationManager.IMPORTANCE_LOW);
        link.setDescription(ctx.getString(R.string.bg_channel_link_desc));
        link.setShowBadge(false);
        NotificationChannel messages = new NotificationChannel(CHANNEL_MESSAGES,
                ctx.getString(R.string.bg_channel_messages), NotificationManager.IMPORTANCE_HIGH);
        messages.setDescription(ctx.getString(R.string.bg_channel_messages_desc));
        nm.createNotificationChannel(link);
        nm.createNotificationChannel(messages);
    }

    private static String hostOf(String url) {
        try {
            Uri u = Uri.parse(url);
            String host = u.getHost();
            if (host == null) return url;
            return u.getPort() > 0 ? host + ":" + u.getPort() : host;
        } catch (RuntimeException e) {
            return url;
        }
    }
}
