package io.vpcore.app;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * Сервисы на телефоне: вход в Google для Gmail — как у ядра
 * (app/integrations/sources/google.py): свой OAuth-клиент пользователя типа
 * «Приложение для компьютера», PKCE и возврат на 127.0.0.1 (loopback).
 * Сервера у проекта нет, поэтому одноразовый HTTP-сервер поднимает само
 * приложение на 127.0.0.1 на случайном порту, а вход идёт в системном
 * браузере телефона (Chrome) — он и вернёт код на этот адрес. Права —
 * только gmail.readonly (+ email — какой аккаунт подключён): Диск без
 * компьютера не проверяется.
 *
 * Процесс приложения, пока открыт браузер, могут выгрузить — тогда сервер
 * пропадёт, браузер покажет «не удаётся подключиться», а адрес с кодом
 * можно вставить в «Сервисах» (finishFromUrl): ожидающий вход (state,
 * verifier) PhoneServices хранит на диске.
 */
final class SvcGoogleLogin {

    private SvcGoogleLogin() {
    }

    static String authUrl = "https://accounts.google.com/o/oauth2/v2/auth";
    static final String GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly";
    static final String[] SCOPES = {"openid", "email", GMAIL_SCOPE};
    static final long TTL_SEC = 600;
    private static final Pattern CLIENT_ID = Pattern.compile("^[\\w.-]+\\.apps\\.googleusercontent\\.com$");

    /** parse_client ядра: ID и секрет из полей или из client_secret_*.json. */
    static JSONObject parseClient(String jsonText, String clientId, String clientSecret) {
        String cid = clientId == null ? "" : clientId.trim();
        String secret = clientSecret == null ? "" : clientSecret.trim();
        if (jsonText != null && !jsonText.trim().isEmpty()) {
            try {
                JSONObject d = new JSONObject(jsonText.trim());
                JSONObject inner = d.optJSONObject("installed");
                if (inner == null) inner = d.optJSONObject("web");
                if (inner == null) inner = d;
                cid = SvcUtil.strip(SvcUtil.str(inner, "client_id"));
                secret = SvcUtil.strip(SvcUtil.str(inner, "client_secret"));
            } catch (Exception e) {
                cid = "";
                secret = "";
            }
        }
        if (!CLIENT_ID.matcher(cid).matches() || secret.isEmpty() || secret.length() > 200) {
            throw new IllegalArgumentException("нужны client_id (…apps.googleusercontent.com) и client_secret");
        }
        JSONObject out = new JSONObject();
        SvcUtil.put(out, "client_id", cid);
        SvcUtil.put(out, "client_secret", secret);
        return out;
    }

    /** Ожидающий вход: {state, verifier, redirect, expires}. */
    static JSONObject pending(int port, double now) {
        JSONObject p = new JSONObject();
        SvcUtil.put(p, "state", SvcUtil.tokenUrlsafe(24));
        SvcUtil.put(p, "verifier", SvcUtil.tokenUrlsafe(64));
        SvcUtil.put(p, "redirect", "http://127.0.0.1:" + port + "/");
        SvcUtil.put(p, "expires", now + TTL_SEC);
        return p;
    }

    static String loginUrl(String clientId, JSONObject pending) {
        Map<String, String> q = new LinkedHashMap<>();
        q.put("client_id", clientId);
        q.put("redirect_uri", SvcUtil.str(pending, "redirect"));
        q.put("response_type", "code");
        StringBuilder scope = new StringBuilder();
        for (String s : SCOPES) scope.append(scope.length() > 0 ? " " : "").append(s);
        q.put("scope", scope.toString());
        q.put("state", SvcUtil.str(pending, "state"));
        q.put("code_challenge", SvcUtil.sha256B64Url(SvcUtil.str(pending, "verifier")));
        q.put("code_challenge_method", "S256");
        q.put("access_type", "offline");
        q.put("prompt", "consent");
        q.put("include_granted_scopes", "true");
        return authUrl + "?" + SvcSources.form(q);
    }

    static String jwtEmail(String idToken) {
        try {
            String[] parts = idToken.split("\\.");
            JSONObject payload = new JSONObject(new String(SvcUtil.b64decode(parts[1]), StandardCharsets.UTF_8));
            return SvcUtil.str(payload, "email");
        } catch (Exception e) {
            return "";
        }
    }

    private static boolean sameState(String a, String b) {
        return MessageDigest.isEqual(a.getBytes(StandardCharsets.UTF_8), b.getBytes(StandardCharsets.UTF_8));
    }

    /**
     * _finish ядра: проверка state, обмен кода на токены. → {ok, email, gmail,
     * error, secrets} — secrets (refresh_token и прочее) сохраняет вызывающий.
     */
    static JSONObject finish(JSONObject pending, JSONObject client, String code, String state, String error, double now) {
        JSONObject res = new JSONObject();
        SvcUtil.put(res, "ok", false);
        if (pending == null || now > SvcUtil.num(pending, "expires", 0)) {
            SvcUtil.put(res, "error", "вход устарел — начните заново");
        } else if (!sameState(state == null ? "" : state, SvcUtil.str(pending, "state"))) {
            SvcUtil.put(res, "error", "чужой ответ входа (state не совпал)");
        } else if (error != null && !error.isEmpty()) {
            SvcUtil.put(res, "error", error.equals("access_denied") ? "доступ не выдан" : error);
        } else if (code == null || code.isEmpty()) {
            SvcUtil.put(res, "error", "в ответе нет кода");
        } else {
            Map<String, String> d = new LinkedHashMap<>();
            d.put("code", code);
            d.put("client_id", SvcUtil.str(client, "client_id"));
            d.put("client_secret", SvcUtil.str(client, "client_secret"));
            d.put("redirect_uri", SvcUtil.str(pending, "redirect"));
            d.put("grant_type", "authorization_code");
            d.put("code_verifier", SvcUtil.str(pending, "verifier"));
            try {
                JSONObject body = SvcSources.tokenRequest(d);
                if (SvcUtil.str(body, "refresh_token").isEmpty()) {
                    throw new SvcSources.SourceError("Google не выдал refresh_token — уберите доступ приложения "
                            + "в аккаунте Google и подключите заново");
                }
                String email = jwtEmail(SvcUtil.str(body, "id_token"));
                org.json.JSONArray scopes = new org.json.JSONArray();
                java.util.TreeSet<String> granted = new java.util.TreeSet<>();
                for (String s : SvcUtil.str(body, "scope").split("\\s+")) if (!s.isEmpty()) granted.add(s);
                for (String s : granted) scopes.put(s);
                JSONObject sec = SvcUtil.copy(client);
                SvcUtil.put(sec, "refresh_token", SvcUtil.str(body, "refresh_token"));
                SvcUtil.put(sec, "access_token", SvcUtil.str(body, "access_token"));
                double exp = SvcUtil.num(body, "expires_in", 0);
                SvcUtil.put(sec, "expires_at", now + (exp > 0 ? exp : 3600));
                SvcUtil.put(sec, "email", email);
                SvcUtil.put(sec, "scopes", scopes);
                SvcUtil.put(sec, "connected_at", now);
                SvcUtil.put(res, "ok", true);
                SvcUtil.put(res, "email", email);
                SvcUtil.put(res, "gmail", granted.contains(GMAIL_SCOPE));
                SvcUtil.put(res, "secrets", sec);
            } catch (SvcSources.SourceError e) {
                SvcUtil.put(res, "error", e.getMessage());
            }
        }
        return res;
    }

    static Map<String, String> query(String url) {
        Map<String, String> out = new HashMap<>();
        int q = url.indexOf('?');
        if (q < 0) return out;
        String qs = url.substring(q + 1);
        int hash = qs.indexOf('#');
        if (hash >= 0) qs = qs.substring(0, hash);
        for (String pair : qs.split("&")) {
            int eq = pair.indexOf('=');
            String k = eq < 0 ? pair : pair.substring(0, eq);
            String v = eq < 0 ? "" : pair.substring(eq + 1);
            try {
                k = URLDecoder.decode(k, "UTF-8");
                v = URLDecoder.decode(v, "UTF-8");
            } catch (Exception ignored) {
                // оставляем как есть
            }
            if (!out.containsKey(k)) out.put(k, v);
        }
        return out;
    }

    /** finish_from_url ядра: адрес из строки браузера (или сам код). */
    static JSONObject finishFromUrl(JSONObject pending, JSONObject client, String pasted, double now) {
        String p = pasted == null ? "" : pasted.trim();
        Map<String, String> q = p.contains("?") ? query(p) : new HashMap<>();
        String code = q.containsKey("code") ? q.get("code") : "";
        if (code.isEmpty() && p.matches("^[\\w/.-]{10,}$")) code = p;
        String state = q.containsKey("state") ? q.get("state") : SvcUtil.str(pending, "state");
        return finish(pending, client, code, state, q.get("error"), now);
    }

    // ── Одноразовый сервер на 127.0.0.1 ─────────────────────────────────

    interface Done {
        /** Ответ Google пришёл — результат finish (вызывающий сохраняет токены). */
        JSONObject handle(String code, String state, String error);
    }

    static final class Server {
        final ServerSocket socket;
        private volatile boolean stopped = false;

        Server(ServerSocket s) {
            socket = s;
        }

        int port() {
            return socket.getLocalPort();
        }

        void stop() {
            stopped = true;
            try {
                socket.close();
            } catch (IOException ignored) {
                // уже закрыт
            }
        }

        boolean stopped() {
            return stopped;
        }
    }

    static Server open() throws IOException {
        return new Server(new ServerSocket(0, 4, InetAddress.getByName("127.0.0.1")));
    }

    /** Ждать ответа браузера (поток вызывающего) до TTL или stop(). */
    static void serve(Server server, Done done, String lang) {
        long until = System.currentTimeMillis() + TTL_SEC * 1000;
        try {
            server.socket.setSoTimeout(1000);
        } catch (IOException ignored) {
            // без таймаута accept ждёт до stop()
        }
        while (!server.stopped() && System.currentTimeMillis() < until) {
            try (Socket s = server.socket.accept()) {
                s.setSoTimeout(10_000);
                BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
                String request = in.readLine();
                String line;
                while ((line = in.readLine()) != null && !line.isEmpty()) {
                    // заголовки запроса не нужны
                }
                String target = request == null ? "" : request.replaceFirst("^[A-Z]+ ", "").replaceFirst(" HTTP/.*$", "");
                Map<String, String> q = query(target);
                OutputStream out = s.getOutputStream();
                if (!q.containsKey("code") && !q.containsKey("error")) {
                    out.write("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                            .getBytes(StandardCharsets.US_ASCII));
                    out.flush();
                    continue;
                }
                JSONObject res = done.handle(q.get("code"), q.get("state"), q.get("error"));
                byte[] body = page(SvcUtil.bool(res, "ok", false), SvcUtil.str(res, "error"), lang)
                        .getBytes(StandardCharsets.UTF_8);
                out.write(("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "
                        + body.length + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
                out.write(body);
                out.flush();
                break;
            } catch (SocketTimeoutException e) {
                // ждём дальше
            } catch (IOException e) {
                if (server.stopped()) break;
            }
        }
        server.stop();
    }

    private static String esc(String s) {
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;");
    }

    static String page(boolean ok, String error, String lang) {
        boolean en = "en".equals(lang);
        String title = ok ? (en ? "Google connected" : "Google подключён")
                : (en ? "Could not connect Google" : "Не получилось подключить Google");
        String text = ok ? (en ? "Go back to the app — mail is now checked from the phone."
                : "Вернитесь в приложение — почта теперь проверяется с телефона.")
                : esc(error.isEmpty() ? (en ? "Try again from Services." : "Попробуйте ещё раз из «Сервисов».") : error);
        return "<!doctype html><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">"
                + "<title>" + title + "</title><body style=\"font:16px system-ui;margin:32px;max-width:520px\">"
                + "<h2>" + title + "</h2><p>" + text + "</p></body>";
    }
}
