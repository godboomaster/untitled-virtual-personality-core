package io.vpcore.app;

import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ComponentName;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.ApplicationInfo;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import android.util.Log;


import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLPeerUnverifiedException;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/**
 * «Сервисы» на телефоне без компьютера: ленты RSS, GitHub, почта IMAP и
 * Gmail проверяются самим телефоном — в фоне, раз в 15 или 30 минут
 * (JobScheduler: периодическая задача с сетью; в Doze система переносит её в
 * окна обслуживания — так и задумано, сервисы не срочные). WebView в фоне
 * через пару минут замерзает, поэтому всё — опрос, разбор, текст,
 * уведомление — здесь, в Java (порт ядра: Svc*.java).
 *
 * Работает только в режиме «без компьютера» (веб сообщает setMode): с
 * компьютером сервисы проверяет он, а если он временно недоступен, телефон
 * не подменяет его — компьютер догонит сам. Перенесённые на компьютер
 * источники (moved) телефон больше не проверяет.
 *
 * Доставка — как у ядра: сообщение персоны (кто сообщает — выбор в
 * настройках, иначе последняя, с кем общались). Уведомление в канале
 * «Сообщения персон» (пока приложение свёрнуто) и запись в очередь
 * delivered.json: переписка без ядра живёт в localStorage WebView, куда Java
 * писать не может, — веб забирает очередь (takeDelivered) при открытии и
 * кладёт сообщения в чат персоны.
 *
 * Хранение — files/phone_services/: settings.json, state.json (курсоры,
 * ошибки), feed.json (лента), delivered.json (очередь для веба). Ключи
 * доступа (токен GitHub, пароль IMAP, токены Google) — в SharedPreferences,
 * зашифрованы ключом Android Keystore; вебу — только маски, целиком — лишь
 * для переноса на компьютер (exportForCore).
 */
final class PhoneServices {

    private static final String TAG = "PhoneServices";
    private static final String PREFS = "vpc_services";
    private static final String KS_ALIAS = "vpc_services_secrets";
    private static final String SECRET_PREFIX = "secret:";
    private static final String KEY_STANDALONE = "standalone";
    private static final String KEY_PERSONAS = "personas";
    private static final String KEY_LANG = "lang";
    private static final String KEY_ENDPOINTS = "endpoints";
    private static final String KEY_LOGIN = "googlePending";
    static final int JOB_ID = 0x5e7c0;
    // Доступ пропал — пробовать снова не чаще (как MAX_BACKOFF ядра)
    private static final double AUTH_RETRY_SEC = 3600;
    private static final int MAX_DELIVERED = 100;

    static final Object LOCK = new Object();
    private static final Object RUN_LOCK = new Object();
    static volatile boolean running = false;

    private PhoneServices() {
    }

    static double now() {
        return System.currentTimeMillis() / 1000.0;
    }

    static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    // ── Файлы ───────────────────────────────────────────────────────────

    private static File dir(Context ctx) {
        File d = new File(ctx.getFilesDir(), "phone_services");
        if (!d.isDirectory() && !d.mkdirs()) Log.w(TAG, "нет папки " + d);
        return d;
    }

    private static JSONObject read(Context ctx, String name) {
        File f = new File(dir(ctx), name);
        if (!f.isFile()) return new JSONObject();
        try {
            byte[] b = new byte[(int) f.length()];
            try (java.io.FileInputStream in = new java.io.FileInputStream(f)) {
                int off = 0;
                while (off < b.length) {
                    int n = in.read(b, off, b.length - off);
                    if (n < 0) break;
                    off += n;
                }
            }
            return new JSONObject(new String(b, StandardCharsets.UTF_8));
        } catch (Exception e) {
            Log.w(TAG, name + " не прочитан — заново", e);
            return new JSONObject();
        }
    }

    /** Атомарно: временный файл и переименование. */
    private static void write(Context ctx, String name, JSONObject data) {
        File d = dir(ctx);
        File tmp = new File(d, name + ".tmp");
        try (FileOutputStream out = new FileOutputStream(tmp)) {
            out.write(data.toString().getBytes(StandardCharsets.UTF_8));
            out.getFD().sync();
        } catch (IOException e) {
            Log.w(TAG, name + " не записан", e);
            return;
        }
        if (!tmp.renameTo(new File(d, name))) Log.w(TAG, name + " не переименован");
    }

    static JSONObject settings(Context ctx) {
        synchronized (LOCK) {
            return SvcTriage.normalizeSettings(read(ctx, "settings.json"));
        }
    }

    static void saveSettings(Context ctx, JSONObject s) {
        synchronized (LOCK) {
            write(ctx, "settings.json", SvcTriage.normalizeSettings(s));
        }
    }

    static JSONObject state(Context ctx) {
        synchronized (LOCK) {
            return read(ctx, "state.json");
        }
    }

    static void saveState(Context ctx, JSONObject st) {
        synchronized (LOCK) {
            write(ctx, "state.json", st);
        }
    }

    static JSONObject feed(Context ctx) {
        synchronized (LOCK) {
            return read(ctx, "feed.json");
        }
    }

    static void saveFeed(Context ctx, JSONObject f) {
        synchronized (LOCK) {
            SvcTriage.trim(f);
            write(ctx, "feed.json", f);
        }
    }

    /** Поправить состояние одного источника (слить поля). */
    static void updateState(Context ctx, String service, JSONObject values, boolean replace) {
        synchronized (LOCK) {
            JSONObject st = read(ctx, "state.json");
            JSONObject cur = replace || values == null ? new JSONObject() : SvcUtil.copy(st.optJSONObject(service));
            if (values == null) {
                st.remove(service);
            } else {
                java.util.Iterator<String> it = values.keys();
                while (it.hasNext()) {
                    String k = it.next();
                    SvcUtil.put(cur, k, values.opt(k));
                }
                SvcUtil.put(st, service, cur);
            }
            write(ctx, "state.json", st);
        }
    }

    // ── Секреты (Android Keystore, как ключи в OfflineLlmPlugin) ────────

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

    /** Секреты аккаунта: github {token, login}, imap {user, password, host, port, folder}, google {…}. */
    static JSONObject secret(Context ctx, String account) {
        synchronized (LOCK) {
            String stored = prefs(ctx).getString(SECRET_PREFIX + account, null);
            if (stored == null) return new JSONObject();
            try {
                return new JSONObject(decrypt(stored));
            } catch (Exception e) {
                // Ключ Keystore пропал (сброс блокировки экрана) — подключить заново
                Log.w(TAG, "секреты " + account + " не расшифровались", e);
                prefs(ctx).edit().remove(SECRET_PREFIX + account).apply();
                return new JSONObject();
            }
        }
    }

    static void saveSecret(Context ctx, String account, JSONObject value) {
        synchronized (LOCK) {
            SharedPreferences.Editor ed = prefs(ctx).edit();
            if (value == null) {
                ed.remove(SECRET_PREFIX + account);
            } else {
                try {
                    ed.putString(SECRET_PREFIX + account, encrypt(value.toString()));
                } catch (Exception e) {
                    throw new IllegalStateException("не удалось зашифровать ключ доступа", e);
                }
            }
            ed.commit();
        }
    }

    static JSONObject secrets(Context ctx) {
        JSONObject out = new JSONObject();
        for (String a : new String[]{"github", "imap", "google"}) SvcUtil.put(out, a, secret(ctx, a));
        return out;
    }

    static String mask(String s) {
        if (s == null || s.isEmpty()) return "";
        if (s.length() <= 8) return "…" + s.substring(Math.max(0, s.length() - 2));
        return s.substring(0, 4) + "…" + s.substring(s.length() - 4);
    }

    // ── Режим, персоны, язык ────────────────────────────────────────────

    static boolean standalone(Context ctx) {
        return prefs(ctx).getBoolean(KEY_STANDALONE, false);
    }

    static void setMode(Context ctx, boolean standalone, JSONArray personas, String lang) {
        SharedPreferences.Editor ed = prefs(ctx).edit().putBoolean(KEY_STANDALONE, standalone);
        if (personas != null) ed.putString(KEY_PERSONAS, personas.toString());
        if (lang != null) ed.putString(KEY_LANG, lang);
        ed.commit();
        reschedule(ctx);
    }

    static JSONArray personas(Context ctx) {
        try {
            return new JSONArray(prefs(ctx).getString(KEY_PERSONAS, "[]"));
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    /** Кто сообщает: выбранная, иначе первая в списке веба (последняя, с кем общались). */
    static JSONObject persona(Context ctx, JSONObject settings) {
        JSONArray list = personas(ctx);
        String want = SvcUtil.str(SvcUtil.obj(settings, "notify"), "persona");
        for (int i = 0; i < list.length(); i++) {
            JSONObject p = list.optJSONObject(i);
            if (p != null && !want.isEmpty() && want.equals(SvcUtil.str(p, "id"))) return p;
        }
        return list.length() > 0 ? list.optJSONObject(0) : null;
    }

    static String lang(Context ctx, JSONObject settings) {
        String l = SvcUtil.str(SvcUtil.obj(settings, "notify"), "language");
        if (l.equals("ru") || l.equals("en")) return l;
        return "en".equals(prefs(ctx).getString(KEY_LANG, "ru")) ? "en" : "ru";
    }

    // ── Адреса API для стенда (только отладочная сборка) ────────────────

    static boolean debuggable(Context ctx) {
        return (ctx.getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
    }

    static void setEndpoints(Context ctx, JSONObject ep) {
        prefs(ctx).edit().putString(KEY_ENDPOINTS, ep == null ? "{}" : ep.toString()).commit();
        applyEndpoints(ctx);
    }

    static void applyEndpoints(Context ctx) {
        JSONObject ep;
        try {
            ep = debuggable(ctx) ? new JSONObject(prefs(ctx).getString(KEY_ENDPOINTS, "{}")) : new JSONObject();
        } catch (Exception e) {
            ep = new JSONObject();
        }
        SvcSources.githubApi = orDefault(ep, "github", "https://api.github.com");
        SvcSources.googleToken = orDefault(ep, "token", "https://oauth2.googleapis.com/token");
        SvcSources.gmailApi = orDefault(ep, "gmail", "https://gmail.googleapis.com/gmail/v1/users/me");
        SvcGoogleLogin.authUrl = orDefault(ep, "auth", "https://accounts.google.com/o/oauth2/v2/auth");
    }

    private static String orDefault(JSONObject ep, String key, String def) {
        String v = SvcUtil.str(ep, key);
        return v.matches("(?i)^https?://.+") ? v.replaceAll("/+$", "") : def;
    }

    // ── IMAP по TLS с проверкой имени сервера ───────────────────────────

    static final SvcImap.Opener TLS = (host, port) -> {
        Socket raw = new Socket();
        raw.connect(new InetSocketAddress(host, port), 20_000);
        SSLSocket s = (SSLSocket) ((SSLSocketFactory) SSLSocketFactory.getDefault()).createSocket(raw, host, port, true);
        s.startHandshake();
        if (!HttpsURLConnection.getDefaultHostnameVerifier().verify(host, s.getSession())) {
            s.close();
            throw new SSLPeerUnverifiedException("сертификат сервера не для " + host);
        }
        return s;
    };

    // ── Что проверять ───────────────────────────────────────────────────

    static boolean configured(String name, JSONObject settings, JSONObject secrets) {
        switch (name) {
            case "rss":
                return SvcUtil.arr(SvcUtil.obj(SvcUtil.obj(settings, "services"), "rss"), "feeds").length() > 0;
            case "github":
                return !SvcUtil.str(SvcUtil.obj(secrets, "github"), "token").isEmpty();
            case "imap":
                return !SvcUtil.str(SvcUtil.obj(secrets, "imap"), "password").isEmpty();
            case "gmail": {
                JSONObject g = SvcUtil.obj(secrets, "google");
                if (SvcUtil.str(g, "refresh_token").isEmpty()) return false;
                JSONArray scopes = SvcUtil.arr(g, "scopes");
                for (int i = 0; i < scopes.length(); i++) {
                    if (SvcGoogleLogin.GMAIL_SCOPE.equals(scopes.optString(i))) return true;
                }
                return false;
            }
            default:
                return false;
        }
    }

    static boolean active(String name, JSONObject settings, JSONObject secrets) {
        return SvcUtil.bool(SvcUtil.obj(SvcUtil.obj(settings, "services"), name), "enabled", false)
                && configured(name, settings, secrets) && !SvcUtil.obj(settings, "moved").has(name);
    }

    static boolean anyActive(Context ctx) {
        JSONObject s = settings(ctx);
        JSONObject sec = secrets(ctx);
        for (String name : SvcTriage.PHONE_SOURCES) if (active(name, s, sec)) return true;
        return false;
    }

    /** Периодическая проверка: есть что проверять и режим «без компьютера» — стоит, иначе снята. */
    static void reschedule(Context ctx) {
        JobScheduler js = (JobScheduler) ctx.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if (js == null) return;
        if (!standalone(ctx) || !anyActive(ctx)) {
            js.cancel(JOB_ID);
            return;
        }
        long interval = (long) SvcUtil.num(settings(ctx), "interval_min", 15) * 60_000L;
        JobInfo existing = js.getPendingJob(JOB_ID);
        if (existing != null && existing.getIntervalMillis() == interval) return; // период не сбрасываем
        JobInfo job = new JobInfo.Builder(JOB_ID, new ComponentName(ctx, PhoneServicesJob.class))
                .setPeriodic(interval, Math.max(5 * 60_000L, interval / 3))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPersisted(true)
                .build();
        try {
            js.schedule(job);
        } catch (RuntimeException e) {
            Log.w(TAG, "проверка сервисов не поставлена", e);
        }
    }

    static boolean scheduled(Context ctx) {
        JobScheduler js = (JobScheduler) ctx.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        return js != null && js.getPendingJob(JOB_ID) != null;
    }

    // ── Проверка ────────────────────────────────────────────────────────

    private static String account(String source) {
        return source.equals("gmail") ? "google" : source;
    }

    /**
     * Опросить источники, разобрать новое, сообщить. force — по кнопке (и вне
     * режима «без компьютера», и источник с пропавшим доступом); only — один
     * источник. → {ok, busy?, polled: {имя: {ok, new, error}}, new, delivered}.
     */
    static JSONObject runCheck(Context ctx, boolean force, String only) {
        JSONObject out = new JSONObject();
        synchronized (RUN_LOCK) {
            if (running) {
                SvcUtil.put(out, "ok", false);
                SvcUtil.put(out, "busy", true);
                return out;
            }
            running = true;
        }
        try {
            applyEndpoints(ctx);
            if (!force && !standalone(ctx)) {
                SvcUtil.put(out, "ok", false);
                SvcUtil.put(out, "skipped", "not standalone");
                return out;
            }
            JSONObject settings = settings(ctx);
            JSONObject polled = new JSONObject();
            int fresh = 0;
            String lang = lang(ctx, settings);
            for (String name : SvcTriage.PHONE_SOURCES) {
                if (only != null && !only.equals(name)) continue;
                JSONObject secrets = secrets(ctx);
                if (!active(name, settings, secrets)) continue;
                JSONObject st = SvcUtil.obj(state(ctx), name);
                if (!force && !SvcUtil.str(st, "auth_error").isEmpty()
                        && now() - SvcUtil.num(st, "last_fail", 0) < AUTH_RETRY_SEC) {
                    continue;
                }
                JSONObject res = pollOne(ctx, name, settings, secrets, lang);
                fresh += (int) SvcUtil.num(res, "new", 0);
                SvcUtil.put(polled, name, res);
            }
            int delivered = triageAndNotify(ctx, lang);
            updateState(ctx, "run", new JSONObject().put("last_run", now()), false);
            SvcUtil.put(out, "ok", true);
            SvcUtil.put(out, "polled", polled);
            SvcUtil.put(out, "new", fresh);
            SvcUtil.put(out, "delivered", delivered);
            return out;
        } catch (Exception e) {
            Log.w(TAG, "проверка сервисов не удалась", e);
            SvcUtil.put(out, "ok", false);
            SvcUtil.put(out, "error", e.getClass().getSimpleName() + ": " + e.getMessage());
            return out;
        } finally {
            synchronized (RUN_LOCK) {
                running = false;
            }
            PhoneServicesPlugin.notifyChanged();
        }
    }

    private static JSONObject pollOne(Context ctx, String name, JSONObject settings, JSONObject secrets, String lang) {
        JSONObject res = new JSONObject();
        JSONObject cursor = SvcUtil.copy(SvcUtil.obj(state(ctx), name));
        double t = now();
        try {
            JSONArray events;
            switch (name) {
                case "rss":
                    events = SvcSources.pollRss(SvcUtil.arr(SvcUtil.obj(SvcUtil.obj(settings, "services"), "rss"), "feeds"),
                            cursor, t);
                    break;
                case "github":
                    events = SvcSources.githubPoll(SvcUtil.str(SvcUtil.obj(secrets, "github"), "token"), cursor, t);
                    break;
                case "imap":
                    events = SvcImap.poll(SvcUtil.obj(secrets, "imap"), cursor, TLS);
                    break;
                case "gmail": {
                    JSONObject g = SvcUtil.obj(secrets, "google");
                    String before = g.toString();
                    try {
                        events = SvcSources.gmailPoll(g, cursor, t);
                    } finally {
                        if (!before.equals(g.toString())) saveSecret(ctx, "google", g);
                    }
                    break;
                }
                default:
                    return res;
            }
            SvcUtil.put(cursor, "last_ok", t);
            SvcUtil.put(cursor, "last_error", SvcUtil.str(cursor, "partial_error"));
            SvcUtil.put(cursor, "auth_error", "");
            updateState(ctx, name, cursor, true);
            updateState(ctx, account(name), new JSONObject().put("auth_notified", false), false);
            int added = 0;
            if (events.length() > 0) {
                synchronized (LOCK) {
                    JSONObject feed = feed(ctx);
                    added = SvcTriage.ingest(feed, events, t).size();
                    saveFeed(ctx, feed);
                }
            }
            SvcUtil.put(res, "ok", true);
            SvcUtil.put(res, "new", added);
        } catch (SvcSources.AuthError e) {
            JSONObject fail = new JSONObject();
            SvcUtil.put(fail, "last_error", e.getMessage());
            SvcUtil.put(fail, "auth_error", e.getMessage());
            SvcUtil.put(fail, "last_fail", t);
            updateState(ctx, name, fail, false);
            authLost(ctx, account(name), lang);
            SvcUtil.put(res, "ok", false);
            SvcUtil.put(res, "auth", true);
            SvcUtil.put(res, "error", e.getMessage());
        } catch (Exception e) {
            String msg = e instanceof SvcSources.SourceError ? e.getMessage()
                    : e.getClass().getSimpleName() + ": " + e.getMessage();
            JSONObject fail = new JSONObject();
            SvcUtil.put(fail, "last_error", SvcUtil.head(String.valueOf(msg), 300));
            SvcUtil.put(fail, "last_fail", t);
            updateState(ctx, name, fail, false);
            SvcUtil.put(res, "ok", false);
            SvcUtil.put(res, "error", msg);
        }
        return res;
    }

    /** Один раз сказать, что доступ к сервису пропал (hub._auth_lost ядра). */
    private static void authLost(Context ctx, String account, String lang) {
        if (SvcUtil.bool(SvcUtil.obj(state(ctx), account), "auth_notified", false)) return;
        String text = SvcTriage.text(lang, "auth").replace("{name}", SvcTriage.serviceName(lang, account));
        if (deliver(ctx, text, new ArrayList<>(), "system")) {
            try {
                updateState(ctx, account, new JSONObject().put("auth_notified", true), false);
            } catch (Exception ignored) {
                // put не падает
            }
        }
    }

    /** Новое — в разбор, разобранное — в уведомления. → сколько сообщений ушло. */
    static int triageAndNotify(Context ctx, String lang) {
        JSONObject settings = settings(ctx);
        List<JSONObject> batch;
        synchronized (LOCK) {
            JSONObject feed = feed(ctx);
            List<JSONObject> fresh = SvcTriage.withStatus(feed, "new");
            if (!fresh.isEmpty()) {
                SvcTriage.accept(SvcTriage.decide(fresh, settings), fresh, now());
                saveFeed(ctx, feed);
            }
            batch = SvcTriage.withStatus(feed, "queued");
        }
        // Прочитанное в самом сервисе — не сообщать (запросы — вне замка)
        Set<String> gone = new HashSet<>();
        if (!batch.isEmpty()) {
            JSONObject secrets = secrets(ctx);
            List<JSONObject> gh = new ArrayList<>();
            List<JSONObject> gm = new ArrayList<>();
            for (JSONObject b : batch) {
                String src = SvcUtil.str(b, "source");
                if (src.equals("github")) gh.add(b);
                if (src.equals("gmail")) gm.add(b);
            }
            if (!gh.isEmpty() && configured("github", settings, secrets)) {
                gone.addAll(SvcSources.githubGone(SvcUtil.str(SvcUtil.obj(secrets, "github"), "token"), gh));
            }
            if (!gm.isEmpty() && configured("gmail", settings, secrets)) {
                JSONObject g = SvcUtil.obj(secrets, "google");
                String before = g.toString();
                gone.addAll(SvcSources.gmailGone(g, gm, now()));
                if (!before.equals(g.toString())) saveSecret(ctx, "google", g);
            }
        }
        final int[] sent = {0};
        synchronized (LOCK) {
            JSONObject feed = feed(ctx);
            JSONObject st = state(ctx);
            JSONObject nstate = SvcUtil.copy(st.optJSONObject("notifier"));
            SvcTriage.tick(settings, feed, nstate, now(), lang, (text, items, kind) -> {
                boolean ok = deliver(ctx, text, items, kind);
                if (ok) sent[0]++;
                return ok;
            }, b -> gone);
            saveFeed(ctx, feed);
            updateState(ctx, "notifier", nstate, true);
        }
        return sent[0];
    }

    /** «Прислать сводку сейчас». */
    static int digestNow(Context ctx) {
        JSONObject settings = settings(ctx);
        String lang = lang(ctx, settings);
        synchronized (LOCK) {
            JSONObject feed = feed(ctx);
            JSONObject r = SvcTriage.digestNow(feed, now(), lang, (text, items, kind) -> deliver(ctx, text, items, kind));
            saveFeed(ctx, feed);
            return (int) SvcUtil.num(r, "sent", 0);
        }
    }

    static boolean testNotify(Context ctx) {
        JSONObject settings = settings(ctx);
        return deliver(ctx, SvcTriage.text(lang(ctx, settings), "test"), new ArrayList<>(), "test");
    }

    // ── Доставка ────────────────────────────────────────────────────────

    /** Сообщение персоны: в очередь для чата и уведомлением. false — некому сообщать. */
    static boolean deliver(Context ctx, String text, List<JSONObject> items, String kind) {
        JSONObject p = persona(ctx, settings(ctx));
        if (p == null) {
            Log.w(TAG, "нет ни одной персоны — сообщать некому");
            return false;
        }
        String id = SvcUtil.str(p, "id");
        String name = SvcUtil.str(p, "name");
        if (name.isEmpty()) name = id;
        double t = now();
        synchronized (LOCK) {
            JSONObject q = read(ctx, "delivered.json");
            JSONArray list = SvcUtil.arr(q, "items");
            JSONObject rec = new JSONObject();
            SvcUtil.put(rec, "id", SvcUtil.randomHex(8));
            SvcUtil.put(rec, "persona", id);
            SvcUtil.put(rec, "name", name);
            SvcUtil.put(rec, "text", text);
            SvcUtil.put(rec, "ts", t);
            SvcUtil.put(rec, "kind", kind);
            JSONArray ids = new JSONArray();
            for (JSONObject it : items) ids.put(SvcUtil.str(it, "id"));
            SvcUtil.put(rec, "items", ids);
            list.put(rec);
            JSONArray trimmed = new JSONArray();
            for (int i = Math.max(0, list.length() - MAX_DELIVERED); i < list.length(); i++) trimmed.put(list.opt(i));
            SvcUtil.put(q, "items", trimmed);
            write(ctx, "delivered.json", q);
        }
        // Приложение на экране — сообщение покажет сам чат (веб заберёт очередь)
        if (!BackgroundInboxService.appVisible || kind.equals("test")) show(ctx, id, name, text, t);
        PhoneServicesPlugin.notifyDelivered();
        Log.i(TAG, "уведомление (" + kind + ", " + items.size() + " соб.) от " + id + ": " + text.length() + " симв.");
        return true;
    }

    /** Очередь для веба — забрать и очистить. */
    static JSONArray takeDelivered(Context ctx) {
        synchronized (LOCK) {
            JSONArray list = SvcUtil.arr(read(ctx, "delivered.json"), "items");
            if (list.length() > 0) write(ctx, "delivered.json", new JSONObject());
            return list;
        }
    }

    /** В карточку-переписку персоны — ту же, куда пишут сообщения с ядра. */
    private static void show(Context ctx, String persona, String name, String text, double ts) {
        NotifyStack.personaMessages(ctx, persona, name,
                Collections.singletonList(new NotifyStack.Line(text, (long) (ts * 1000))));
    }

    // ── Вход в Google ───────────────────────────────────────────────────

    private static volatile SvcGoogleLogin.Server loginServer;
    static volatile JSONObject loginResult;

    /** Начать вход: сервер на 127.0.0.1 и ссылка для браузера. */
    static String startGoogleLogin(Context ctx) throws IOException {
        applyEndpoints(ctx);
        JSONObject client = secret(ctx, "google");
        if (SvcUtil.str(client, "client_id").isEmpty() || SvcUtil.str(client, "client_secret").isEmpty()) {
            throw new IllegalArgumentException("сначала укажите client_id и client_secret");
        }
        SvcGoogleLogin.Server old = loginServer;
        if (old != null) old.stop();
        SvcGoogleLogin.Server server = SvcGoogleLogin.open();
        loginServer = server;
        loginResult = null;
        JSONObject pending = SvcGoogleLogin.pending(server.port(), now());
        prefs(ctx).edit().putString(KEY_LOGIN, pending.toString()).commit();
        String lang = lang(ctx, settings(ctx));
        Thread t = new Thread(() -> SvcGoogleLogin.serve(server,
                (code, state, error) -> finishGoogle(ctx, code, state, error, null), lang), "vpc-google-login");
        t.setDaemon(true);
        t.start();
        return SvcGoogleLogin.loginUrl(SvcUtil.str(client, "client_id"), pending);
    }

    /** Ответ Google: из браузера (сервер) или вставленный адрес (pasted). */
    static JSONObject finishGoogle(Context ctx, String code, String state, String error, String pasted) {
        applyEndpoints(ctx);
        JSONObject pending;
        try {
            pending = new JSONObject(prefs(ctx).getString(KEY_LOGIN, "{}"));
        } catch (Exception e) {
            pending = null;
        }
        if (pending != null && pending.length() == 0) pending = null;
        JSONObject client = secret(ctx, "google");
        JSONObject res = pasted != null
                ? SvcGoogleLogin.finishFromUrl(pending, client, pasted, now())
                : SvcGoogleLogin.finish(pending, client, code, state, error, now());
        if (SvcUtil.bool(res, "ok", false)) {
            saveSecret(ctx, "google", res.optJSONObject("secrets"));
            updateState(ctx, "gmail", null, true);
            updateState(ctx, "google", new JSONObject(), true);
            JSONObject s = settings(ctx);
            SvcUtil.put(SvcUtil.obj(s, "services").optJSONObject("gmail"), "enabled", SvcUtil.bool(res, "gmail", false));
            SvcUtil.obj(s, "moved").remove("gmail");
            saveSettings(ctx, s);
            prefs(ctx).edit().remove(KEY_LOGIN).commit();
            reschedule(ctx);
            if (pasted != null && loginServer != null) loginServer.stop();
        }
        res.remove("secrets");
        loginResult = res;
        PhoneServicesPlugin.notifyChanged();
        return res;
    }

    // ── Отключение ──────────────────────────────────────────────────────

    static void disconnect(Context ctx, String account) {
        JSONObject s = settings(ctx);
        JSONObject svcs = SvcUtil.obj(s, "services");
        switch (account) {
            case "rss":
                SvcUtil.put(svcs.optJSONObject("rss"), "feeds", new JSONArray());
                SvcUtil.put(svcs.optJSONObject("rss"), "enabled", false);
                updateState(ctx, "rss", null, true);
                break;
            case "github":
            case "imap":
                saveSecret(ctx, account, null);
                SvcUtil.put(svcs.optJSONObject(account), "enabled", false);
                updateState(ctx, account, null, true);
                break;
            case "google":
                saveSecret(ctx, "google", null);
                SvcUtil.put(svcs.optJSONObject("gmail"), "enabled", false);
                updateState(ctx, "gmail", null, true);
                updateState(ctx, "google", null, true);
                break;
            default:
                return;
        }
        SvcUtil.obj(s, "moved").remove(account.equals("google") ? "gmail" : account);
        saveSettings(ctx, s);
        reschedule(ctx);
    }
}
