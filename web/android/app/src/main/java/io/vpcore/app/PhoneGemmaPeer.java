package io.vpcore.app;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Looper;
import android.os.Message;
import android.os.Messenger;
import android.os.RemoteException;
import android.os.SystemClock;
import android.util.Log;

import org.json.JSONArray;

import java.io.IOException;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Gemma соседнего приложения. Приложение ядра (io.vpcore.app) и Арродес
 * (dev.arrodes.app) собраны из одного нативного кода, и Gemma (2,6 ГБ) может
 * быть у обоих. Две копии на телефоне и модель в двух процессах ни к чему:
 * считает одно приложение, другое подключается к нему (PhoneGemmaService,
 * Messenger) — слова ответа по мере генерации, прерывание, прогрев.
 *
 * Кто считает — правило, одинаковое для обоих (PhoneGemma.status):
 * приложение ядра, если у него есть модель; иначе Арродес, если есть у него.
 * Соседа нет, он не подписан тем же ключом, его процесс не запустился
 * (Honor/MagicOS «Запуск приложений» не даёт другим приложениям его будить)
 * или умер — своя модель, если есть, иначе «модели нет» с причиной.
 *
 * Доверие — только приложению с той же подписью: проверяют обе стороны
 * (здесь — пакет соседа до подключения и отправителя каждого ответа, служба —
 * отправителя каждого запроса). Отладочные сборки обоих подписаны одним
 * ~/.android/debug.keystore; выпускные тоже должны подписываться одним
 * ключом, иначе соседа не видно и каждое приложение считает своей моделью.
 * Своего разрешения (permission) нет нарочно: одно и то же разрешение в двух
 * пакетах с разными ключами Android не даёт установить.
 *
 * Связь держится, пока соседом пользуются (HOLD_CHAT_MS после ответа,
 * HOLD_STATUS_MS после вопроса о модели), потом отпускается — процесс
 * соседа система может выгрузить. Все вызовы блокирующие и не с главного
 * потока: ответы ServiceConnection приходят на главный.
 */
final class PhoneGemmaPeer {

    private static final String TAG = "OfflineLlm";

    /** Пакет приложения ядра: модель у него — считает оно. */
    static final String PRIMARY = "io.vpcore.app";
    static final String SECONDARY = "dev.arrodes.app";

    // Запросы службе (what) и её ответы; arg1 — номер запроса
    static final int MSG_STATUS = 1;
    static final int MSG_CHAT = 2;
    static final int MSG_WARM = 3;
    static final int MSG_STOP = 4;
    static final int MSG_UNLOAD = 5;
    static final int MSG_TOKEN = 101;
    static final int MSG_DONE = 102;
    static final int MSG_ERROR = 103;
    static final int PROTOCOL = 1;

    // Процесс соседа поднимается до стольких мс (холодный старт ~1 с)
    private static final long BIND_TIMEOUT_MS = 8_000L;
    private static final long STATUS_TIMEOUT_MS = 5_000L;
    // Ответ: загрузка модели (до минуты на зависшем видеоядре), очередь и
    // генерация (LOCAL_TIMEOUT_MS у соседа — 4 мин) — с запасом
    private static final long CHAT_TIMEOUT_MS = 10 * 60_000L;
    private static final long WARM_TIMEOUT_MS = 3 * 60_000L;
    private static final long UNLOAD_TIMEOUT_MS = 30_000L;
    // Ответ о модели соседа помнится столько (его спрашивают каждые 25–30 с)
    private static final long STATUS_TTL_MS = 60_000L;
    private static final long HOLD_STATUS_MS = 20_000L;
    // Модель соседа сама выгружается после 5 мин простоя — связь чуть дольше
    private static final long HOLD_CHAT_MS = 6 * 60_000L;
    private static final long IDLE_CHECK_MS = 15_000L;

    /** Сосед недоступен (связи нет, а не ошибка модели): можно считать своей. */
    static final class Unavailable extends IOException {
        final String reason;
        // Слова ответа уже пошли — своей моделью заново не начать
        final boolean started;

        Unavailable(String reason, boolean started) {
            super("peer_unavailable (" + reason + ")");
            this.reason = reason;
            this.started = started;
        }
    }

    /** Что сосед сказал о своей модели. reachable=false — reason почему. */
    static final class Status {
        final boolean reachable;
        final String reason; // not_installed | untrusted | unreachable | died | timeout | no_model
        final boolean downloaded;
        final long size;
        final boolean loaded;
        final String backend;
        final boolean busy; // сосед считает ответ (свой или чужой) — PhoneGemma.busy()

        Status(boolean reachable, String reason, boolean downloaded, long size, boolean loaded, String backend) {
            this(reachable, reason, downloaded, size, loaded, backend, false);
        }

        Status(boolean reachable, String reason, boolean downloaded, long size, boolean loaded, String backend,
               boolean busy) {
            this.reachable = reachable;
            this.reason = reason;
            this.downloaded = downloaded;
            this.size = size;
            this.loaded = loaded;
            this.backend = backend;
            this.busy = busy;
        }

        boolean serves() {
            return reachable && downloaded;
        }
    }

    private static final class Call {
        final int id;
        final PhoneGemma.Tokens tokens;
        final CountDownLatch done = new CountDownLatch(1);
        volatile Bundle result;
        volatile String error;
        volatile boolean lost;
        volatile boolean started;

        Call(int id, PhoneGemma.Tokens tokens) {
            this.id = id;
            this.tokens = tokens;
        }
    }

    private final Context app;
    private final String pkg;
    private final AtomicInteger ids = new AtomicInteger();
    private final Map<Integer, Call> calls = new ConcurrentHashMap<>();
    private Handler handler;
    private Messenger replies;
    // Связь (под this): conn — bindService вызван, remote — служба ответила
    private ServiceConnection conn;
    private Messenger remote;
    private IBinder remoteBinder;
    private volatile int peerUid = -1;
    private long holdUntil = 0;
    private volatile Status cached;
    private volatile long cachedAt = 0;

    PhoneGemmaPeer(Context app) {
        this.app = app;
        this.pkg = PRIMARY.equals(app.getPackageName()) ? SECONDARY : PRIMARY;
    }

    /** Это приложение ядра: его модель — главная. */
    boolean isPrimary() {
        return PRIMARY.equals(app.getPackageName());
    }

    String packageName() {
        return pkg;
    }

    // ── Связь ───────────────────────────────────────────────────────────

    private synchronized Handler handler() {
        if (handler == null) {
            HandlerThread t = new HandlerThread("vpc-gemma-peer");
            t.start();
            handler = new Handler(t.getLooper(), this::onReply);
            replies = new Messenger(handler);
        }
        return handler;
    }

    /** Сосед установлен и подписан тем же ключом; иначе — причина. */
    private String check() {
        PackageManager pm = app.getPackageManager();
        try {
            peerUid = pm.getApplicationInfo(pkg, 0).uid;
        } catch (PackageManager.NameNotFoundException e) {
            return "not_installed";
        }
        if (pm.checkSignatures(app.getPackageName(), pkg) != PackageManager.SIGNATURE_MATCH) return "untrusted";
        return null;
    }

    private final class Conn implements ServiceConnection {
        @Override
        public void onServiceConnected(ComponentName name, IBinder service) {
            synchronized (PhoneGemmaPeer.this) {
                if (conn != this) return;
                try {
                    // Умер процесс соседа — узнаём сразу, а не когда освободится главный поток
                    service.linkToDeath(() -> lost(this), 0);
                } catch (RemoteException e) {
                    lost(this);
                    return;
                }
                remoteBinder = service;
                remote = new Messenger(service);
                PhoneGemmaPeer.this.notifyAll();
            }
        }

        @Override
        public void onServiceDisconnected(ComponentName name) {
            lost(this);
        }

        @Override
        public void onBindingDied(ComponentName name) {
            lost(this);
        }

        @Override
        public void onNullBinding(ComponentName name) {
            lost(this);
        }
    }

    /** Связь пропала: ждущие запросы — «сосед недоступен», связь — заново при следующем. */
    private void lost(ServiceConnection c) {
        synchronized (this) {
            if (conn != c) return;
            drop();
        }
        for (Call call : calls.values()) {
            call.lost = true;
            call.done.countDown();
        }
        cached = null;
    }

    // под this
    private void drop() {
        ServiceConnection c = conn;
        conn = null;
        remote = null;
        remoteBinder = null;
        if (c != null) {
            try {
                app.unbindService(c);
            } catch (RuntimeException ignored) {
                // уже отвязана
            }
        }
        notifyAll();
    }

    private synchronized Messenger connect() throws Unavailable, InterruptedException {
        if (remote != null && remoteBinder != null && remoteBinder.isBinderAlive()) return remote;
        if (Looper.myLooper() == Looper.getMainLooper()) throw new Unavailable("main_thread", false);
        String why = check();
        if (why != null) throw new Unavailable(why, false);
        handler();
        if (conn == null) {
            Conn c = new Conn();
            Intent i = new Intent().setComponent(new ComponentName(pkg, PhoneGemmaService.class.getName()));
            boolean ok;
            try {
                // BIND_IMPORTANT: пока это приложение на экране, сосед считает
                // с приоритетом переднего плана, а не фонового процесса
                ok = app.bindService(i, c, Context.BIND_AUTO_CREATE | Context.BIND_IMPORTANT);
            } catch (RuntimeException e) {
                Log.i(TAG, "сосед " + pkg + " не подключился: " + e);
                ok = false;
            }
            conn = c;
            if (!ok) {
                drop();
                throw new Unavailable("unreachable", false);
            }
            scheduleIdleCheck();
        }
        long end = SystemClock.elapsedRealtime() + BIND_TIMEOUT_MS;
        while (remote == null && conn != null) {
            long left = end - SystemClock.elapsedRealtime();
            if (left <= 0) break;
            wait(left); // прерван — служба остановлена: не своей моделью, а наверх
        }
        if (remote == null) {
            // Процесс соседа не поднялся: на Honor/MagicOS это «Запуск приложений»
            // (косвенный запуск запрещён) — bindService «прошёл», а службы нет
            Log.i(TAG, "сосед " + pkg + " не отозвался за " + BIND_TIMEOUT_MS + " мс");
            drop();
            throw new Unavailable("unreachable", false);
        }
        return remote;
    }

    private synchronized void hold(long ms) {
        holdUntil = Math.max(holdUntil, SystemClock.elapsedRealtime() + ms);
    }

    private void scheduleIdleCheck() {
        handler().postDelayed(this::idleCheck, IDLE_CHECK_MS);
    }

    // Соседом давно не пользовались — отпустить его процесс
    private void idleCheck() {
        synchronized (this) {
            if (conn == null) return;
            if (!calls.isEmpty() || SystemClock.elapsedRealtime() < holdUntil) {
                scheduleIdleCheck();
                return;
            }
            drop();
        }
    }

    // ── Ответы соседа (поток vpc-gemma-peer) ────────────────────────────

    private boolean onReply(Message msg) {
        // Отвечать сюда может только сосед: Messenger есть только у него
        if (peerUid >= 0 && msg.sendingUid != peerUid) return true;
        Call c = calls.get(msg.arg1);
        if (c == null) return true;
        Bundle data = msg.getData();
        switch (msg.what) {
            case MSG_TOKEN:
                c.started = true;
                if (c.tokens != null) {
                    String chunk = data.getString("text", "");
                    if (!chunk.isEmpty()) {
                        try {
                            c.tokens.onToken(chunk);
                        } catch (RuntimeException e) {
                            Log.w(TAG, "слова ответа соседа не переданы", e);
                        }
                    }
                }
                break;
            case MSG_DONE:
                c.result = data;
                c.done.countDown();
                break;
            case MSG_ERROR:
                c.error = data.getString("error", "failed");
                c.done.countDown();
                break;
            default:
                break;
        }
        return true;
    }

    // ── Запросы ─────────────────────────────────────────────────────────

    private Bundle request(int what, Bundle data, PhoneGemma.Tokens tokens, long timeoutMs, long holdMs)
            throws Exception {
        // Запрос записан до связи: idleCheck не отпустит соседа между connect и send
        Call c = new Call(ids.incrementAndGet(), tokens);
        calls.put(c.id, c);
        hold(holdMs);
        try {
            Messenger m = connect();
            Message msg = Message.obtain(null, what, c.id, 0);
            msg.setData(data);
            msg.replyTo = replies;
            try {
                m.send(msg);
            } catch (RemoteException e) {
                lostAll();
                throw new Unavailable("died", false);
            }
            boolean finished;
            try {
                finished = c.done.await(timeoutMs, TimeUnit.MILLISECONDS);
            } catch (InterruptedException e) {
                sendStop(c.id);
                throw e;
            }
            if (!finished) {
                sendStop(c.id);
                throw new IOException("peer_timeout");
            }
            if (c.lost) throw new Unavailable("died", c.started);
            if (c.error != null) throw new IOException(c.error); // ошибка модели соседа — как своей
            return c.result;
        } finally {
            calls.remove(c.id);
            hold(holdMs);
        }
    }

    private void lostAll() {
        ServiceConnection c;
        synchronized (this) {
            c = conn;
        }
        if (c != null) lost(c);
    }

    private void sendStop(int id) {
        Messenger m;
        synchronized (this) {
            m = remote;
        }
        if (m == null) return;
        try {
            m.send(Message.obtain(null, MSG_STOP, id, 0));
        } catch (RemoteException ignored) {
            // соседа уже нет
        }
    }

    /** Что с моделью у соседа (своей, не его соседа). Ответ помнится STATUS_TTL_MS. */
    Status status(boolean fresh) {
        Status s = cached;
        if (!fresh && s != null && SystemClock.elapsedRealtime() - cachedAt < STATUS_TTL_MS) return s;
        try {
            Bundle b = request(MSG_STATUS, new Bundle(), null, STATUS_TIMEOUT_MS, HOLD_STATUS_MS);
            boolean dl = b != null && b.getBoolean("downloaded", false);
            s = new Status(true, dl ? null : "no_model", dl, b == null ? 0 : b.getLong("size", 0),
                    b != null && b.getBoolean("loaded", false), b == null ? null : b.getString("backend"),
                    b != null && b.getBoolean("busy", false));
        } catch (Unavailable e) {
            s = new Status(false, e.reason, false, 0, false, null);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt(); // служба остановлена — не помнить «недоступен»
            return new Status(false, "interrupted", false, 0, false, null);
        } catch (Exception e) {
            String m = e.getMessage();
            s = new Status(false, "peer_timeout".equals(m) ? "timeout" : "unreachable", false, 0, false, null);
        }
        cached = s;
        cachedAt = SystemClock.elapsedRealtime();
        return s;
    }

    /** Последний известный ответ о модели соседа, без запроса (null — не спрашивали). */
    Status lastStatus() {
        return cached;
    }

    void forget() {
        cached = null;
    }

    // Модель соседа загружена: так и запомнить (статус показывает «в памяти»)
    private void noteLoaded(String backend) {
        Status s = cached;
        if (s != null && s.serves()) {
            cached = new Status(true, null, true, s.size, true, backend);
        }
    }

    PhoneGemma.Reply chat(String system, JSONArray history, String text, int maxTokens,
                          double temperature, double topP, PhoneGemma.Tokens tokens, long deadline) throws Exception {
        Bundle d = new Bundle();
        d.putLong("deadline", deadline);
        d.putString("system", system);
        d.putString("history", history == null ? "[]" : history.toString());
        d.putString("text", text);
        d.putInt("maxTokens", maxTokens);
        d.putDouble("temperature", temperature);
        d.putDouble("topP", topP);
        Bundle r = request(MSG_CHAT, d, tokens, CHAT_TIMEOUT_MS, HOLD_CHAT_MS);
        String backend = r == null ? null : r.getString("backend");
        noteLoaded(backend);
        return new PhoneGemma.Reply(r == null ? "" : r.getString("text", ""), backend);
    }

    void warm() throws Exception {
        Bundle r = request(MSG_WARM, new Bundle(), null, WARM_TIMEOUT_MS, HOLD_CHAT_MS);
        noteLoaded(r == null ? null : r.getString("backend"));
    }

    /** Выгрузить модель соседа — только если связь уже есть (будить его ради этого незачем). */
    void unload() throws Exception {
        synchronized (this) {
            if (remote == null) return;
        }
        request(MSG_UNLOAD, new Bundle(), null, UNLOAD_TIMEOUT_MS, HOLD_STATUS_MS);
        Status s = cached;
        if (s != null && s.serves()) cached = new Status(true, null, true, s.size, false, null);
    }

    /** Прервать свои ответы, которые сейчас считает сосед: вернётся то, что успело. */
    void stopAll() {
        for (Call c : calls.values()) sendStop(c.id);
    }
}
