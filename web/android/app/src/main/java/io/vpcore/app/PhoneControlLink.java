package io.vpcore.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.SystemClock;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;

/* Связь службы управления экраном с ядром: телефон — устройство ядра
   (app/core/devices), как Pi с камерой. POST /api/devices/hello — кто он и
   что умеет; дальше долгий опрос GET /api/devices/{id}/commands и итог
   каждой команды POST /api/devices/{id}/result.

   Адрес ядра и токен кладёт приложение (PhoneControlPlugin.configure) при
   каждом подключении к ядру — в свои prefs: служба живёт и при закрытом
   приложении, и с выключенными фоновыми уведомлениями. Адреса нет — ждём,
   пока приложение его даст (kick).

   Команды — по одной, по очереди: после действия ждём, пока экран
   успокоится, и шлём свежий снимок вместе с итогом (ядру не нужен второй
   запрос, чтобы увидеть, что стало). */
final class PhoneControlLink implements Runnable {

    private static final String TAG = "PhoneControl";

    static final String PREFS = "phone_control";
    static final String KEY_BASE_URL = "baseUrl";
    static final String KEY_TOKEN = "token";
    static final String KEY_DEVICE_ID = "deviceId";

    static final String[] CAPABILITIES = {
        "screen.read", "screen.tap", "screen.type", "screen.scroll", "screen.global",
        "apps.list", "app.open",
    };

    private static final int LONG_WAIT_SEC = 25;
    private static final int READ_TIMEOUT_MS = (LONG_WAIT_SEC + 15) * 1000;
    private static final long RETRY_MS = 3_000L;
    private static final long MAX_RETRY_MS = 60_000L;
    private static final long NO_CORE_WAIT_MS = 30_000L;

    // Для карточки в «Настройках»: off | no_core | connecting | online | offline | auth
    static volatile String state = "off";
    static volatile String lastError = "";
    static volatile long lastSeenAt; // System.currentTimeMillis() последнего ответа ядра
    static volatile String lastCommand = "";

    private static final Object wake = new Object();

    private final PhoneControlService service;
    private volatile boolean running;
    private volatile HttpURLConnection current;
    private Thread thread;

    PhoneControlLink(PhoneControlService service) {
        this.service = service;
    }

    static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** Адрес или токен сменились — не ждать конца паузы. */
    static void kick() {
        synchronized (wake) {
            wake.notifyAll();
        }
    }

    static String deviceId(Context ctx) {
        SharedPreferences p = prefs(ctx);
        String id = p.getString(KEY_DEVICE_ID, "");
        if (id.isEmpty()) {
            byte[] b = new byte[6];
            new SecureRandom().nextBytes(b);
            StringBuilder sb = new StringBuilder("phone-");
            for (byte x : b) sb.append(String.format("%02x", x));
            id = sb.toString();
            p.edit().putString(KEY_DEVICE_ID, id).apply();
        }
        return id;
    }

    void start() {
        running = true;
        thread = new Thread(this, "phone-control-link");
        thread.start();
    }

    void stop() {
        running = false;
        state = "off";
        HttpURLConnection c = current;
        if (c != null) new Thread(c::disconnect).start();
        kick();
        if (thread != null) thread.interrupt();
        // Ядру — «ухожу»: иначе оно ещё ~40 с считает телефон на связи, и
        // просьба в чате ждёт таймаута вместо честного «не на связи»
        SharedPreferences p = prefs(service);
        String base = p.getString(KEY_BASE_URL, "").replaceAll("/+$", "");
        String token = p.getString(KEY_TOKEN, "");
        String id = deviceId(service);
        if (!base.isEmpty()) {
            new Thread(() -> {
                try {
                    request("POST", base + "/api/devices/" + URLEncoder.encode(id, "UTF-8") + "/bye",
                        token, new JSONObject(), 5_000);
                } catch (Exception ignored) {
                    // ядра нет — оно и так увидит, что опросов больше нет
                }
            }, "phone-control-bye").start();
        }
    }

    @Override
    public void run() {
        long backoff = RETRY_MS;
        boolean hello = false;
        String helloFor = "";
        while (running) {
            SharedPreferences p = prefs(service);
            String base = p.getString(KEY_BASE_URL, "").replaceAll("/+$", "");
            String token = p.getString(KEY_TOKEN, "");
            if (base.isEmpty()) {
                state = "no_core";
                pause(NO_CORE_WAIT_MS);
                continue;
            }
            String id = deviceId(service);
            try {
                // Новое ядро или новый токен — представиться заново
                if (!hello || !helloFor.equals(base + "\n" + token)) {
                    state = "connecting";
                    hello(base, token, id);
                    hello = true;
                    helloFor = base + "\n" + token;
                    // Ядро нас знает — уже на связи, не ждём конца первого
                    // долгого опроса (до LONG_WAIT_SEC)
                    state = "online";
                    lastError = "";
                    lastSeenAt = System.currentTimeMillis();
                }
                JSONArray cmds = poll(base, token, id);
                state = "online";
                lastError = "";
                lastSeenAt = System.currentTimeMillis();
                backoff = RETRY_MS;
                for (int k = 0; cmds != null && k < cmds.length() && running; k++) {
                    JSONObject cmd = cmds.optJSONObject(k);
                    if (cmd != null) execute(base, token, id, cmd);
                }
            } catch (HttpStatus e) {
                if (e.code == 404) {
                    hello = false; // ядро перезапустилось и нас не знает
                    pause(500);
                    continue;
                }
                state = e.code == 401 || e.code == 403 ? "auth" : "offline";
                lastError = "HTTP " + e.code;
                pause(backoff);
                backoff = Math.min(backoff * 2, MAX_RETRY_MS);
            } catch (Exception e) {
                if (!running) break;
                state = "offline";
                lastError = String.valueOf(e.getMessage());
                hello = false;
                pause(backoff);
                backoff = Math.min(backoff * 2, MAX_RETRY_MS);
            }
        }
        state = "off";
    }

    private void pause(long ms) {
        synchronized (wake) {
            try {
                wake.wait(ms);
            } catch (InterruptedException ignored) {
                // stop() — цикл проверит running
            }
        }
    }

    private void hello(String base, String token, String id) throws Exception {
        JSONObject body = new JSONObject();
        body.put("id", id);
        String model = (Build.MANUFACTURER + " " + Build.MODEL).trim();
        body.put("name", model.length() > 64 ? model.substring(0, 64) : model);
        body.put("names", new JSONArray().put("телефон").put("phone"));
        JSONArray caps = new JSONArray();
        for (String c : CAPABILITIES) caps.put(c);
        body.put("capabilities", caps);
        request("POST", base + "/api/devices/hello", token, body, 15_000);
    }

    private JSONArray poll(String base, String token, String id) throws Exception {
        String url = base + "/api/devices/" + URLEncoder.encode(id, "UTF-8") + "/commands?wait=" + LONG_WAIT_SEC;
        JSONObject o = request("GET", url, token, null, READ_TIMEOUT_MS);
        return o.optJSONArray("commands");
    }

    private void execute(String base, String token, String id, JSONObject cmd) {
        String action = cmd.optString("action", "");
        JSONObject args = cmd.optJSONObject("args");
        if (args == null) args = new JSONObject();
        lastCommand = action;
        JSONObject result = new JSONObject();
        long t0 = SystemClock.uptimeMillis();
        try {
            result.put("command_id", cmd.optString("id", ""));
            JSONObject data;
            try {
                data = run(action, args);
                result.put("ok", true);
                result.put("data", data);
            } catch (PhoneScreen.Failure f) {
                result.put("ok", false);
                result.put("error", f.code + ": " + f.getMessage());
            } catch (RuntimeException e) {
                Log.w(TAG, action + ": сбой", e);
                result.put("ok", false);
                result.put("error", "failed: " + e);
            }
        } catch (JSONException e) {
            return; // наш же JSON — не бывает
        }
        Log.i(TAG, action + " за " + (SystemClock.uptimeMillis() - t0) + " мс, ok=" + result.optBoolean("ok"));
        try {
            request("POST", base + "/api/devices/" + URLEncoder.encode(id, "UTF-8") + "/result",
                token, result, 20_000);
        } catch (Exception e) {
            // Ход на ядре не дождётся итога и честно скажет «не ответил»
            Log.w(TAG, "итог не отправлен: " + e.getMessage());
        }
    }

    private JSONObject run(String action, JSONObject args) throws PhoneScreen.Failure {
        PhoneControlService s = service;
        try {
            JSONObject data;
            long[] wait; // min, тишина, максимум — сколько ждать, пока экран успокоится
            switch (action) {
                case "screen.read":
                    return new JSONObject().put("screen", PhoneScreen.snapshot(s));
                case "apps.list":
                    return PhoneScreen.apps(s);
                case "screen.tap":
                    data = PhoneScreen.tap(s, args);
                    wait = new long[]{250, 400, 3000};
                    break;
                case "screen.type":
                    data = PhoneScreen.type(s, args);
                    wait = new long[]{150, 300, 1500};
                    break;
                case "screen.scroll":
                    data = PhoneScreen.scroll(s, args);
                    wait = new long[]{300, 300, 2000};
                    break;
                case "screen.global":
                    data = PhoneScreen.global(s, args);
                    wait = new long[]{300, 400, 2500};
                    break;
                case "app.open":
                    data = PhoneScreen.openApp(s, args);
                    wait = new long[]{700, 500, 5000};
                    break;
                default:
                    throw new PhoneScreen.Failure("unsupported", "нет такой команды: " + action);
            }
            s.settle(wait[0], wait[1], wait[2]);
            try {
                data.put("screen", PhoneScreen.snapshot(s));
            } catch (PhoneScreen.Failure f) {
                data.put("screen_error", f.code);
            }
            return data;
        } catch (JSONException e) {
            throw new PhoneScreen.Failure("failed", "JSON: " + e.getMessage());
        }
    }

    // ── HTTP ──────────────────────────────────────────────────────────

    static final class HttpStatus extends IOException {
        final int code;

        HttpStatus(int code) {
            super("HTTP " + code);
            this.code = code;
        }
    }

    private JSONObject request(String method, String url, String token, JSONObject body, int readTimeoutMs)
            throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        current = c;
        try {
            c.setRequestMethod(method);
            c.setConnectTimeout(10_000);
            c.setReadTimeout(readTimeoutMs);
            c.setUseCaches(false);
            c.setRequestProperty("Accept", "application/json");
            if (!token.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + token);
            if (body != null) {
                byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                c.setFixedLengthStreamingMode(bytes.length);
                try (OutputStream os = c.getOutputStream()) {
                    os.write(bytes);
                }
            }
            int code = c.getResponseCode();
            if (code != 200) throw new HttpStatus(code);
            String t = readAll(c.getInputStream()).trim();
            return t.startsWith("{") ? new JSONObject(t) : new JSONObject();
        } finally {
            current = null;
            c.disconnect();
        }
    }

    private static String readAll(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        byte[] b = new byte[8192];
        int n;
        while ((n = in.read(b)) > 0) buf.write(b, 0, n);
        return buf.toString("UTF-8");
    }
}
