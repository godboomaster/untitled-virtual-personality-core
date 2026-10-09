package io.vpcore.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.os.Build;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
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
 */
@CapacitorPlugin(name = "OfflineLlm")
public class OfflineLlmPlugin extends Plugin {

    private static final String TAG = "OfflineLlm";
    private static final String PREFS = "vpc_offline";
    private static final String KS_ALIAS = "vpc_offline_secrets";
    private static final String PROV_PREFIX = "prov:";

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
                    } catch (UnknownHostException | ConnectException | NoRouteToHostException e) {
                        // Сети нет (или провайдер недоступен) — следующий провайдер
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
            c.setReadTimeout(90_000);
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
        try {
            Engine e = new Engine(new EngineConfig(f.getAbsolutePath(), new Backend.GPU(), null, null,
                    MAX_CONTEXT_TOKENS, null, cache));
            e.initialize();
            engine = e;
            engineBackend = "gpu";
            return e;
        } catch (Throwable t) {
            Log.i(TAG, "видеоядро не подошло, загружаю на процессоре: " + t);
        }
        Engine e = new Engine(new EngineConfig(f.getAbsolutePath(), new Backend.CPU(), null, null,
                MAX_CONTEXT_TOKENS, null, cache));
        e.initialize();
        engine = e;
        engineBackend = "cpu";
        return e;
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
            Conversation conv = null;
            try {
                Engine e = ensureEngine();
                List<Message> initial = new ArrayList<>();
                for (int i = 0; i < history.length(); i++) {
                    JSONObject m = history.optJSONObject(i);
                    if (m == null) continue;
                    String body = m.optString("content", "");
                    if (body.isEmpty()) continue;
                    initial.add("user".equals(m.optString("role"))
                            ? Message.Companion.user(body) : Message.Companion.model(body));
                }
                conv = e.createConversation(new ConversationConfig(
                        Contents.Companion.of(system), initial, Collections.emptyList(),
                        new SamplerConfig(40, 0.95, temperature, 0), false, Collections.emptyList(),
                        Collections.emptyMap(), null, false, maxTokens, new ThinkingConfig(false)));
                current = conv;
                cancelledAt = 0;
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
                synchronized (done) {
                    while (!finished[0]) {
                        done.wait(500);
                        long now = System.currentTimeMillis();
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
                if (failure[0] != null && out.length() == 0) throw new IOException(String.valueOf(failure[0].getMessage()));
                JSObject ret = new JSObject();
                ret.put("text", out.toString());
                ret.put("backend", engineBackend);
                call.resolve(ret);
            } catch (Throwable t) {
                String msg = t.getMessage() != null ? t.getMessage() : t.getClass().getSimpleName();
                call.reject("Модель на телефоне не ответила: " + msg,
                        "model_missing".equals(msg) || "model_broken".equals(msg) ? msg : "failed");
            } finally {
                current = null;
                if (conv != null) {
                    try {
                        conv.close();
                    } catch (Exception ignored) {
                        // уже закрыт
                    }
                }
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

    @Override
    protected void handleOnDestroy() {
        cancelDownload = true;
        llm.execute(this::closeEngine);
    }
}
