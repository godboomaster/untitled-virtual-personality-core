package io.vpcore.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.util.Log;
import com.google.ai.edge.litertlm.Backend;
import com.google.ai.edge.litertlm.Content;
import com.google.ai.edge.litertlm.Contents;
import com.google.ai.edge.litertlm.Conversation;
import com.google.ai.edge.litertlm.ConversationConfig;
import com.google.ai.edge.litertlm.Engine;
import com.google.ai.edge.litertlm.EngineConfig;
import com.google.ai.edge.litertlm.Message;
import com.google.ai.edge.litertlm.MessageCallback;
import com.google.ai.edge.litertlm.SamplerConfig;
import com.google.ai.edge.litertlm.ThinkingConfig;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Gemma на телефоне (Gemma 4 E2B, LiteRT-LM) — одна на процесс. Её считают
 * приложение (плагин OfflineLlm: ответы без ноутбука, задачи Gemma ядра, пока
 * приложение на экране) и фоновая служба (BackgroundInboxService: задачи
 * Gemma ядра, пока приложение свёрнуто или закрыто). Раньше модель жила в
 * плагине и умирала вместе с экраном; общая — не грузится дважды и не
 * занимает память дважды.
 *
 * Модель — строго по одному: загрузка, генерация, выгрузка в одном потоке
 * llm. Сначала видеоядро, не вышло — процессор; неудача видеоядра
 * помнится сутки (GPU_RETRY_MS), потом оно пробуется снова: одного сбоя
 * хватало, чтобы телефон навсегда считал на процессоре вдвое медленнее,
 * хотя видеоядро исправно. Простоя IDLE_UNLOAD_MS — модель выгружается.
 *
 * Одна и на два приложения: у приложения ядра и Арродеса один нативный код,
 * и модель, скачанная в одном, нужна и другому. Кто считает — status():
 * приложение ядра, если у него есть модель, иначе Арродес, если есть у
 * него; другое подключается к нему (PhoneGemmaPeer → PhoneGemmaService).
 * Плагин и фоновая служба этого не замечают: downloaded/chat/warm/stop те
 * же. Своя модель — *Own (их же зовёт служба для соседа).
 */
final class PhoneGemma {

    private static final String TAG = "OfflineLlm";
    static final String PREFS = "vpc_offline";
    // До 19.10 неудача видеоядра помнилась навсегда (флаг), теперь — срок
    private static final String KEY_GPU_BAD_LEGACY = "gpu_bad";
    private static final String KEY_GPU_BAD_UNTIL = "gpu_bad_until";
    private static final long GPU_RETRY_MS = 24 * 3600_000L;

    static final String MODEL_URL =
            "https://huggingface.co/litert-community/gemma-4-E2B-it-litert-lm/resolve/main/gemma-4-E2B-it.litertlm";
    static final String MODEL_FILE = "gemma-4-E2B-it.litertlm";
    // Начало любого файла .litertlm: недокачанный или чужой файл не берём
    private static final byte[] MODEL_MAGIC = "LITERTLM".getBytes(StandardCharsets.US_ASCII);
    // Простоя столько — модель выгружается (≈1,7 ГБ памяти на процессоре)
    private static final long IDLE_UNLOAD_MS = 5 * 60_000L;
    private static final int MAX_CONTEXT_TOKENS = 4096;
    // Ответ модели телефона дольше — прерывается; после «стоп» ждём не дольше
    private static final long LOCAL_TIMEOUT_MS = 4 * 60_000L;
    private static final long CANCEL_GRACE_MS = 5_000L;
    // На видеоядре за столько нет первого слова — видеоядро зависло (на
    // эмуляторе без OpenCL оно уходит в медленную эмуляцию): дальше процессор
    private static final long GPU_FIRST_TOKEN_MS = 60_000L;

    /** Слова ответа по мере генерации. */
    interface Tokens {
        void onToken(String chunk);
    }

    static final class Reply {
        final String text;
        final String backend;

        Reply(String text, String backend) {
            this.text = text;
            this.backend = backend;
        }
    }

    private static PhoneGemma instance;

    static synchronized PhoneGemma get(Context ctx) {
        if (instance == null) instance = new PhoneGemma(ctx.getApplicationContext());
        return instance;
    }

    /** Чем считать: своя модель, модель соседнего приложения или нечем. */
    static final class Status {
        final boolean available;
        final String via; // "own" | "peer" | null
        final String peer; // пакет соседа, который считает (via = peer)
        final String peerReason; // почему не сосед: no_model | unreachable | not_installed | untrusted | …
        final boolean own; // своя модель скачана
        final long size;
        final boolean loaded;
        final String backend;

        Status(String via, String peer, String peerReason, boolean own, long size, boolean loaded, String backend) {
            this.available = via != null;
            this.via = via;
            this.peer = peer;
            this.peerReason = peerReason;
            this.own = own;
            this.size = size;
            this.loaded = loaded;
            this.backend = backend;
        }
    }

    private final Context app;
    private final PhoneGemmaPeer peer;
    private final ScheduledExecutorService llm = Executors.newSingleThreadScheduledExecutor();
    private volatile Engine engine;
    private volatile String engineBackend;
    private volatile Conversation current;
    // Чей ответ считается: null — этого приложения, иначе метка запроса соседа
    private volatile String currentTag;
    // Ответы соседа, прерванные, пока стояли в очереди, — не считать
    private final java.util.Set<String> cancelledTags = java.util.concurrent.ConcurrentHashMap.newKeySet();
    private volatile long cancelledAt = 0;
    private volatile boolean viaPeer = false;
    // Ответы в работе или в очереди этого процесса: свои, через соседа и для
    // соседа (PhoneGemmaService) — busy() для воркеров задач ядра
    private final java.util.concurrent.atomic.AtomicInteger inFlight = new java.util.concurrent.atomic.AtomicInteger();
    private ScheduledFuture<?> idleUnload;

    private PhoneGemma(Context app) {
        this.app = app;
        this.peer = new PhoneGemmaPeer(app);
    }

    private SharedPreferences prefs() {
        return app.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    // ── Файл модели ─────────────────────────────────────────────────────

    static File modelDir(Context ctx) {
        File base = ctx.getExternalFilesDir(null);
        if (base == null) base = ctx.getFilesDir();
        File dir = new File(base, "models");
        //noinspection ResultOfMethodCallIgnored
        dir.mkdirs();
        return dir;
    }

    static File modelFile(Context ctx) {
        return new File(modelDir(ctx), MODEL_FILE);
    }

    static boolean hasModelMagic(File f) {
        byte[] head = new byte[MODEL_MAGIC.length];
        try (InputStream in = new java.io.FileInputStream(f)) {
            int n = 0;
            while (n < head.length) {
                int r = in.read(head, n, head.length - n);
                if (r < 0) return false;
                n += r;
            }
            return java.util.Arrays.equals(head, MODEL_MAGIC);
        } catch (IOException e) {
            return false;
        }
    }

    /** Своя модель скачана (файл этого приложения). */
    boolean ownDownloaded() {
        File f = modelFile(app);
        return f.exists() && hasModelMagic(f);
    }

    /** Считать есть чем — своей моделью или соседа (может спросить соседа: не с главного потока). */
    boolean downloaded() {
        return status().available;
    }

    boolean loaded() {
        PhoneGemmaPeer.Status s = viaPeer ? peer.lastStatus() : null;
        return s != null ? s.loaded : engine != null;
    }

    /** На чём загружена модель: "gpu" | "cpu" | null (не загружена). */
    String backend() {
        PhoneGemmaPeer.Status s = viaPeer ? peer.lastStatus() : null;
        return s != null ? s.backend : engineBackend;
    }

    String ownBackend() {
        return engineBackend;
    }

    /** Своя модель — для соседа (PhoneGemmaService, MSG_STATUS). */
    android.os.Bundle ownStatus() {
        File f = modelFile(app);
        boolean dl = f.exists() && hasModelMagic(f);
        android.os.Bundle b = new android.os.Bundle();
        b.putInt("protocol", PhoneGemmaPeer.PROTOCOL);
        b.putBoolean("downloaded", dl);
        b.putLong("size", dl ? f.length() : 0);
        b.putBoolean("loaded", engine != null);
        b.putString("backend", engineBackend);
        b.putBoolean("busy", inFlight.get() > 0);
        return b;
    }

    /**
     * Модель считает или ответ ждёт очереди — здесь или у соседа, который
     * считает за это приложение. Воркеры задач ядра тогда не берут новых:
     * задача встала бы за длинным ответом и пропала по таймауту ядра. Про
     * соседа — свежий вопрос ему (не с главного потока), только если он
     * сейчас и считает (status() перед этим).
     */
    boolean busy() {
        if (inFlight.get() > 0) return true;
        return viaPeer && peer.status(true).busy;
    }

    /**
     * Кто считает. Правило одно для обоих приложений: приложение ядра — если
     * у него есть модель, иначе Арродес — если есть у него. Сосед не
     * отозвался — своя модель, если есть. Ответ соседа помнится минуту.
     */
    Status status() {
        boolean own = ownDownloaded();
        PhoneGemmaPeer.Status ps = null;
        String via;
        if (peer.isPrimary()) {
            if (!own) ps = peer.status(false);
            via = own ? "own" : ps.serves() ? "peer" : null;
        } else {
            ps = peer.status(false);
            via = ps.serves() ? "peer" : own ? "own" : null;
        }
        viaPeer = "peer".equals(via);
        if (viaPeer) return new Status(via, peer.packageName(), null, own, ps.size, ps.loaded, ps.backend);
        File f = modelFile(app);
        return new Status(via, null, ps == null ? null : ps.reason, own, own ? f.length() : 0,
                engine != null, engineBackend);
    }

    // ── Видеоядро: неудача помнится сутки ───────────────────────────────

    private boolean gpuBad() {
        return System.currentTimeMillis() < prefs().getLong(KEY_GPU_BAD_UNTIL, 0);
    }

    private void markGpuBad() {
        prefs().edit()
                .putLong(KEY_GPU_BAD_UNTIL, System.currentTimeMillis() + GPU_RETRY_MS)
                .remove(KEY_GPU_BAD_LEGACY)
                .apply();
    }

    // ── Модель: загрузка, генерация, выгрузка (поток llm) ───────────────

    private void closeEngine() {
        Conversation conv = current;
        current = null;
        if (conv != null) {
            try {
                conv.close();
            } catch (Exception ignored) {
                // уже закрыт
            }
        }
        Engine e = engine;
        engine = null;
        engineBackend = null;
        if (e != null) {
            try {
                e.close();
            } catch (Exception ex) {
                Log.w(TAG, "модель не закрылась", ex);
            }
        }
    }

    /** Загрузить модель (в потоке llm): сначала видеоядро, не вышло — процессор. */
    private Engine ensureEngine() throws Exception {
        if (engine != null) return engine;
        File f = modelFile(app);
        if (!f.exists()) throw new IOException("model_missing");
        if (!hasModelMagic(f)) {
            // Испорченный файл — удалить, чтобы в настройках можно было скачать заново
            //noinspection ResultOfMethodCallIgnored
            f.delete();
            throw new IOException("model_broken");
        }
        String cache = app.getCacheDir().getAbsolutePath();
        if (!gpuBad() && !isEmulator()) {
            try {
                Engine e = new Engine(new EngineConfig(f.getAbsolutePath(), new Backend.GPU(), null, null,
                        MAX_CONTEXT_TOKENS, null, cache));
                e.initialize();
                engine = e;
                engineBackend = "gpu";
                return e;
            } catch (Throwable t) {
                Log.i(TAG, "видеоядро не подошло, загружаю на процессоре: " + t);
                markGpuBad();
            }
        }
        Engine e = new Engine(new EngineConfig(f.getAbsolutePath(), new Backend.CPU(), null, null,
                MAX_CONTEXT_TOKENS, null, cache));
        e.initialize();
        engine = e;
        engineBackend = "cpu";
        return e;
    }

    /** Эмулятор: видеоядро там программное — модель на нём не считает. */
    private static boolean isEmulator() {
        String hw = Build.HARDWARE == null ? "" : Build.HARDWARE;
        String fp = Build.FINGERPRINT == null ? "" : Build.FINGERPRINT;
        return hw.contains("ranchu") || hw.contains("goldfish") || fp.startsWith("generic")
                || fp.contains("emulator") || fp.contains("sdk_gphone");
    }

    private void scheduleIdleUnload() {
        if (idleUnload != null) idleUnload.cancel(false);
        idleUnload = llm.schedule(this::closeEngine, IDLE_UNLOAD_MS, TimeUnit.MILLISECONDS);
    }

    private static String textOf(Message m) {
        StringBuilder sb = new StringBuilder();
        if (m == null || m.getContents() == null) return "";
        for (Content c : m.getContents().getContents()) {
            if (c instanceof Content.Text) sb.append(((Content.Text) c).getText());
        }
        return sb.toString();
    }

    /** Один ответ модели (в потоке llm): слова — в tokens по мере генерации.
     * Ошибка без единого слова — исключение; прерванный ответ — то, что успело. */
    private String generate(Engine e, String system, JSONArray history, String text,
                            int maxTokens, double temperature, double topP, Tokens tokens) throws Exception {
        final boolean gpu = "gpu".equals(engineBackend);
        boolean stalled = false;
        List<Message> initial = new ArrayList<>();
        for (int i = 0; i < history.length(); i++) {
            JSONObject m = history.optJSONObject(i);
            if (m == null) continue;
            String body = m.optString("content", "");
            if (body.isEmpty()) continue;
            initial.add("user".equals(m.optString("role"))
                    ? Message.Companion.user(body) : Message.Companion.model(body));
        }
        Conversation conv = e.createConversation(new ConversationConfig(
                Contents.Companion.of(system), initial, Collections.emptyList(),
                new SamplerConfig(40, topP, temperature, 0), false, Collections.emptyList(),
                Collections.emptyMap(), null, false, maxTokens, new ThinkingConfig(false)));
        current = conv;
        try {
            final StringBuilder out = new StringBuilder();
            final Object done = new Object();
            final Throwable[] failure = new Throwable[1];
            final boolean[] finished = new boolean[1];
            conv.sendMessageAsync(text, new MessageCallback() {
                @Override
                public void onMessage(Message message) {
                    String chunk = textOf(message);
                    if (chunk.isEmpty()) return;
                    out.append(chunk);
                    if (tokens != null) tokens.onToken(chunk);
                }

                @Override
                public void onDone() {
                    synchronized (done) {
                        finished[0] = true;
                        done.notifyAll();
                    }
                }

                @Override
                public void onError(Throwable t) {
                    synchronized (done) {
                        failure[0] = t;
                        finished[0] = true;
                        done.notifyAll();
                    }
                }
            }, Collections.emptyMap(), null, null, null, null, null, null);
            // Обычно конец приходит в onDone/onError; страховка — если после
            // прерывания или по таймауту библиотека промолчит, не висеть
            final long started = System.currentTimeMillis();
            long stalledAt = 0;
            synchronized (done) {
                while (!finished[0]) {
                    done.wait(500);
                    long now = System.currentTimeMillis();
                    if (gpu && stalledAt == 0 && cancelledAt == 0 && out.length() == 0
                            && now - started > GPU_FIRST_TOKEN_MS) {
                        stalledAt = now;
                        // Прерывание ждёт, пока видеоядро досчитает шаг, — не здесь
                        inBackground(conv::cancelProcess);
                    }
                    if (stalledAt > 0 && now - stalledAt > CANCEL_GRACE_MS) break;
                    // Сосед прервал свой ответ (или его уже нет) — до первого слова тоже
                    String tag = currentTag;
                    if (cancelledAt == 0 && tag != null && cancelledTags.contains(tag)) {
                        cancelledAt = now;
                        try {
                            conv.cancelProcess();
                        } catch (Exception ignored) {
                            // уже закончил
                        }
                    }
                    if (now - started > LOCAL_TIMEOUT_MS && cancelledAt == 0) {
                        cancelledAt = now;
                        try {
                            conv.cancelProcess();
                        } catch (Exception ignored) {
                            // уже закончил
                        }
                    }
                    if (cancelledAt > 0 && now - cancelledAt > CANCEL_GRACE_MS) break;
                }
            }
            if (stalledAt > 0 && out.length() == 0) {
                stalled = true;
                throw new IOException("gpu_stalled");
            }
            if (failure[0] != null && out.length() == 0) throw new IOException(String.valueOf(failure[0].getMessage()));
            return out.toString();
        } finally {
            current = null;
            if (stalled) {
                inBackground(conv::close); // зависший диалог закрывается, когда отпустит
            } else {
                try {
                    conv.close();
                } catch (Exception ignored) {
                    // уже закрыт
                }
            }
        }
    }

    /** Действие с моделью, которое может надолго заблокироваться (прерывание
     * или закрытие на зависшем видеоядре), — в отдельном потоке. */
    private static void inBackground(ThrowingRunnable r) {
        new Thread(() -> {
            try {
                r.run();
            } catch (Throwable ignored) {
                // уже закрыт/закончил
            }
        }, "vpc-llm-release").start();
    }

    private interface ThrowingRunnable {
        void run() throws Exception;
    }

    private Reply chatLocked(String system, JSONArray history, String text, int maxTokens,
                             double temperature, double topP, Tokens tokens, String tag,
                             long deadline) throws Exception {
        // Сосед прервал ответ, пока тот ждал очереди, — не начинать
        if (tag != null && cancelledTags.remove(tag)) return new Reply("", engineBackend);
        // Задача ядра простояла в очереди дольше, чем ядро ждёт ответ, — не считать впустую
        if (deadline > 0 && System.currentTimeMillis() > deadline) throw new IOException("expired");
        if (idleUnload != null) idleUnload.cancel(false);
        cancelledAt = 0;
        currentTag = tag;
        try {
            String out;
            try {
                out = generate(ensureEngine(), system, history, text, maxTokens, temperature, topP, tokens);
            } catch (Throwable first) {
                String m = String.valueOf(first.getMessage());
                if (!"gpu".equals(engineBackend) || cancelledAt > 0
                        || "model_missing".equals(m) || "model_broken".equals(m)) throw first;
                // На видеоядре модель загрузилась, но не считает (нет OpenCL
                // и т. п.): дальше — процессор, и телефон это запоминает
                Log.w(TAG, "видеоядро не считает, перехожу на процессор: " + first);
                markGpuBad();
                // Модель на видеоядре может висеть в его шаге — закрыть её в фоне,
                // не дожидаясь, и загрузить на процессоре
                final Engine stuck = engine;
                engine = null;
                engineBackend = null;
                if (stuck != null) inBackground(stuck::close);
                out = generate(ensureEngine(), system, history, text, maxTokens, temperature, topP, tokens);
            }
            return new Reply(out, engineBackend);
        } finally {
            currentTag = null;
            if (tag != null) cancelledTags.remove(tag);
            scheduleIdleUnload();
        }
    }

    private static <T> T await(Future<T> f) throws Exception {
        try {
            return f.get();
        } catch (ExecutionException e) {
            Throwable c = e.getCause();
            throw c instanceof Exception ? (Exception) c : new IOException(String.valueOf(c));
        }
    }

    // ── Для плагина и службы (блокирующие — не из потока llm) ───────────

    /** Сосед не отозвался: своя модель есть — считать ею, нет — понятная причина. */
    private void peerFailed(PhoneGemmaPeer.Unavailable e, boolean canRedo) throws IOException {
        peer.forget(); // в следующий раз — спросить заново
        if (!canRedo || !ownDownloaded()) {
            throw new IOException("peer_unavailable (" + peer.packageName() + ": " + e.reason + ")");
        }
        Log.i(TAG, "сосед " + peer.packageName() + " не считает (" + e.reason + ") — считаю своей моделью");
    }

    /** Ответ модели (своей или соседа — status()); генерации ставятся в очередь и идут по одной. */
    Reply chat(String system, JSONArray history, String text, int maxTokens,
               double temperature, double topP, Tokens tokens) throws Exception {
        return chat(system, history, text, maxTokens, temperature, topP, tokens, 0L);
    }

    /** deadline — System.currentTimeMillis(), после которого не начинать (задача ядра); 0 — без срока. */
    Reply chat(String system, JSONArray history, String text, int maxTokens,
               double temperature, double topP, Tokens tokens, long deadline) throws Exception {
        inFlight.incrementAndGet();
        try {
            return chatRouted(system, history, text, maxTokens, temperature, topP, tokens, deadline);
        } finally {
            inFlight.decrementAndGet();
        }
    }

    private Reply chatRouted(String system, JSONArray history, String text, int maxTokens,
                             double temperature, double topP, Tokens tokens, long deadline) throws Exception {
        Status s = status();
        if ("peer".equals(s.via)) {
            try {
                return peer.chat(system, history, text, maxTokens, temperature, topP, tokens, deadline);
            } catch (PhoneGemmaPeer.Unavailable e) {
                // Слова уже пошли — заново своей моделью не начать
                peerFailed(e, !e.started);
            }
        } else if (s.via == null && s.peerReason != null
                && java.util.Arrays.asList("unreachable", "died", "timeout").contains(s.peerReason)) {
            // Своей нет, а сосед, у которого она может быть, не отозвался
            throw new IOException("peer_unavailable (" + peer.packageName() + ": " + s.peerReason + ")");
        }
        return chatOwn(system, history, text, maxTokens, temperature, topP, tokens, null, deadline);
    }

    /** Ответ своей моделью; tag — метка ответа соседа (PhoneGemmaService), null — этого приложения. */
    Reply chatOwn(String system, JSONArray history, String text, int maxTokens,
                  double temperature, double topP, Tokens tokens, String tag, long deadline) throws Exception {
        inFlight.incrementAndGet();
        try {
            return await(llm.submit(() -> chatLocked(system, history, text, maxTokens, temperature, topP,
                    tokens, tag, deadline)));
        } finally {
            inFlight.decrementAndGet();
        }
    }

    /** Загрузить модель заранее (вопрос придёт следом, загрузка — 10–20 с). */
    void warm() throws Exception {
        if ("peer".equals(status().via)) {
            try {
                peer.warm();
                return;
            } catch (PhoneGemmaPeer.Unavailable e) {
                peerFailed(e, true);
            }
        }
        warmOwn();
    }

    void warmOwn() throws Exception {
        await(llm.submit(() -> {
            if (idleUnload != null) idleUnload.cancel(false);
            try {
                ensureEngine();
            } finally {
                scheduleIdleUnload();
            }
            return null;
        }));
    }

    /** Прервать идущий ответ: вернётся то, что успело. Ответы для соседа — не наши, их не трогаем. */
    void stop() {
        peer.stopAll();
        Conversation conv = current;
        if (conv != null && currentTag == null) {
            cancelledAt = System.currentTimeMillis();
            try {
                conv.cancelProcess();
            } catch (Exception ignored) {
                // уже закончил
            }
        }
    }

    /** Только метка: из обратного вызова токенов модель не трогать — прервёт
     *  цикл ожидания в generate (раз в 0,5 с), как по таймауту. */
    void markCancelled(String tag) {
        if (cancelledTags.size() > 64) cancelledTags.clear(); // метки давно законченных
        cancelledTags.add(tag);
    }

    /** Прервать ответ соседа с меткой tag: идёт — вернётся то, что успело; ждёт очереди — не начнётся. */
    void cancel(String tag) {
        markCancelled(tag);
        Conversation conv = current;
        if (conv != null && tag.equals(currentTag)) {
            cancelledAt = System.currentTimeMillis();
            try {
                conv.cancelProcess();
            } catch (Exception ignored) {
                // уже закончил
            }
        }
    }

    void unload() throws Exception {
        if (viaPeer) {
            try {
                peer.unload();
            } catch (Exception e) {
                Log.i(TAG, "модель соседа не выгрузилась: " + e.getMessage());
            }
        }
        unloadOwn();
    }

    void unloadOwn() throws Exception {
        await(llm.submit(() -> {
            closeEngine();
            return null;
        }));
    }

    /** Удалить свою модель (модель соседа — его): выгрузить, стереть файлы, видеоядро — пробовать снова. */
    void deleteModel() throws Exception {
        peer.forget();
        await(llm.submit(() -> {
            closeEngine();
            prefs().edit().remove(KEY_GPU_BAD_LEGACY).remove(KEY_GPU_BAD_UNTIL).apply();
            //noinspection ResultOfMethodCallIgnored
            modelFile(app).delete();
            //noinspection ResultOfMethodCallIgnored
            new File(modelDir(app), MODEL_FILE + ".part").delete();
            return null;
        }));
    }
}
