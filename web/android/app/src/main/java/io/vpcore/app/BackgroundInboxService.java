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
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Фоновые сообщения персон, пока приложение свёрнуто или закрыто.
 *
 * WebView в фоне не работает, а Notification API в нём нет — поэтому опрос
 * ядра (GET /api/inbox — новые сообщения сразу по всем персонам) делает
 * эта служба переднего плана: каждые 20 с, без связи — с паузой до 60 с.
 * Процессор постоянно не держим: пока он не спит, срабатывает таймер
 * executor, а уснувший телефон будит будильник AlarmManager; блокировка
 * сна — только на время одного запроса (с тайм-аутом). В глубоком сне
 * (Doze) система будит не чаще раза в несколько минут — это плата за
 * батарею.
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
    private static final String GROUP_PREFIX = "io.vpcore.app.persona.";

    private static final long POLL_MS = 20_000;
    private static final long MAX_BACKOFF_MS = 60_000;
    // Блокировка сна на один опрос: соединение 10 с + чтение 15 с + запас
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
    // Проверка и notify — под notifyLock, onDestroy ставит флаг под ним же
    private volatile boolean destroyed = false;
    private final Object notifyLock = new Object();
    // Текущий запрос — onDestroy его обрывает
    private volatile HttpURLConnection current;
    // Когда должен быть следующий опрос (elapsedRealtime): опоздавший
    // будильник после свежего опроса таймером пропускаем
    private volatile long dueAt = 0;

    // Настройки текущего запуска (меняются только в потоке executor)
    private String baseUrl = "";
    private String token = "";
    private String clientId = "";
    private long backoffMs = POLL_MS;
    private Boolean connected = null; // null — ещё не опрашивали
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
                holdAwake();
                try {
                    executor.execute(() -> {
                        // Таймер успел раньше (телефон не спал) — этот лишний
                        if (SystemClock.elapsedRealtime() + 2_000 < dueAt) {
                            releaseAwake();
                            return;
                        }
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
        final String tok = p.getString(KEY_TOKEN, "");
        final String cid = p.getString(KEY_CLIENT_ID, "");
        executor.execute(() -> {
            baseUrl = url.replaceAll("/+$", "");
            token = tok;
            clientId = cid;
            connected = null;
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
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
        stopSelf(startId);
    }

    @Override
    public void onDestroy() {
        synchronized (notifyLock) {
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
        // Идущий запрос — оборвать: прерывание потока HttpURLConnection не слышит
        HttpURLConnection c = current;
        if (c != null) {
            new Thread(c::disconnect).start(); // disconnect может ждать сеть — не в главном потоке
        }
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

    private void holdAwake() {
        if (wakeLock != null) wakeLock.acquire(WAKE_MS);
    }

    private void releaseAwake() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        } catch (RuntimeException ignored) {
            // тайм-аут уже снял блокировку
        }
    }

    private void pollOnce() {
        if (destroyed) return;
        holdAwake();
        try {
            JSONArray items = null;
            String err = null;
            try {
                items = fetchInbox();
            } catch (Exception e) {
                err = e.getMessage() != null ? e.getMessage() : e.getClass().getSimpleName();
            }
            // Пока шёл запрос, службу остановили — ничего не показываем
            if (destroyed || executor.isShutdown()) return;
            if (err == null) {
                lastOk = System.currentTimeMillis();
                lastError = null;
                backoffMs = POLL_MS;
                setConnected(true);
                for (int i = 0; i < items.length(); i++) {
                    JSONObject m = items.optJSONObject(i);
                    if (m != null && !appVisible) showMessage(m);
                }
            } else {
                lastError = err;
                setConnected(false);
                // Нет связи — пауза растёт до минуты: ноутбук спит или телефон вне дома
                backoffMs = Math.min(backoffMs * 2, MAX_BACKOFF_MS);
            }
            schedule(err == null ? POLL_MS : backoffMs);
        } finally {
            releaseAwake();
        }
    }

    private JSONArray fetchInbox() throws Exception {
        String q = "?chat_id=web_user&client_id=" + URLEncoder.encode(clientId + "-bg", "UTF-8");
        HttpURLConnection c = (HttpURLConnection) new URL(baseUrl + "/api/inbox" + q).openConnection();
        current = c;
        try {
            c.setConnectTimeout(10_000);
            c.setReadTimeout(15_000);
            c.setUseCaches(false);
            c.setRequestProperty("Accept", "application/json");
            if (!token.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + token);
            int code = c.getResponseCode();
            if (code != 200) throw new Exception("HTTP " + code);
            String body = readAll(c.getInputStream());
            // Ответ — список; на будущее терпим и {"messages": [...]}
            String t = body.trim();
            if (t.startsWith("{")) {
                JSONArray arr = new JSONObject(t).optJSONArray("messages");
                return arr != null ? arr : new JSONArray();
            }
            return new JSONArray(t);
        } finally {
            current = null;
            c.disconnect();
        }
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

    private void setConnected(boolean ok) {
        if (connected != null && connected == ok) return;
        connected = ok;
        String text = ok ? getString(R.string.bg_connected, hostOf(baseUrl))
                : getString(R.string.bg_offline);
        synchronized (notifyLock) {
            if (destroyed) return;
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
        synchronized (notifyLock) {
            if (destroyed) return; // уведомления выключили, пока шёл запрос
            try {
                NotificationManagerCompat nm = NotificationManagerCompat.from(this);
                nm.notify(TAG_MESSAGE + persona, ++messageSeq, msg);
                nm.notify(TAG_SUMMARY + persona, 0, summary);
            } catch (SecurityException e) {
                // Разрешение на уведомления отозвано — молча пропускаем
            }
        }
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
