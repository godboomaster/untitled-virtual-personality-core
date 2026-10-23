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

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

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
 * Сначала видеоядро, не вышло — процессор. Сама модель — PhoneGemma, одна на
 * процесс: её же считает фоновая служба (задачи Gemma ядра). И одна на два
 * приложения: модель есть у соседа (приложение ядра ↔ Арродес) — считает
 * он, modelStatus говорит via: "peer" (PhoneGemmaPeer).
 *
 * JS: setProviders({persona, providers}), providersInfo({persona}) →
 * {names}, clearProviders({persona?}), cloudChat({persona, messages,
 * maxTokens?, temperature?}) → {text, provider, model}; modelStatus(),
 * downloadModel({url?, allowMetered?}), cancelDownload(), deleteModel(),
 * localChat({requestId, system, history, text, maxTokens?, temperature?, topP?}) →
 * {text, backend}, stopLocal(), unloadModel(); события modelProgress
 * {bytes, total}, modelDone, modelError {message}, localToken {requestId, text}.
 *
 * Поиск по смыслу: модель векторов ядра в ONNX (≈135 МБ) — embedStatus() →
 * {downloaded, dir, size, partial, downloading, bytes, total, unmetered,
 * freeBytes}, downloadEmbed({allowMetered?}), cancelEmbed(), deleteEmbed();
 * события embedProgress {bytes, total}, embedDone, embedError {message}.
 * Считает её WebView, плагин только скачивает.
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
    private final ExecutorService io = Executors.newCachedThreadPool();

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
        return PhoneGemma.modelDir(getContext());
    }

    private File modelFile() {
        return PhoneGemma.modelFile(getContext());
    }

    private static boolean hasModelMagic(File f) {
        return PhoneGemma.hasModelMagic(f);
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

    /** Модель — своя или соседнего приложения (PhoneGemma.status): downloaded — считать есть чем;
     * via "own" | "peer" | null, peer — пакет, чья модель считает, own — своя скачана,
     * peerReason — почему не сосед. Соседа спрашивают по связи — не в потоке плагинов. */
    @PluginMethod
    public void modelStatus(PluginCall call) {
        io.execute(() -> {
            File part = new File(modelDir(), PhoneGemma.MODEL_FILE + ".part");
            PhoneGemma.Status s = gemma().status();
            JSObject ret = new JSObject();
            ret.put("downloaded", s.available);
            ret.put("size", s.size);
            ret.put("partial", part.exists() ? part.length() : 0);
            ret.put("downloading", downloading);
            ret.put("bytes", downloadBytes);
            ret.put("total", downloadTotal);
            ret.put("loaded", s.loaded);
            ret.put("backend", s.backend);
            ret.put("via", s.via != null ? s.via : JSONObject.NULL);
            ret.put("peer", s.peer != null ? s.peer : JSONObject.NULL);
            ret.put("own", s.own);
            ret.put("peerReason", s.peerReason != null ? s.peerReason : JSONObject.NULL);
            // Модель считает (здесь или у соседа) — воркеры задач ядра ждут
            ret.put("busy", s.available && gemma().busy());
            ret.put("unmetered", unmetered());
            ret.put("freeBytes", modelDir().getUsableSpace());
            call.resolve(ret);
        });
    }

    @PluginMethod
    public void downloadModel(PluginCall call) {
        final String url = call.getString("url", PhoneGemma.MODEL_URL);
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
        File part = new File(modelDir(), PhoneGemma.MODEL_FILE + ".part");
        ModelDownload.fetch(url, part, new ModelDownload.Progress() {
            long lastEvent = 0;

            @Override
            public void start(long total, long have) {
                downloadTotal = total;
                downloadBytes = have;
            }

            @Override
            public void bytes(long have) {
                downloadBytes = have;
                long now = System.currentTimeMillis();
                if (now - lastEvent > 1000) {
                    lastEvent = now;
                    JSObject ev = new JSObject();
                    ev.put("bytes", downloadBytes);
                    ev.put("total", downloadTotal);
                    notifyListeners("modelProgress", ev);
                }
            }

            @Override
            public boolean cancelled() {
                return cancelDownload;
            }
        });
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
        io.execute(() -> {
            try {
                gemma().deleteModel(); // и видеоядро новой модели — пробовать снова
            } catch (Exception e) {
                Log.w(TAG, "модель не удалилась", e);
            }
            call.resolve();
        });
    }

    // ── Модель векторов: поиск по смыслу в памяти персоны ───────────────
    // Та же модель, что у ядра (paraphrase-multilingual-MiniLM-L12-v2), в
    // ONNX int8; считает её WebView (onnxruntime-web, web/src/offline/embed.ts),
    // файлы читает по Capacitor.convertFileSrc(dir + "/" + имя). Ревизия
    // закреплена, файл проверяется по SHA-256 — подменённый не возьмём.

    private static final String EMBED_BASE =
            "https://huggingface.co/Xenova/paraphrase-multilingual-MiniLM-L12-v2/resolve/"
                    + "2c4055b12046f11709e9df2c122e59ffbdc2f900/";
    // {путь в репозитории, имя на телефоне, SHA-256, размер}
    private static final String[][] EMBED_FILES = {
            {"onnx/model_quantized.onnx", "model_quantized.onnx",
                    "66fc00f5f29afcaff34092e1bdd20008ca3918265a82fb9695a551e510cc4ebc", "118308126"},
            {"tokenizer.json", "tokenizer.json",
                    "b60b6b43406a48bf3638526314f3d232d97058bc93472ff2de930d43686fa441", "17082913"},
    };

    private volatile boolean embedDownloading = false;
    private volatile boolean embedCancel = false;
    private volatile long embedBytes = 0;

    private File embedDir() {
        File dir = new File(modelDir(), "embed");
        //noinspection ResultOfMethodCallIgnored
        dir.mkdirs();
        return dir;
    }

    private static long embedTotal() {
        long sum = 0;
        for (String[] f : EMBED_FILES) sum += Long.parseLong(f[3]);
        return sum;
    }

    /** Файл модели векторов на месте: проверен по SHA-256 при скачивании. */
    private boolean embedFileReady(String[] f) {
        File file = new File(embedDir(), f[1]);
        return file.exists() && file.length() == Long.parseLong(f[3]);
    }

    @PluginMethod
    public void embedStatus(PluginCall call) {
        long size = 0;
        long partial = 0;
        boolean all = true;
        for (String[] f : EMBED_FILES) {
            if (embedFileReady(f)) size += Long.parseLong(f[3]);
            else all = false;
            File part = new File(embedDir(), f[1] + ".part");
            if (part.exists()) partial += part.length();
        }
        JSObject ret = new JSObject();
        ret.put("downloaded", all);
        ret.put("dir", embedDir().getAbsolutePath());
        ret.put("size", size);
        ret.put("partial", size + partial);
        ret.put("downloading", embedDownloading);
        ret.put("bytes", embedBytes);
        ret.put("total", embedTotal());
        ret.put("unmetered", unmetered());
        ret.put("freeBytes", embedDir().getUsableSpace());
        call.resolve(ret);
    }

    @PluginMethod
    public void downloadEmbed(PluginCall call) {
        boolean allowMetered = Boolean.TRUE.equals(call.getBoolean("allowMetered", false));
        if (embedDownloading) {
            call.resolve();
            return;
        }
        if (!allowMetered && !unmetered()) {
            call.reject("Модель поиска (≈135 МБ) — скачивается только по Wi-Fi", "metered");
            return;
        }
        embedDownloading = true;
        embedCancel = false;
        call.resolve();
        io.execute(() -> {
            try {
                downloadEmbedFiles();
                notifyListeners("embedDone", new JSObject());
            } catch (Exception e) {
                JSObject ev = new JSObject();
                ev.put("message", embedCancel ? "cancelled" : String.valueOf(e.getMessage()));
                notifyListeners("embedError", ev);
            } finally {
                embedDownloading = false;
            }
        });
    }

    private void downloadEmbedFiles() throws Exception {
        long done = 0;
        embedBytes = 0;
        for (String[] f : EMBED_FILES) {
            final long before = done;
            if (!embedFileReady(f)) {
                File part = new File(embedDir(), f[1] + ".part");
                ModelDownload.fetch(EMBED_BASE + f[0], part, new ModelDownload.Progress() {
                    long lastEvent = 0;

                    @Override
                    public void start(long total, long have) {
                        embedBytes = before + have;
                    }

                    @Override
                    public void bytes(long have) {
                        embedBytes = before + have;
                        long now = System.currentTimeMillis();
                        if (now - lastEvent > 1000) {
                            lastEvent = now;
                            JSObject ev = new JSObject();
                            ev.put("bytes", embedBytes);
                            ev.put("total", embedTotal());
                            notifyListeners("embedProgress", ev);
                        }
                    }

                    @Override
                    public boolean cancelled() {
                        return embedCancel;
                    }
                });
                if (!ModelDownload.sha256(part).equals(f[2])) {
                    //noinspection ResultOfMethodCallIgnored
                    part.delete();
                    throw new IOException("файл " + f[1] + " не тот (SHA-256 не совпал)");
                }
                File target = new File(embedDir(), f[1]);
                if (target.exists()) //noinspection ResultOfMethodCallIgnored
                    target.delete();
                if (!part.renameTo(target)) throw new IOException("не удалось сохранить " + f[1]);
            }
            done += Long.parseLong(f[3]);
            embedBytes = done;
        }
    }

    @PluginMethod
    public void cancelEmbed(PluginCall call) {
        embedCancel = true;
        call.resolve();
    }

    @PluginMethod
    public void deleteEmbed(PluginCall call) {
        embedCancel = true;
        io.execute(() -> {
            for (String[] f : EMBED_FILES) {
                //noinspection ResultOfMethodCallIgnored
                new File(embedDir(), f[1]).delete();
                //noinspection ResultOfMethodCallIgnored
                new File(embedDir(), f[1] + ".part").delete();
            }
            call.resolve();
        });
    }

    // ── Модель на телефоне: генерация (PhoneGemma — общая с фоновой службой) ──

    private PhoneGemma gemma() {
        return PhoneGemma.get(getContext());
    }

    @PluginMethod
    public void localChat(PluginCall call) {
        final String requestId = call.getString("requestId", "");
        final String system = call.getString("system", "");
        final JSArray history = call.getArray("history", new JSArray());
        final String text = call.getString("text", "");
        final int maxTokens = call.getInt("maxTokens", 400);
        final double temperature = call.getDouble("temperature", 0.8);
        final double topP = call.getDouble("topP", 0.95);
        // Задача ядра: не начинать после этого времени (System.currentTimeMillis)
        final Long deadline = call.getLong("deadline", 0L);
        // Страница рисует 3D — видеоядро её: считать на процессоре (PhoneGemma)
        final Boolean spareGpu = call.getBoolean("spareGpu", false);
        // Генерации идут по одной в потоке модели; здесь — только ждём ответ
        io.execute(() -> {
            try {
                PhoneGemma.Reply r = gemma().chat(system, history, text, maxTokens, temperature, topP, chunk -> {
                    JSObject ev = new JSObject();
                    ev.put("requestId", requestId);
                    ev.put("text", chunk);
                    notifyListeners("localToken", ev);
                }, deadline == null ? 0L : deadline, Boolean.TRUE.equals(spareGpu));
                JSObject ret = new JSObject();
                ret.put("text", r.text);
                ret.put("backend", r.backend);
                call.resolve(ret);
            } catch (Throwable t) {
                String msg = t.getMessage() != null ? t.getMessage() : t.getClass().getSimpleName();
                // peer_unavailable — модель в соседнем приложении, а оно не отозвалось
                call.reject("Модель на телефоне не ответила: " + msg,
                        "model_missing".equals(msg) || "model_broken".equals(msg) ? msg
                                : msg.startsWith("peer_unavailable") ? "peer_unavailable" : "failed");
            }
        });
    }

    @PluginMethod
    public void stopLocal(PluginCall call) {
        gemma().stop();
        call.resolve();
    }

    @PluginMethod
    public void unloadModel(PluginCall call) {
        io.execute(() -> {
            try {
                gemma().unload();
            } catch (Exception e) {
                Log.w(TAG, "модель не выгрузилась", e);
            }
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
        // Модель не выгружаем: её может считать фоновая служба (задачи Gemma
        // ядра), а без дела она выгрузится сама после простоя (PhoneGemma)
    }
}
