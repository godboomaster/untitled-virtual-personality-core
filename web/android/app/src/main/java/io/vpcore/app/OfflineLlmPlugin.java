package io.vpcore.app;

import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.Build;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.provider.Settings;
import android.util.Base64;
import android.util.Log;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
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
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ConnectException;
import java.net.HttpURLConnection;
import java.net.NoRouteToHostException;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.net.UnknownHostException;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * Плагин Capacitor «OfflineLlm»: ответы персоны без ноутбука.
 *
 * Облако: ключи API-провайдеров персоны (ядро отдаёт их при связи,
 * GET /api/personas/{p}/offline/providers) хранятся зашифрованными ключом
 * Android Keystore и вебу не возвращаются: запрос к провайдеру делает сам
 * плагин (cloudChat) — заодно без CORS WebView. Перебор: провайдеры по
 * порядку персоны, у каждого — его ключи.
 *
 * Телефон: Gemma 4 E2B (LiteRT-LM, файл .litertlm ~2,6 ГБ). Загрузка с
 * докачкой (по умолчанию только по безлимитной сети), генерация с потоковой
 * выдачей (события localToken), прерывание, выгрузка из памяти после простоя.
 * Сначала видеоядро, не вышло — процессор.
 *
 * JS: setProviders({persona, providers}), providersInfo({persona}) →
 * {names}, clearProviders({persona?}), cloudChat({persona, messages,
 * maxTokens?, temperature?}) → {text, provider, model}; modelStatus(),
 * downloadModel({url?, allowMetered?}), cancelDownload(), deleteModel(),
 * localChat({requestId, system, history, text, maxTokens?, temperature?}) →
 * {text, backend}, stopLocal(), unloadModel(); события modelProgress
 * {bytes, total}, modelDone, modelError {message}, localToken {requestId, text}.
 *
 * Поиск: webSearch({query, max?, engine?: "ddg" | "bing"}) → {results:
 * [{title, body, href}]} — страница выдачи без ключа, скрытым WebView. Напоминания будильником
 * телефона (ReminderAlarms): syncReminders({items, probes}) → {armed, exact},
 * reminderStatus() → {pending, exact}, markDelivered({refs}),
 * firedReminders() → {items}, openExactAlarmSettings().
 */
@CapacitorPlugin(name = "OfflineLlm")
public class OfflineLlmPlugin extends Plugin {

    private static final String TAG = "OfflineLlm";
    private static final String PREFS = "vpc_offline";
    private static final String KS_ALIAS = "vpc_offline_secrets";
    private static final String PROV_PREFIX = "prov:";
    // Видеоядро на этом телефоне не годится (не загрузилось или не считает) — сразу процессор
    private static final String KEY_GPU_BAD = "gpu_bad";

    static final String MODEL_URL =
            "https://huggingface.co/litert-community/gemma-4-E2B-it-litert-lm/resolve/main/gemma-4-E2B-it.litertlm";
    private static final String MODEL_FILE = "gemma-4-E2B-it.litertlm";
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

    private final ExecutorService io = Executors.newCachedThreadPool();
    // Модель — строго по одному: загрузка, генерация, выгрузка не пересекаются
    private final ScheduledExecutorService llm = Executors.newSingleThreadScheduledExecutor();

    private volatile Engine engine;
    private volatile String engineBackend;
    private volatile Conversation current;
    private volatile long cancelledAt = 0;
    private ScheduledFuture<?> idleUnload;

    private volatile boolean downloading = false;
    private volatile boolean cancelDownload = false;
    private volatile long downloadBytes = 0;
    private volatile long downloadTotal = 0;

    // ── Ключи провайдеров (зашифрованы ключом Android Keystore) ─────────

    private SharedPreferences prefs() {
        return getContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static SecretKey secretKey() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        if (ks.containsAlias(KS_ALIAS)) {
            return ((KeyStore.SecretKeyEntry) ks.getEntry(KS_ALIAS, null)).getSecretKey();
        }
        KeyGenerator kg = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        kg.init(new KeyGenParameterSpec.Builder(KS_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build());
        return kg.generateKey();
    }

    private static String encrypt(String plain) throws Exception {
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.ENCRYPT_MODE, secretKey());
        byte[] ct = c.doFinal(plain.getBytes(StandardCharsets.UTF_8));
        return Base64.encodeToString(c.getIV(), Base64.NO_WRAP) + ":" + Base64.encodeToString(ct, Base64.NO_WRAP);
    }

    private static String decrypt(String stored) throws Exception {
        String[] parts = stored.split(":", 2);
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, secretKey(),
                new GCMParameterSpec(128, Base64.decode(parts[0], Base64.NO_WRAP)));
        return new String(c.doFinal(Base64.decode(parts[1], Base64.NO_WRAP)), StandardCharsets.UTF_8);
    }

    private JSONArray loadProviders(String persona) {
        String stored = prefs().getString(PROV_PREFIX + persona, null);
        if (stored == null) return new JSONArray();
        try {
            return new JSONArray(decrypt(stored));
        } catch (Exception e) {
            // Ключ Keystore пропал (сброс блокировки экрана и т. п.) — ключи
            // заберутся заново при следующей связи с ноутбуком
            Log.w(TAG, "ключи провайдеров не расшифровались", e);
            prefs().edit().remove(PROV_PREFIX + persona).apply();
            return new JSONArray();
        }
    }

    @PluginMethod
    public void setProviders(PluginCall call) {
        String persona = call.getString("persona", "");
        JSArray providers = call.getArray("providers", new JSArray());
        if (persona == null || persona.isEmpty()) {
            call.reject("persona");
            return;
        }
        try {
            prefs().edit().putString(PROV_PREFIX + persona, encrypt(providers.toString())).apply();
            JSObject ret = new JSObject();
            ret.put("count", providers.length());
            call.resolve(ret);
        } catch (Exception e) {
            call.reject("Не удалось сохранить ключи: " + e.getClass().getSimpleName());
        }
    }

    @PluginMethod
    public void providersInfo(PluginCall call) {
        JSONArray list = loadProviders(call.getString("persona", ""));
        JSArray names = new JSArray();
        for (int i = 0; i < list.length(); i++) {
            JSONObject p = list.optJSONObject(i);
            if (p != null) names.put(p.optString("name"));
        }
        JSObject ret = new JSObject();
        ret.put("names", names);
        call.resolve(ret);
    }

    @PluginMethod
    public void clearProviders(PluginCall call) {
        String persona = call.getString("persona", null);
        SharedPreferences.Editor ed = prefs().edit();
        if (persona == null || persona.isEmpty()) {
            for (String k : prefs().getAll().keySet()) if (k.startsWith(PROV_PREFIX)) ed.remove(k);
        } else {
            ed.remove(PROV_PREFIX + persona);
        }
        ed.apply();
        call.resolve();
    }

    // ── Облако: запрос к OpenAI-совместимому API ────────────────────────

    @PluginMethod
    public void cloudChat(PluginCall call) {
        final String persona = call.getString("persona", "");
        final JSArray messages = call.getArray("messages", new JSArray());
        final int maxTokens = call.getInt("maxTokens", 800);
        final double temperature = call.getDouble("temperature", 0.8);
        io.execute(() -> {
            JSONArray providers = loadProviders(persona);
            if (providers.length() == 0) {
                call.reject("Нет ключей облачных моделей для этой персоны", "no_providers");
                return;
            }
            boolean anyReached = false;
            String lastError = "";
            for (int i = 0; i < providers.length(); i++) {
                JSONObject p = providers.optJSONObject(i);
                if (p == null) continue;
                JSONArray keys = p.optJSONArray("api_keys");
                for (int k = 0; keys != null && k < keys.length(); k++) {
                    try {
                        String text = postChat(p.optString("base_url"), keys.optString(k), p.optString("model"),
                                messages, maxTokens, temperature);
                        JSObject ret = new JSObject();
                        ret.put("text", text);
                        ret.put("provider", p.optString("name"));
                        ret.put("model", p.optString("model"));
                        call.resolve(ret);
                        return;
                    } catch (UnknownHostException | ConnectException | NoRouteToHostException
                             | SocketTimeoutException e) {
                        // Сети нет (или провайдер недоступен) — другие ключи того же
                        // провайдера не помогут, следующий провайдер
                        lastError = p.optString("name") + ": нет соединения";
                        break;
                    } catch (Exception e) {
                        anyReached = true;
                        lastError = p.optString("name") + ": " + e.getMessage();
                    }
                }
            }
            call.reject(anyReached ? "Облачные модели не ответили (" + lastError + ")" : "Нет интернета",
                    anyReached ? "failed" : "offline");
        });
    }

    private static String postChat(String baseUrl, String key, String model, JSArray messages,
                                   int maxTokens, double temperature) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(baseUrl + "/chat/completions").openConnection();
        try {
            c.setConnectTimeout(10_000);
            c.setReadTimeout(60_000);
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("Accept", "application/json");
            c.setRequestProperty("Authorization", "Bearer " + key);
            JSONObject body = new JSONObject();
            body.put("model", model);
            body.put("messages", new JSONArray(messages.toString()));
            body.put("max_tokens", maxTokens);
            body.put("temperature", temperature);
            try (OutputStream out = c.getOutputStream()) {
                out.write(body.toString().getBytes(StandardCharsets.UTF_8));
            }
            int code = c.getResponseCode();
            if (code != 200) throw new IOException("HTTP " + code);
            JSONObject resp = new JSONObject(readAll(c.getInputStream()));
            JSONArray choices = resp.optJSONArray("choices");
            JSONObject msg = choices == null || choices.length() == 0 ? null
                    : choices.getJSONObject(0).optJSONObject("message");
            String text = msg == null ? "" : msg.optString("content", "");
            if (text.trim().isEmpty()) throw new IOException("пустой ответ");
            return text;
        } finally {
            c.disconnect();
        }
    }

    private static String readAll(InputStream in) throws IOException {
        try (InputStream s = in) {
            java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = s.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString("UTF-8");
        }
    }

    // ── Модель на телефоне: файл ────────────────────────────────────────

    private File modelDir() {
        File base = getContext().getExternalFilesDir(null);
        if (base == null) base = getContext().getFilesDir();
        File dir = new File(base, "models");
        //noinspection ResultOfMethodCallIgnored
        dir.mkdirs();
        return dir;
    }

    private File modelFile() {
        return new File(modelDir(), MODEL_FILE);
    }

    private static boolean hasModelMagic(File f) {
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

    private boolean unmetered() {
        ConnectivityManager cm = (ConnectivityManager) getContext().getSystemService(Context.CONNECTIVITY_SERVICE);
        if (cm == null || Build.VERSION.SDK_INT < 23) return false;
        try {
            NetworkCapabilities nc = cm.getNetworkCapabilities(cm.getActiveNetwork());
            return nc != null && nc.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED);
        } catch (RuntimeException e) {
            // Без ACCESS_NETWORK_STATE или сбой службы — считаем сеть платной
            Log.w(TAG, "тип сети не определился", e);
            return false;
        }
    }

    @PluginMethod
    public void modelStatus(PluginCall call) {
        File f = modelFile();
        File part = new File(modelDir(), MODEL_FILE + ".part");
        JSObject ret = new JSObject();
        ret.put("downloaded", f.exists() && hasModelMagic(f));
        ret.put("size", f.exists() ? f.length() : 0);
        ret.put("partial", part.exists() ? part.length() : 0);
        ret.put("downloading", downloading);
        ret.put("bytes", downloadBytes);
        ret.put("total", downloadTotal);
        ret.put("loaded", engine != null);
        ret.put("backend", engineBackend);
        ret.put("unmetered", unmetered());
        ret.put("freeBytes", modelDir().getUsableSpace());
        call.resolve(ret);
    }

    @PluginMethod
    public void downloadModel(PluginCall call) {
        final String url = call.getString("url", MODEL_URL);
        boolean allowMetered = Boolean.TRUE.equals(call.getBoolean("allowMetered", false));
        if (downloading) {
            call.resolve();
            return;
        }
        if (!allowMetered && !unmetered()) {
            call.reject("Модель большая (≈2,6 ГБ) — скачивается только по Wi-Fi", "metered");
            return;
        }
        downloading = true;
        cancelDownload = false;
        call.resolve();
        io.execute(() -> {
            try {
                download(url);
                notifyListeners("modelDone", new JSObject());
            } catch (Exception e) {
                JSObject ev = new JSObject();
                ev.put("message", cancelDownload ? "cancelled" : String.valueOf(e.getMessage()));
                notifyListeners("modelError", ev);
            } finally {
                downloading = false;
            }
        });
    }

    /** Докачка в .part (Range), затем переименование: битого файла модели не бывает. */
    private void download(String url) throws Exception {
        File part = new File(modelDir(), MODEL_FILE + ".part");
        long have = part.exists() ? part.length() : 0;
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setInstanceFollowRedirects(true);
        c.setConnectTimeout(15_000);
        c.setReadTimeout(60_000);
        if (have > 0) c.setRequestProperty("Range", "bytes=" + have + "-");
        int code = c.getResponseCode();
        if (code == 200) have = 0; // сервер не умеет докачку — заново
        else if (code != 206) throw new IOException("HTTP " + code);
        long rest = c.getContentLengthLong();
        downloadTotal = rest > 0 ? have + rest : 0;
        downloadBytes = have;
        long lastEvent = 0;
        try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(part, have > 0)) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) {
                if (cancelDownload) throw new IOException("cancelled");
                out.write(buf, 0, n);
                downloadBytes += n;
                long now = System.currentTimeMillis();
                if (now - lastEvent > 1000) {
                    lastEvent = now;
                    JSObject ev = new JSObject();
                    ev.put("bytes", downloadBytes);
                    ev.put("total", downloadTotal);
                    notifyListeners("modelProgress", ev);
                }
            }
        } finally {
            c.disconnect();
        }
        if (downloadTotal > 0 && part.length() != downloadTotal) {
            throw new IOException("файл скачан не полностью");
        }
        if (!hasModelMagic(part)) {
            //noinspection ResultOfMethodCallIgnored
            part.delete();
            throw new IOException("скачанный файл — не модель LiteRT-LM");
        }
        File target = modelFile();
        if (target.exists()) //noinspection ResultOfMethodCallIgnored
            target.delete();
        if (!part.renameTo(target)) throw new IOException("не удалось сохранить файл модели");
    }

    @PluginMethod
    public void cancelDownload(PluginCall call) {
        cancelDownload = true;
        call.resolve();
    }

    @PluginMethod
    public void deleteModel(PluginCall call) {
        cancelDownload = true;
        llm.execute(() -> {
            closeEngine();
            prefs().edit().remove(KEY_GPU_BAD).apply(); // новая модель — попробовать видеоядро снова
            //noinspection ResultOfMethodCallIgnored
            modelFile().delete();
            //noinspection ResultOfMethodCallIgnored
            new File(modelDir(), MODEL_FILE + ".part").delete();
            call.resolve();
        });
    }

    // ── Модель на телефоне: генерация ───────────────────────────────────

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
        File f = modelFile();
        if (!f.exists()) throw new IOException("model_missing");
        if (!hasModelMagic(f)) {
            // Испорченный файл — удалить, чтобы в настройках можно было скачать заново
            //noinspection ResultOfMethodCallIgnored
            f.delete();
            throw new IOException("model_broken");
        }
        String cache = getContext().getCacheDir().getAbsolutePath();
        if (!prefs().getBoolean(KEY_GPU_BAD, false) && !isEmulator()) {
            try {
                Engine e = new Engine(new EngineConfig(f.getAbsolutePath(), new Backend.GPU(), null, null,
                        MAX_CONTEXT_TOKENS, null, cache));
                e.initialize();
                engine = e;
                engineBackend = "gpu";
                return e;
            } catch (Throwable t) {
                Log.i(TAG, "видеоядро не подошло, загружаю на процессоре: " + t);
                prefs().edit().putBoolean(KEY_GPU_BAD, true).apply();
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

    /** Один ответ модели (в потоке llm): поток слов — событиями localToken.
     * Ошибка без единого слова — исключение; прерванный ответ — то, что успело. */
    private String generate(Engine e, String requestId, String system, JSArray history, String text,
                            int maxTokens, double temperature) throws Exception {
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
                new SamplerConfig(40, 0.95, temperature, 0), false, Collections.emptyList(),
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
                    JSObject ev = new JSObject();
                    ev.put("requestId", requestId);
                    ev.put("text", chunk);
                    notifyListeners("localToken", ev);
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

    @PluginMethod
    public void localChat(PluginCall call) {
        final String requestId = call.getString("requestId", "");
        final String system = call.getString("system", "");
        final JSArray history = call.getArray("history", new JSArray());
        final String text = call.getString("text", "");
        final int maxTokens = call.getInt("maxTokens", 400);
        final double temperature = call.getDouble("temperature", 0.8);
        llm.execute(() -> {
            if (idleUnload != null) idleUnload.cancel(false);
            cancelledAt = 0;
            try {
                String out;
                try {
                    out = generate(ensureEngine(), requestId, system, history, text, maxTokens, temperature);
                } catch (Throwable first) {
                    String m = String.valueOf(first.getMessage());
                    if (!"gpu".equals(engineBackend) || cancelledAt > 0
                            || "model_missing".equals(m) || "model_broken".equals(m)) throw first;
                    // На видеоядре модель загрузилась, но не считает (нет OpenCL
                    // и т. п.): дальше — процессор, и телефон это запоминает
                    Log.w(TAG, "видеоядро не считает, перехожу на процессор: " + first);
                    prefs().edit().putBoolean(KEY_GPU_BAD, true).apply();
                    // Модель на видеоядре может висеть в его шаге — закрыть её в фоне,
                    // не дожидаясь, и загрузить на процессоре
                    final Engine stuck = engine;
                    engine = null;
                    engineBackend = null;
                    if (stuck != null) inBackground(stuck::close);
                    out = generate(ensureEngine(), requestId, system, history, text, maxTokens, temperature);
                }
                JSObject ret = new JSObject();
                ret.put("text", out);
                ret.put("backend", engineBackend);
                call.resolve(ret);
            } catch (Throwable t) {
                String msg = t.getMessage() != null ? t.getMessage() : t.getClass().getSimpleName();
                call.reject("Модель на телефоне не ответила: " + msg,
                        "model_missing".equals(msg) || "model_broken".equals(msg) ? msg : "failed");
            } finally {
                scheduleIdleUnload();
            }
        });
    }

    @PluginMethod
    public void stopLocal(PluginCall call) {
        Conversation conv = current;
        if (conv != null) {
            cancelledAt = System.currentTimeMillis();
            try {
                conv.cancelProcess();
            } catch (Exception ignored) {
                // уже закончил
            }
        }
        call.resolve();
    }

    @PluginMethod
    public void unloadModel(PluginCall call) {
        llm.execute(() -> {
            closeEngine();
            call.resolve();
        });
    }

    // ── Поиск в интернете без ноутбука: DuckDuckGo ──────────────────────

    private static final String DDG_LITE = "https://lite.duckduckgo.com/lite/";


    // Результаты со страницы выдачи (по поисковику): заголовок, ссылка, описание
    private static final String DDG_EXTRACT = "(function(){var out=[];"
            + "var links=document.querySelectorAll('a.result-link');"
            + "var snips=document.querySelectorAll('td.result-snippet');"
            + "for(var i=0;i<links.length;i++){out.push({title:links[i].textContent.trim(),href:links[i].href,"
            + "body:(snips[i]?snips[i].textContent:'').replace(/\\s+/g,' ').trim()});}"
            + "return JSON.stringify({items:out,blocked:/anomaly/i.test(document.documentElement.innerHTML)});})()";
    // Bing: ссылка результата — переход через bing.com/ck/a?…&u=a1<base64url адреса>
    private static final String BING_EXTRACT = "(function(){var out=[];"
            + "function real(h){try{var u=new URL(h);var p=u.searchParams.get('u');"
            + "if(u.hostname.endsWith('bing.com')&&p&&p.indexOf('a1')===0){"
            + "return decodeURIComponent(escape(atob(p.slice(2).replace(/-/g,'+').replace(/_/g,'/'))));}}catch(e){}return h;}"
            + "document.querySelectorAll('li.b_algo').forEach(function(li){"
            + "var h=li.querySelector('h2');var a=li.querySelector('h2 a')||(h&&h.closest('a'))||li.querySelector('a');"
            + "if(!a)return;"
            + "var p=li.querySelector('.b_caption p, p.b_lineclamp2, p.b_lineclamp3, p.b_lineclamp4, .b_algoSlug, p');"
            + "out.push({title:(h||a).textContent.trim(),href:real(a.href),"
            + "body:(p?p.textContent:'').replace(/\\s+/g,' ').trim()});});"
            + "return JSON.stringify({items:out,blocked:!!document.querySelector('#b_captcha, .captcha')});})()";

    /**
     * Поиск — скрытым WebView (движок Chromium): запрос из Java-клиента
     * DuckDuckGo принимает за бота (HTTP 202), а браузерный движок — нет.
     * У этого WebView нет мостов в приложение: он только открывает страницу
     * выдачи и отдаёт из неё ссылки и описания.
     */
    @PluginMethod
    public void webSearch(PluginCall call) {
        final String query = call.getString("query", "").trim();
        final int max = Math.max(1, Math.min(10, call.getInt("max", 5)));
        final boolean bing = "bing".equals(call.getString("engine", "ddg"));
        if (query.isEmpty()) {
            call.reject("query");
            return;
        }
        final String url;
        try {
            String q = java.net.URLEncoder.encode(query, "UTF-8");
            url = bing ? "https://www.bing.com/search?q=" + q : DDG_LITE + "?q=" + q;
        } catch (java.io.UnsupportedEncodingException e) {
            call.reject("query");
            return;
        }
        final String extract = bing ? BING_EXTRACT : DDG_EXTRACT;
        new android.os.Handler(android.os.Looper.getMainLooper()).post(() -> {
            final android.webkit.WebView web = new android.webkit.WebView(getContext());
            final boolean[] done = new boolean[1];
            final android.os.Handler main = new android.os.Handler(android.os.Looper.getMainLooper());
            final Runnable finish = () -> {
                web.stopLoading();
                web.destroy();
            };
            final Runnable timeout = () -> {
                if (done[0]) return;
                done[0] = true;
                finish.run();
                call.reject("Поиск не ответил вовремя", "timeout");
            };
            android.webkit.WebSettings ws = web.getSettings();
            ws.setJavaScriptEnabled(true); // только для чтения результатов со страницы
            ws.setBlockNetworkImage(true);
            ws.setAllowFileAccess(false);
            ws.setAllowContentAccess(false);
            web.setWebViewClient(new android.webkit.WebViewClient() {
                @Override
                public void onPageFinished(android.webkit.WebView view, String loaded) {
                    if (done[0]) return;
                    view.evaluateJavascript(extract, value -> {
                        if (done[0]) return;
                        done[0] = true;
                        main.removeCallbacks(timeout);
                        finish.run();
                        try {
                            // evaluateJavascript отдаёт строку как JSON-литерал
                            Object raw = new org.json.JSONTokener(value).nextValue();
                            JSONObject page = new JSONObject(String.valueOf(raw));
                            JSONArray items = page.optJSONArray("items");
                            if ((items == null || items.length() == 0) && page.optBoolean("blocked")) {
                                call.reject("Поисковик принял запрос за бота", "blocked");
                                return;
                            }
                            JSArray results = new JSArray();
                            for (int i = 0; items != null && i < items.length() && results.length() < max; i++) {
                                JSONObject it = items.optJSONObject(i);
                                if (it == null || it.optString("href").contains("duckduckgo.com/y.js")) continue;
                                JSObject r = new JSObject();
                                r.put("title", it.optString("title"));
                                r.put("body", it.optString("body"));
                                r.put("href", it.optString("href"));
                                results.put(r);
                            }
                            JSObject ret = new JSObject();
                            ret.put("results", results);
                            call.resolve(ret);
                        } catch (Exception e) {
                            call.reject("Поиск не удался: " + e.getMessage(), "failed");
                        }
                    });
                }

                @Override
                public void onReceivedError(android.webkit.WebView view, android.webkit.WebResourceRequest req,
                                            android.webkit.WebResourceError err) {
                    if (done[0] || !req.isForMainFrame()) return;
                    done[0] = true;
                    main.removeCallbacks(timeout);
                    finish.run();
                    call.reject("Нет интернета", "offline");
                }
            });
            main.postDelayed(timeout, 15_000);
            web.loadUrl(url);
        });
    }

    // ── Напоминания будильником телефона (ReminderAlarms) ───────────────

    @PluginMethod
    public void syncReminders(PluginCall call) {
        final JSArray items = call.getArray("items", new JSArray());
        final JSArray probes = call.getArray("probes", new JSArray());
        io.execute(() -> {
            JSObject ret = new JSObject();
            ret.put("armed", ReminderAlarms.sync(getContext(), items, probes));
            ret.put("exact", ReminderAlarms.canExact(getContext()));
            call.resolve(ret);
        });
    }

    @PluginMethod
    public void reminderStatus(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("pending", ReminderAlarms.pendingCount(getContext()));
        ret.put("exact", ReminderAlarms.canExact(getContext()));
        call.resolve(ret);
    }

    @PluginMethod
    public void markDelivered(PluginCall call) {
        JSArray refs = call.getArray("refs", new JSArray());
        for (int i = 0; i < refs.length(); i++) ReminderAlarms.markDelivered(getContext(), refs.optString(i));
        call.resolve();
    }

    @PluginMethod
    public void firedReminders(PluginCall call) {
        JSObject ret = new JSObject();
        try {
            ret.put("items", new JSArray(ReminderAlarms.firedLog(getContext()).toString()));
        } catch (org.json.JSONException e) {
            ret.put("items", new JSArray());
        }
        call.resolve(ret);
    }

    @PluginMethod
    public void openExactAlarmSettings(PluginCall call) {
        if (Build.VERSION.SDK_INT < 31) {
            call.resolve();
            return;
        }
        try {
            Intent i = new Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM,
                    Uri.parse("package:" + getContext().getPackageName()))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
            call.resolve();
        } catch (Exception e) {
            call.reject("Настройки точных будильников не открылись");
        }
    }

    @Override
    protected void handleOnDestroy() {
        cancelDownload = true;
        llm.execute(this::closeEngine);
    }
}
