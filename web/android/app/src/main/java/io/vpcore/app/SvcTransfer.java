package io.vpcore.app;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Сервисы на телефоне → компьютер: что отдать ядру при переносе
 * (POST /api/integrations/import, app/integrations/transfer.py). Ключи
 * доступа целиком — только здесь и только для ядра; курсоры источников —
 * чтобы компьютер не прислал второй раз то, что телефон уже показал.
 */
final class SvcTransfer {

    private SvcTransfer() {
    }

    static final int MAX_ITEMS = 200;

    /** Что из сервисов телефона можно перенести (настроено и ещё не перенесено). */
    static JSONArray movable(JSONObject settings, JSONObject secrets) {
        JSONArray out = new JSONArray();
        JSONObject moved = SvcUtil.obj(settings, "moved");
        JSONObject svcs = SvcUtil.obj(settings, "services");
        if (!moved.has("rss") && SvcUtil.arr(SvcUtil.obj(svcs, "rss"), "feeds").length() > 0) out.put("rss");
        if (!moved.has("github") && !SvcUtil.str(SvcUtil.obj(secrets, "github"), "token").isEmpty()) out.put("github");
        if (!moved.has("imap") && !SvcUtil.str(SvcUtil.obj(secrets, "imap"), "password").isEmpty()) out.put("imap");
        if (!moved.has("gmail") && !SvcUtil.str(SvcUtil.obj(secrets, "google"), "refresh_token").isEmpty()) out.put("gmail");
        return out;
    }

    /**
     * Тело запроса ядру. secrets — {github, imap, google}; state — {rss,
     * github, imap, gmail}; feed — {items, seen}.
     */
    static JSONObject bundle(JSONObject settings, JSONObject secrets, JSONObject state, JSONObject feed) {
        JSONObject out = new JSONObject();
        JSONArray names = movable(settings, secrets);
        java.util.Set<String> take = new java.util.HashSet<>();
        for (int i = 0; i < names.length(); i++) take.add(names.optString(i));
        JSONObject svcs = SvcUtil.obj(settings, "services");

        if (take.contains("rss")) {
            JSONObject rss = new JSONObject();
            SvcUtil.put(rss, "enabled", SvcUtil.bool(SvcUtil.obj(svcs, "rss"), "enabled", false));
            JSONArray feeds = new JSONArray();
            JSONObject per = SvcUtil.obj(SvcUtil.obj(state, "rss"), "feeds");
            JSONArray list = SvcUtil.arr(SvcUtil.obj(svcs, "rss"), "feeds");
            for (int i = 0; i < list.length(); i++) {
                JSONObject f = list.optJSONObject(i);
                JSONObject fs = SvcUtil.obj(per, SvcUtil.str(f, "url"));
                JSONObject one = new JSONObject();
                SvcUtil.put(one, "url", SvcUtil.str(f, "url"));
                SvcUtil.put(one, "title", SvcUtil.str(f, "title"));
                SvcUtil.put(one, "seen", SvcUtil.arr(fs, "seen"));
                SvcUtil.put(one, "seen_init", SvcUtil.bool(fs, "seen_init", false));
                SvcUtil.put(one, "etag", SvcUtil.str(fs, "etag"));
                SvcUtil.put(one, "modified", SvcUtil.str(fs, "modified"));
                feeds.put(one);
            }
            SvcUtil.put(rss, "feeds", feeds);
            SvcUtil.put(out, "rss", rss);
        }
        if (take.contains("github")) {
            JSONObject sec = SvcUtil.obj(secrets, "github");
            JSONObject st = SvcUtil.obj(state, "github");
            JSONObject gh = new JSONObject();
            SvcUtil.put(gh, "enabled", SvcUtil.bool(SvcUtil.obj(svcs, "github"), "enabled", false));
            SvcUtil.put(gh, "token", SvcUtil.str(sec, "token"));
            SvcUtil.put(gh, "login", SvcUtil.str(sec, "login"));
            SvcUtil.put(gh, "since", SvcUtil.num(st, "since", 0));
            SvcUtil.put(gh, "last_modified", SvcUtil.str(st, "last_modified"));
            SvcUtil.put(out, "github", gh);
        }
        if (take.contains("imap")) {
            JSONObject sec = SvcUtil.obj(secrets, "imap");
            JSONObject st = SvcUtil.obj(state, "imap");
            JSONObject im = new JSONObject();
            SvcUtil.put(im, "enabled", SvcUtil.bool(SvcUtil.obj(svcs, "imap"), "enabled", false));
            for (String k : new String[]{"user", "password", "host", "folder"}) SvcUtil.put(im, k, SvcUtil.str(sec, k));
            SvcUtil.put(im, "port", (int) SvcUtil.num(sec, "port", 993));
            SvcUtil.put(im, "validity", SvcUtil.str(st, "validity"));
            SvcUtil.put(im, "last_uid", (long) SvcUtil.num(st, "last_uid", 0));
            SvcUtil.put(out, "imap", im);
        }
        if (take.contains("gmail")) {
            JSONObject sec = SvcUtil.obj(secrets, "google");
            JSONObject g = new JSONObject();
            SvcUtil.put(g, "enabled", SvcUtil.bool(SvcUtil.obj(svcs, "gmail"), "enabled", false));
            for (String k : new String[]{"client_id", "client_secret", "refresh_token", "email"}) {
                SvcUtil.put(g, k, SvcUtil.str(sec, k));
            }
            SvcUtil.put(g, "scopes", SvcUtil.arr(sec, "scopes"));
            Object hid = SvcUtil.obj(state, "gmail").opt("history_id");
            SvcUtil.put(g, "history_id", SvcUtil.isNull(hid) ? "" : (hid instanceof Number
                    ? SvcUtil.numStr((Number) hid) : hid.toString()).replaceAll("\\.0$", ""));
            SvcUtil.put(out, "google", g);
        }
        JSONArray rules = new JSONArray();
        JSONArray rr = SvcUtil.arr(settings, "rules");
        for (int i = 0; i < rr.length(); i++) {
            JSONObject r = rr.optJSONObject(i);
            JSONObject one = new JSONObject();
            SvcUtil.put(one, "action", SvcUtil.str(r, "action"));
            SvcUtil.put(one, "source", SvcUtil.str(r, "source"));
            SvcUtil.put(one, "match", SvcUtil.str(r, "match"));
            rules.put(one);
        }
        SvcUtil.put(out, "rules", rules);
        JSONObject n = SvcUtil.obj(settings, "notify");
        JSONObject notify = new JSONObject();
        if (!SvcUtil.str(n, "persona").isEmpty()) SvcUtil.put(notify, "persona", SvcUtil.str(n, "persona"));
        SvcUtil.put(notify, "quiet", SvcUtil.obj(n, "quiet"));
        SvcUtil.put(notify, "digest_times", SvcUtil.arr(n, "digest_times"));
        SvcUtil.put(notify, "max_per_hour", (int) SvcUtil.num(n, "max_per_hour", 6));
        SvcUtil.put(notify, "language", SvcUtil.str(n, "language").isEmpty() ? "auto" : SvcUtil.str(n, "language"));
        SvcUtil.put(out, "notify", notify);
        JSONArray items = SvcUtil.arr(feed, "items");
        JSONArray outItems = new JSONArray();
        for (int i = Math.max(0, items.length() - MAX_ITEMS); i < items.length(); i++) outItems.put(items.opt(i));
        SvcUtil.put(out, "items", outItems);
        JSONArray seen = SvcUtil.arr(feed, "seen");
        JSONArray outSeen = new JSONArray();
        for (int i = Math.max(0, seen.length() - 4000); i < seen.length(); i++) outSeen.put(seen.opt(i));
        SvcUtil.put(out, "seen_ids", outSeen);
        SvcUtil.put(out, "sources", names);
        return out;
    }
}
