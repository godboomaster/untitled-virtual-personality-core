package io.vpcore.app;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * Сервисы на телефоне: опрос источников — порт ядра
 * (app/integrations/sources: rss, github, google — Gmail). Только чтение:
 * ничего не помечается прочитанным. Первый опрос только запоминает курсор —
 * старое не становится «новым». Состояние (курсоры) — JSONObject источника,
 * который опрос меняет на месте; токены Google — так же (секреты, их
 * сохраняет вызывающий).
 *
 * Адреса API — поля класса: тесты и стенд подменяют их своим сервером.
 */
final class SvcSources {

    private SvcSources() {
    }

    static String githubApi = "https://api.github.com";
    static String googleToken = "https://oauth2.googleapis.com/token";
    static String gmailApi = "https://gmail.googleapis.com/gmail/v1/users/me";

    static final String USER_AGENT = "virtual-persona-core";
    static final int MAX_FEED_BYTES = 5 * 1024 * 1024;
    static final int MAX_JSON_BYTES = 8 * 1024 * 1024;
    static final int RSS_MAX_SEEN = 300;
    static final int RSS_MAX_NEW = 15;
    static final int GMAIL_MAX_NEW = 30;

    static class SourceError extends Exception {
        SourceError(String msg) {
            super(msg);
        }
    }

    /** Доступ отозван или истёк — подключить заново. */
    static class AuthError extends SourceError {
        AuthError(String msg) {
            super(msg);
        }
    }

    // ── HTTP ────────────────────────────────────────────────────────────

    static final class Resp {
        int code;
        byte[] body = new byte[0];
        final Map<String, String> headers = new HashMap<>();

        String header(String name) {
            String v = headers.get(name.toLowerCase(java.util.Locale.ROOT));
            return v == null ? "" : v;
        }

        String text() {
            return new String(body, StandardCharsets.UTF_8);
        }

        Object json() {
            String t = text().trim();
            try {
                if (t.startsWith("[")) return new JSONArray(t);
                return new JSONObject(t);
            } catch (Exception e) {
                return null;
            }
        }
    }

    static Resp http(String method, String url, Map<String, String> headers, byte[] body, int timeoutMs, int maxBytes)
            throws IOException, SourceError {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(Math.min(timeoutMs, 15_000));
        c.setReadTimeout(timeoutMs);
        c.setInstanceFollowRedirects(true);
        c.setRequestMethod(method);
        c.setRequestProperty("User-Agent", USER_AGENT);
        if (headers != null) for (Map.Entry<String, String> h : headers.entrySet()) c.setRequestProperty(h.getKey(), h.getValue());
        try {
            if (body != null) {
                c.setDoOutput(true);
                try (OutputStream os = c.getOutputStream()) {
                    os.write(body);
                }
            }
            Resp r = new Resp();
            r.code = c.getResponseCode();
            for (Map.Entry<String, List<String>> e : c.getHeaderFields().entrySet()) {
                if (e.getKey() != null && e.getValue() != null && !e.getValue().isEmpty()) {
                    r.headers.put(e.getKey().toLowerCase(java.util.Locale.ROOT), e.getValue().get(0));
                }
            }
            InputStream in = r.code >= 400 ? c.getErrorStream() : c.getInputStream();
            if (in != null) {
                try (InputStream is = in) {
                    ByteArrayOutputStream out = new ByteArrayOutputStream();
                    byte[] buf = new byte[16384];
                    int n;
                    while ((n = is.read(buf)) > 0) {
                        out.write(buf, 0, n);
                        if (out.size() > maxBytes) throw new SourceError("ответ больше " + (maxBytes / 1024 / 1024) + " МБ");
                    }
                    r.body = out.toByteArray();
                }
            }
            return r;
        } finally {
            c.disconnect();
        }
    }

    static String form(Map<String, String> data) {
        StringBuilder sb = new StringBuilder();
        try {
            for (Map.Entry<String, String> e : data.entrySet()) {
                if (sb.length() > 0) sb.append('&');
                sb.append(URLEncoder.encode(e.getKey(), "UTF-8")).append('=')
                        .append(URLEncoder.encode(e.getValue() == null ? "" : e.getValue(), "UTF-8"));
            }
        } catch (java.io.UnsupportedEncodingException e) {
            throw new IllegalStateException(e);
        }
        return sb.toString();
    }

    /** raise_for ядра. */
    static void raiseFor(Resp r, String service) throws SourceError {
        if ((r.code == 401 || r.code == 403) && !service.equals("github")) {
            throw new AuthError(service + ": доступ отклонён (" + r.code + ")");
        }
        if (r.code == 401) throw new AuthError(service + ": токен не принят");
        if (r.code >= 400) {
            String detail = "";
            Object j = r.json();
            if (j instanceof JSONObject) {
                JSONObject o = (JSONObject) j;
                JSONObject err = o.optJSONObject("error");
                detail = err != null ? SvcUtil.str(err, "message") : "";
                if (detail.isEmpty()) detail = SvcUtil.str(o, "message");
            } else {
                detail = SvcUtil.head(r.text(), 200);
            }
            throw new SourceError((service + ": HTTP " + r.code + " " + detail).trim());
        }
    }

    // ── RSS ─────────────────────────────────────────────────────────────

    static Resp fetchFeed(String url, String etag, String modified) throws SourceError {
        Map<String, String> h = new LinkedHashMap<>();
        h.put("Accept", "application/rss+xml, application/atom+xml, application/xml, text/xml, */*");
        if (etag != null && !etag.isEmpty()) h.put("If-None-Match", etag);
        if (modified != null && !modified.isEmpty()) h.put("If-Modified-Since", modified);
        Resp r;
        try {
            r = http("GET", url, h, null, 30_000, MAX_FEED_BYTES);
        } catch (SourceError e) {
            throw new SourceError("rss: " + url + " — лента больше 5 МБ");
        } catch (IOException e) {
            throw new SourceError("rss: " + url + " — " + e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        if (r.code == 304) return r;
        if (r.code >= 400) throw new SourceError("rss: " + url + " — HTTP " + r.code);
        return r;
    }

    /** «Проверить» одну ленту перед добавлением: {url, ok, title, items | error}. */
    static JSONObject checkFeed(String url) {
        JSONObject out = new JSONObject();
        SvcUtil.put(out, "url", url);
        try {
            Resp r = fetchFeed(url, "", "");
            JSONObject parsed = SvcParse.parseFeed(r.body);
            SvcUtil.put(out, "ok", true);
            SvcUtil.put(out, "title", SvcUtil.str(parsed, "title"));
            SvcUtil.put(out, "items", parsed.optJSONArray("items").length());
        } catch (SourceError e) {
            SvcUtil.put(out, "ok", false);
            SvcUtil.put(out, "error", e.getMessage());
        } catch (SvcParse.ParseError e) {
            SvcUtil.put(out, "ok", false);
            SvcUtil.put(out, "error", e.getMessage());
        }
        return out;
    }

    /** RssSource.poll ядра. state — {feeds: {url: {etag, modified, seen, seen_init, title}}}. */
    static JSONArray pollRss(JSONArray feeds, JSONObject state, double now) throws SourceError {
        JSONObject per = SvcUtil.copy(state.optJSONObject("feeds"));
        JSONArray events = new JSONArray();
        List<String> errors = new ArrayList<>();
        Set<String> urls = new HashSet<>();
        for (int i = 0; i < feeds.length(); i++) {
            JSONObject f = feeds.optJSONObject(i);
            String url = SvcUtil.str(f, "url");
            urls.add(url);
            JSONObject fs = SvcUtil.copy(per.optJSONObject(url));
            Resp r;
            try {
                r = fetchFeed(url, SvcUtil.str(fs, "etag"), SvcUtil.str(fs, "modified"));
            } catch (SourceError e) {
                errors.add(e.getMessage());
                continue;
            }
            if (r.code == 304) continue;
            JSONObject parsed;
            try {
                parsed = SvcParse.parseFeed(r.body);
            } catch (SvcParse.ParseError e) {
                errors.add(e.getMessage());
                continue;
            }
            String title = SvcUtil.str(parsed, "title");
            JSONArray seenArr = SvcUtil.arr(fs, "seen");
            List<String> seen = new ArrayList<>();
            for (int k = 0; k < seenArr.length(); k++) seen.add(seenArr.optString(k));
            Set<String> seenSet = new HashSet<>(seen);
            boolean first = !SvcUtil.bool(fs, "seen_init", false);
            List<Object[]> fresh = new ArrayList<>();
            JSONArray items = parsed.optJSONArray("items");
            for (int k = 0; k < items.length(); k++) {
                JSONObject it = items.optJSONObject(k);
                String key = SvcParse.rssKey(url, SvcUtil.str(it, "id"));
                if (!seenSet.add(key)) continue;
                seen.add(key);
                if (!first) fresh.add(new Object[]{key, it});
            }
            String name = SvcUtil.str(f, "title");
            if (name.isEmpty()) name = title;
            if (name.isEmpty()) name = url;
            for (int k = 0; k < Math.min(RSS_MAX_NEW, fresh.size()); k++) {
                JSONObject it = (JSONObject) fresh.get(k)[1];
                JSONObject meta = new JSONObject();
                SvcUtil.put(meta, "feed", name);
                String t = SvcUtil.str(it, "title");
                double ts = SvcUtil.num(it, "ts", 0);
                events.put(SvcParse.event("rss", (String) fresh.get(k)[0], "item", t.isEmpty() ? name : t, name,
                        SvcUtil.str(it, "summary"), SvcUtil.str(it, "link"), ts != 0 ? ts : now, meta, name));
            }
            JSONObject next = new JSONObject();
            SvcUtil.put(next, "etag", r.header("etag"));
            SvcUtil.put(next, "modified", r.header("last-modified"));
            JSONArray keep = new JSONArray();
            for (int k = Math.max(0, seen.size() - RSS_MAX_SEEN); k < seen.size(); k++) keep.put(seen.get(k));
            SvcUtil.put(next, "seen", keep);
            SvcUtil.put(next, "seen_init", true);
            SvcUtil.put(next, "title", title);
            SvcUtil.put(per, url, next);
        }
        // Удалённые из настроек ленты — забываем
        JSONObject kept = new JSONObject();
        java.util.Iterator<String> it = per.keys();
        while (it.hasNext()) {
            String u = it.next();
            if (urls.contains(u)) SvcUtil.put(kept, u, per.opt(u));
        }
        SvcUtil.put(state, "feeds", kept);
        if (!errors.isEmpty() && events.length() == 0 && errors.size() == feeds.length()) {
            throw new SourceError(SvcUtil.join("; ", errors.subList(0, Math.min(3, errors.size()))));
        }
        SvcUtil.put(state, "partial_error", errors.isEmpty() ? "" : SvcUtil.join("; ", errors.subList(0, Math.min(3, errors.size()))));
        return events;
    }

    // ── GitHub ──────────────────────────────────────────────────────────

    static final Pattern GITHUB_TOKEN = Pattern.compile("^(gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})$");

    static boolean validGithubToken(String token) {
        return token != null && GITHUB_TOKEN.matcher(token.trim()).matches();
    }

    private static Map<String, String> ghHeaders(String token) throws AuthError {
        if (token == null || token.isEmpty()) throw new AuthError("github: нет токена");
        Map<String, String> h = new LinkedHashMap<>();
        h.put("Authorization", "Bearer " + token);
        h.put("Accept", "application/vnd.github+json");
        h.put("X-GitHub-Api-Version", "2022-11-28");
        return h;
    }

    private static Resp get(String url, Map<String, String> h) throws SourceError {
        try {
            return http("GET", url, h, null, 20_000, MAX_JSON_BYTES);
        } catch (IOException e) {
            throw new SourceError(e.getClass().getSimpleName() + ": " + e.getMessage());
        }
    }

    /** GitHubSource.check ядра: {ok, account, name, scopes}. */
    static JSONObject githubCheck(String token) throws SourceError {
        Resp r = get(githubApi + "/user", ghHeaders(token));
        if (r.code == 401) throw new AuthError("github: токен не принят");
        raiseFor(r, "github");
        Object j = r.json();
        JSONObject user = j instanceof JSONObject ? (JSONObject) j : new JSONObject();
        Resp probe = get(githubApi + "/notifications?per_page=1", ghHeaders(token));
        if (probe.code == 401 || probe.code == 403) {
            throw new AuthError("github: у токена нет доступа к уведомлениям (notifications)");
        }
        JSONObject out = new JSONObject();
        SvcUtil.put(out, "ok", true);
        SvcUtil.put(out, "account", SvcUtil.str(user, "login"));
        SvcUtil.put(out, "name", SvcUtil.str(user, "name"));
        SvcUtil.put(out, "scopes", r.headers.containsKey("x-oauth-scopes") ? r.header("x-oauth-scopes") : null);
        return out;
    }

    /** GitHubSource.poll ядра. state — {since, last_modified, poll_interval}. */
    static JSONArray githubPoll(String token, JSONObject state, double now) throws SourceError {
        Map<String, String> h = ghHeaders(token);
        double since = SvcUtil.num(state, "since", 0);
        if (since == 0) {
            // Первый опрос: старые непрочитанные — не новость
            SvcUtil.put(state, "since", now);
            return new JSONArray();
        }
        String lm = SvcUtil.str(state, "last_modified");
        if (!lm.isEmpty()) h.put("If-Modified-Since", lm);
        Resp r = get(githubApi + "/notifications?per_page=50", h);
        try {
            double pi = Double.parseDouble(r.header("x-poll-interval"));
            SvcUtil.put(state, "poll_interval", pi > 0 ? pi : null);
        } catch (NumberFormatException e) {
            SvcUtil.put(state, "poll_interval", null);
        }
        if (r.code == 304) return new JSONArray();
        if (r.code == 401) throw new AuthError("github: токен отозван или истёк");
        raiseFor(r, "github");
        Object j = r.json();
        JSONArray threads = j instanceof JSONArray ? (JSONArray) j : new JSONArray();
        JSONArray events = new JSONArray();
        double newest = since;
        for (int i = 0; i < threads.length(); i++) {
            JSONObject th = threads.optJSONObject(i);
            if (th == null) continue;
            double updated = SvcUtil.isoTs(SvcUtil.str(th, "updated_at"));
            newest = Math.max(newest, updated);
            if (updated <= since) continue;
            JSONObject comment = null;
            String cu = SvcParse.commentUrl(th);
            if (cu != null) {
                try {
                    Resp c = get(cu, ghHeaders(token));
                    if (c.code < 400 && c.json() instanceof JSONObject) comment = (JSONObject) c.json();
                } catch (SourceError e) {
                    // без текста комментария — только заголовок
                }
            }
            events.put(SvcParse.githubEvent(th, comment));
        }
        SvcUtil.put(state, "since", newest);
        SvcUtil.put(state, "last_modified", r.header("last-modified"));
        return events;
    }

    /** GitHubSource.gone ядра: прочитанные на GitHub. */
    static Set<String> githubGone(String token, List<JSONObject> items) {
        Set<String> out = new HashSet<>();
        for (JSONObject it : items) {
            String thread = SvcUtil.str(SvcUtil.obj(it, "meta"), "thread");
            if (!"github".equals(SvcUtil.str(it, "source")) || thread.isEmpty()) continue;
            try {
                Resp r = get(githubApi + "/notifications/threads/" + thread, ghHeaders(token));
                Object j = r.json();
                if (r.code < 400 && j instanceof JSONObject && Boolean.FALSE.equals(((JSONObject) j).opt("unread"))) {
                    out.add(SvcUtil.str(it, "id"));
                }
            } catch (Exception ignored) {
                // не проверили — сообщаем
            }
        }
        return out;
    }

    // ── Google: токены и Gmail ──────────────────────────────────────────

    static JSONObject tokenRequest(Map<String, String> data) throws SourceError {
        Resp r;
        try {
            Map<String, String> h = new HashMap<>();
            h.put("Content-Type", "application/x-www-form-urlencoded");
            r = http("POST", googleToken, h, form(data).getBytes(StandardCharsets.UTF_8), 20_000, MAX_JSON_BYTES);
        } catch (IOException e) {
            throw new SourceError("google: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        Object j = r.json();
        JSONObject body = j instanceof JSONObject ? (JSONObject) j : new JSONObject();
        if (r.code >= 400) {
            String err = SvcUtil.str(body, "error");
            if (err.isEmpty()) err = String.valueOf(r.code);
            if (err.equals("invalid_grant") || err.equals("unauthorized_client") || err.equals("invalid_client")) {
                throw new AuthError("google: " + err);
            }
            throw new SourceError("google: вход не удался (" + err + ")");
        }
        return body;
    }

    /** Действующий access_token; истёк — обновление (sec меняется — сохранить). */
    static String googleAccessToken(JSONObject sec, double now) throws SourceError {
        if (SvcUtil.str(sec, "refresh_token").isEmpty()) throw new AuthError("google: не подключён");
        if (!SvcUtil.str(sec, "access_token").isEmpty() && SvcUtil.num(sec, "expires_at", 0) > now + 60) {
            return SvcUtil.str(sec, "access_token");
        }
        Map<String, String> d = new LinkedHashMap<>();
        d.put("client_id", SvcUtil.str(sec, "client_id"));
        d.put("client_secret", SvcUtil.str(sec, "client_secret"));
        d.put("refresh_token", SvcUtil.str(sec, "refresh_token"));
        d.put("grant_type", "refresh_token");
        JSONObject body = tokenRequest(d);
        String token = SvcUtil.str(body, "access_token");
        if (token.isEmpty()) throw new SourceError("google: нет access_token в ответе");
        double exp = SvcUtil.num(body, "expires_in", 0);
        SvcUtil.put(sec, "access_token", token);
        SvcUtil.put(sec, "expires_at", now + (exp > 0 ? exp : 3600));
        return token;
    }

    private static Resp gget(JSONObject sec, String url, double now) throws SourceError {
        Map<String, String> h = new HashMap<>();
        h.put("Authorization", "Bearer " + googleAccessToken(sec, now));
        return get(url, h);
    }

    private static JSONObject gjson(JSONObject sec, String url, double now) throws SourceError {
        Resp r = gget(sec, url, now);
        raiseFor(r, "google");
        Object j = r.json();
        return j instanceof JSONObject ? (JSONObject) j : new JSONObject();
    }

    static String enc(String s) {
        try {
            return URLEncoder.encode(s, "UTF-8");
        } catch (java.io.UnsupportedEncodingException e) {
            throw new IllegalStateException(e);
        }
    }

    static JSONObject gmailCheck(JSONObject sec, double now) throws SourceError {
        JSONObject prof = gjson(sec, gmailApi + "/profile", now);
        JSONObject out = new JSONObject();
        SvcUtil.put(out, "ok", true);
        SvcUtil.put(out, "account", SvcUtil.str(prof, "emailAddress"));
        SvcUtil.put(out, "messages", prof.opt("messagesTotal"));
        return out;
    }

    /** GmailSource.poll ядра. state — {history_id}. */
    static JSONArray gmailPoll(JSONObject sec, JSONObject state, double now) throws SourceError {
        String hid = SvcUtil.str(state, "history_id");
        if (hid.isEmpty()) {
            JSONObject prof = gjson(sec, gmailApi + "/profile", now);
            SvcUtil.put(state, "history_id", prof.opt("historyId"));
            return new JSONArray();
        }
        List<String> ids = new ArrayList<>();
        String page = null;
        Object newHid = state.opt("history_id");
        for (int round = 0; round < 5; round++) {
            String url = gmailApi + "/history?startHistoryId=" + enc(hid) + "&historyTypes=messageAdded&labelId=INBOX&maxResults=100"
                    + (page != null ? "&pageToken=" + enc(page) : "");
            Resp r = gget(sec, url, now);
            if (r.code == 404) {
                // historyId слишком старый (телефон долго не проверял) — с текущего
                JSONObject prof = gjson(sec, gmailApi + "/profile", now);
                SvcUtil.put(state, "history_id", prof.opt("historyId"));
                return new JSONArray();
            }
            raiseFor(r, "google");
            Object j = r.json();
            JSONObject data = j instanceof JSONObject ? (JSONObject) j : new JSONObject();
            if (!SvcUtil.isNull(data.opt("historyId")) && !SvcUtil.str(data, "historyId").isEmpty()) newHid = data.opt("historyId");
            JSONArray hist = SvcUtil.arr(data, "history");
            for (int i = 0; i < hist.length(); i++) {
                JSONArray added = SvcUtil.arr(hist.optJSONObject(i), "messagesAdded");
                for (int k = 0; k < added.length(); k++) {
                    String mid = SvcUtil.str(SvcUtil.obj(added.optJSONObject(k), "message"), "id");
                    if (!mid.isEmpty() && !ids.contains(mid)) ids.add(mid);
                }
            }
            page = SvcUtil.str(data, "nextPageToken");
            if (page.isEmpty()) break;
        }
        JSONArray events = new JSONArray();
        String account = SvcUtil.str(sec, "email");
        for (int i = Math.max(0, ids.size() - GMAIL_MAX_NEW); i < ids.size(); i++) {
            JSONObject msg;
            try {
                msg = gjson(sec, gmailApi + "/messages/" + enc(ids.get(i)) + "?format=full", now);
            } catch (SourceError e) {
                if (e.getMessage() != null && e.getMessage().contains("404")) continue; // письмо уже удалено
                throw e;
            }
            boolean unread = false;
            JSONArray la = SvcUtil.arr(msg, "labelIds");
            for (int k = 0; k < la.length(); k++) unread |= "UNREAD".equals(la.optString(k));
            if (!unread) continue; // прочитано раньше, чем мы увидели
            JSONObject ev = SvcParse.gmailEvent(msg, account);
            if (ev != null) events.put(ev);
        }
        SvcUtil.put(state, "history_id", newHid);
        return events;
    }

    static Set<String> gmailGone(JSONObject sec, List<JSONObject> items, double now) {
        Set<String> out = new HashSet<>();
        for (JSONObject it : items) {
            String mid = SvcUtil.str(SvcUtil.obj(it, "meta"), "message_id");
            if (!"gmail".equals(SvcUtil.str(it, "source")) || mid.isEmpty()) continue;
            try {
                JSONObject msg = gjson(sec, gmailApi + "/messages/" + enc(mid) + "?format=minimal", now);
                boolean unread = false;
                JSONArray la = SvcUtil.arr(msg, "labelIds");
                for (int k = 0; k < la.length(); k++) unread |= "UNREAD".equals(la.optString(k));
                if (!unread) out.add(SvcUtil.str(it, "id"));
            } catch (Exception ignored) {
                // не проверили — сообщаем
            }
        }
        return out;
    }
}
