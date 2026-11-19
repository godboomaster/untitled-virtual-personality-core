package io.vpcore.app;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Calendar;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Сервисы на телефоне: разбор событий и уведомитель — порт ядра
 * (app/integrations/triage.py, notifier.py, feed.py, store.py) без модели.
 *
 * Разбор — как у ядра с engine «rules»: правила пользователя (первое
 * совпавшее решает), потом признаки источника; модели на телефоне нет, и
 * неуверенный признак остаётся подсказкой. Тексты — шаблон ядра (style
 * plain), на русском или английском.
 *
 * Уведомитель — как Notifier.tick ядра, но без окна склейки: телефон
 * проверяет сервисы раз в 15–30 минут, и всё найденное за проверку и так
 * уходит одним сообщением. Тихие часы, «не беспокоить», лимит в час,
 * «устаревшее — в сводку», сводка по расписанию — те же.
 *
 * Паритет с ядром проверяет scripts/test_phone_services.py.
 */
final class SvcTriage {

    private SvcTriage() {
    }

    static final List<String> LEVELS = Arrays.asList("urgent", "notify", "digest", "ignore");
    static final List<String> SERVICES = Arrays.asList("gmail", "drive", "github", "telegram", "imap", "rss", "webhook");
    static final List<String> RULE_ACTIONS = Arrays.asList("urgent", "always", "digest", "never");
    // Источники, которые телефон проверяет сам; остальные — только на компьютере
    static final List<String> PHONE_SOURCES = Arrays.asList("rss", "github", "imap", "gmail");

    static final double STALE_SEC = 6 * 3600;
    static final int MAX_LINES = 8;
    static final int DIGEST_PER_SOURCE = 4;
    static final int MAX_ITEMS = 200;
    static final int MAX_SEEN = 2000;
    static final int MAX_RULES = 200;
    static final int MAX_FEEDS = 50;

    private static final Map<String, String> RULE_LEVEL = new HashMap<>();

    static {
        RULE_LEVEL.put("urgent", "urgent");
        RULE_LEVEL.put("always", "notify");
        RULE_LEVEL.put("digest", "digest");
        RULE_LEVEL.put("never", "ignore");
    }

    private static final Pattern NOREPLY = Pattern.compile(
            "no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?@|newsletter|mailer-daemon|news@|info@|digest@|marketing",
            Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE);
    private static final Pattern CI_FAIL = Pattern.compile("\\bfail|\\berror|\\bcancel|упал|ошибк",
            Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE);
    private static final Pattern WORDS4 = Pattern.compile("[\\p{L}\\p{N}_]{4,}");
    private static final Pattern HHMM = Pattern.compile("^([01]?\\d|2[0-3]):([0-5]\\d)$");
    private static final Pattern RULE_ID = Pattern.compile("^[a-f0-9]{8,32}$");
    private static final Pattern FEED_URL = Pattern.compile("^https?://[^\\t\\n\\x0B\\f\\r ]+$");

    // Окончания — app/core/word_stem.py
    private static final String[] ENDINGS = {
            "ого", "его", "ому", "ему", "ыми", "ими", "ами", "ями",
            "ая", "яя", "ое", "ее", "ой", "ей", "ый", "ий", "ых", "их",
            "ом", "ем", "ам", "ям", "ах", "ях", "ов", "ев", "ью",
            "а", "я", "у", "ю", "о", "е", "ы", "и", "ь", "s",
    };

    static String stem(String word) {
        for (String e : ENDINGS) {
            if (word.endsWith(e) && SvcUtil.cpLen(word) - SvcUtil.cpLen(e) >= 4) {
                return word.substring(0, word.length() - e.length());
            }
        }
        return word;
    }

    static int rank(String level) {
        int i = LEVELS.indexOf(level);
        return i < 0 ? 9 : i;
    }

    // ── Настройки телефона ─────────────────────────────────────────────

    static JSONObject defaults() {
        try {
            return new JSONObject("{\"notify\":{\"persona\":null,\"interests\":\"\","
                    + "\"quiet\":{\"enabled\":true,\"from\":\"23:00\",\"to\":\"08:00\",\"urgent\":true},"
                    + "\"digest_times\":[\"09:00\",\"19:00\"],\"max_per_hour\":6,\"language\":\"auto\"},"
                    + "\"services\":{\"rss\":{\"enabled\":false,\"feeds\":[]},\"github\":{\"enabled\":false},"
                    + "\"imap\":{\"enabled\":false},\"gmail\":{\"enabled\":false},"
                    + "\"telegram\":{\"muted\":\"digest\"}},"
                    + "\"rules\":[],\"dnd_until\":0,\"interval_min\":15,\"moved\":{}}");
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** _merge ядра: недостающее — из умолчаний, лишнее — прочь, типы — как у умолчания. */
    static JSONObject merge(JSONObject defaults, Object data) {
        JSONObject out = SvcUtil.copy(defaults);
        if (!(data instanceof JSONObject)) return out;
        JSONObject d = (JSONObject) data;
        java.util.Iterator<String> keys = defaults.keys();
        while (keys.hasNext()) {
            String key = keys.next();
            if (!d.has(key)) continue;
            Object dv = defaults.opt(key);
            Object v = d.opt(key);
            if (dv instanceof JSONObject) {
                JSONObject dj = (JSONObject) dv;
                if (dj.length() > 0) SvcUtil.put(out, key, merge(dj, v));
                else SvcUtil.put(out, key, v instanceof JSONObject ? SvcUtil.copy((JSONObject) v) : new JSONObject());
            } else if (dv instanceof Boolean) {
                if (v instanceof Boolean) SvcUtil.put(out, key, v);
            } else if (dv instanceof Number) {
                if (v instanceof Number) SvcUtil.put(out, key, v);
            } else if (dv instanceof JSONArray) {
                if (v instanceof JSONArray) SvcUtil.put(out, key, v);
            } else if (SvcUtil.isNull(dv) || dv instanceof String) {
                if (SvcUtil.isNull(v) || v instanceof String) SvcUtil.put(out, key, v);
            }
        }
        return out;
    }

    static String hhmm(Object value) {
        Matcher m = HHMM.matcher(SvcUtil.strip(SvcUtil.isNull(value) ? "" : value.toString()));
        if (!m.matches()) return null;
        return String.format(java.util.Locale.ROOT, "%02d:%s", Integer.parseInt(m.group(1)), m.group(2));
    }

    static JSONObject normalizeSettings(Object data) {
        JSONObject s = merge(defaults(), data);
        JSONObject n = s.optJSONObject("notify");
        String lang = SvcUtil.str(n, "language");
        if (!Arrays.asList("auto", "ru", "en").contains(lang)) SvcUtil.put(n, "language", "auto");
        SvcUtil.put(n, "interests", SvcUtil.head(SvcUtil.str(n, "interests"), 2000));
        SvcUtil.put(n, "max_per_hour", Math.max(1, Math.min(60, (int) SvcUtil.num(n, "max_per_hour", 6))));
        JSONObject q = n.optJSONObject("quiet");
        String from = hhmm(q.opt("from"));
        String to = hhmm(q.opt("to"));
        SvcUtil.put(q, "from", from == null ? "23:00" : from);
        SvcUtil.put(q, "to", to == null ? "08:00" : to);
        java.util.TreeSet<String> times = new java.util.TreeSet<>();
        JSONArray dt = n.optJSONArray("digest_times");
        for (int i = 0; dt != null && i < dt.length(); i++) {
            String t = hhmm(dt.opt(i));
            if (t != null) times.add(t);
        }
        JSONArray outTimes = new JSONArray();
        for (String t : times) if (outTimes.length() < 6) outTimes.put(t);
        SvcUtil.put(n, "digest_times", outTimes);
        String persona = SvcUtil.str(n, "persona");
        SvcUtil.put(n, "persona", persona.matches("^[A-Za-z0-9_-]{1,64}$") ? persona : null);

        JSONObject svcs = s.optJSONObject("services");
        JSONObject rss = svcs.optJSONObject("rss");
        JSONArray feeds = new JSONArray();
        Set<String> seenUrls = new HashSet<>();
        JSONArray raw = rss.optJSONArray("feeds");
        for (int i = 0; raw != null && i < raw.length(); i++) {
            Object f = raw.opt(i);
            String url = SvcUtil.strip(f instanceof JSONObject ? SvcUtil.str((JSONObject) f, "url")
                    : (SvcUtil.isNull(f) ? "" : f.toString()));
            if (FEED_URL.matcher(url).matches() && seenUrls.add(url) && feeds.length() < MAX_FEEDS) {
                JSONObject one = new JSONObject();
                SvcUtil.put(one, "url", SvcUtil.head(url, 500));
                SvcUtil.put(one, "title", f instanceof JSONObject ? SvcUtil.head(SvcUtil.str((JSONObject) f, "title"), 120) : "");
                feeds.put(one);
            }
        }
        SvcUtil.put(rss, "feeds", feeds);
        JSONObject tg = svcs.optJSONObject("telegram");
        if (!Arrays.asList("digest", "ignore", "normal").contains(SvcUtil.str(tg, "muted"))) SvcUtil.put(tg, "muted", "digest");

        JSONArray rules = new JSONArray();
        JSONArray rr = s.optJSONArray("rules");
        for (int i = 0; rr != null && i < rr.length() && rules.length() < MAX_RULES; i++) {
            JSONObject r = normalizeRule(rr.opt(i));
            if (r != null) rules.put(r);
        }
        SvcUtil.put(s, "rules", rules);
        SvcUtil.put(s, "dnd_until", SvcUtil.num(s, "dnd_until", 0));
        int interval = (int) SvcUtil.num(s, "interval_min", 15);
        SvcUtil.put(s, "interval_min", interval >= 30 ? 30 : 15);
        // Куда перенесено: {сервис: когда} — только сервисы телефона
        JSONObject moved = new JSONObject();
        JSONObject mv = s.optJSONObject("moved");
        for (String name : PHONE_SOURCES) {
            double ts = SvcUtil.num(mv, name, 0);
            if (ts > 0) SvcUtil.put(moved, name, ts);
        }
        SvcUtil.put(s, "moved", moved);
        return s;
    }

    /** normalize_rule ядра. */
    static JSONObject normalizeRule(Object raw) {
        if (!(raw instanceof JSONObject)) return null;
        JSONObject rule = (JSONObject) raw;
        String action = SvcUtil.str(rule, "action");
        String match = SvcUtil.head(SvcUtil.collapse(SvcUtil.str(rule, "match")), 200);
        String source = SvcUtil.str(rule, "source");
        if (source.isEmpty()) source = "any";
        if (!RULE_ACTIONS.contains(action) || match.isEmpty()) return null;
        if (!SERVICES.contains(source) && !source.equals("any")) source = "any";
        String rid = SvcUtil.str(rule, "id");
        if (!RULE_ID.matcher(rid).matches()) rid = SvcUtil.randomHex(6);
        double created = SvcUtil.num(rule, "created_at", 0);
        JSONObject out = new JSONObject();
        SvcUtil.put(out, "id", rid);
        SvcUtil.put(out, "action", action);
        SvcUtil.put(out, "source", source);
        SvcUtil.put(out, "match", match);
        SvcUtil.put(out, "created_at", created > 0 ? created : System.currentTimeMillis() / 1000.0);
        return out;
    }

    // ── Разбор ──────────────────────────────────────────────────────────

    static String haystack(JSONObject item) {
        JSONObject meta = SvcUtil.obj(item, "meta");
        String[] parts = {SvcUtil.str(item, "sender"), SvcUtil.str(item, "group"), SvcUtil.str(item, "title"),
                SvcUtil.str(meta, "email"), SvcUtil.str(meta, "chat"), SvcUtil.str(meta, "repo"),
                SvcUtil.str(meta, "username"), SvcUtil.str(meta, "feed")};
        StringBuilder sb = new StringBuilder();
        for (String p : parts) {
            if (p.isEmpty()) continue;
            if (sb.length() > 0) sb.append(" | ");
            sb.append(p);
        }
        return SvcUtil.lower(sb.toString());
    }

    static boolean contains(String needle, String hay) {
        if (hay.contains(needle)) return true;
        Matcher m = WORDS4.matcher(needle);
        boolean any = false;
        while (m.find()) {
            any = true;
            if (!hay.contains(stem(m.group()))) return false;
        }
        return any;
    }

    static JSONObject matchRule(JSONObject item, JSONArray rules) {
        String hay = haystack(item);
        String text = SvcUtil.lower(SvcUtil.str(item, "text"));
        String source = SvcUtil.str(item, "source");
        for (int i = 0; rules != null && i < rules.length(); i++) {
            JSONObject rule = rules.optJSONObject(i);
            if (rule == null) continue;
            String rs = SvcUtil.str(rule, "source");
            if (!rs.equals("any") && !rs.equals(source)) continue;
            String needle = SvcUtil.strip(SvcUtil.lower(SvcUtil.str(rule, "match")));
            if (!needle.isEmpty() && (contains(needle, hay)
                    || (SvcUtil.cpLen(needle) >= 4 && contains(needle, text)))) {
                return rule;
            }
        }
        return null;
    }

    /** (уровень, уверенно, причина) по признакам источника — heuristic ядра. */
    static Object[] heuristic(JSONObject item, JSONObject settings) {
        String src = SvcUtil.str(item, "source");
        JSONObject meta = SvcUtil.obj(item, "meta");
        String kind = SvcUtil.str(item, "kind");
        String sender = SvcUtil.str(item, "sender") + " " + SvcUtil.str(meta, "email");
        switch (src) {
            case "gmail": {
                Set<String> labels = new HashSet<>();
                JSONArray la = meta.optJSONArray("labels");
                for (int i = 0; la != null && i < la.length(); i++) labels.add(String.valueOf(la.opt(i)));
                if (labels.contains("SPAM") || labels.contains("TRASH")) return r("ignore", true, "spam");
                if (labels.contains("CATEGORY_PROMOTIONS")) return r("ignore", true, "promotions");
                if (labels.contains("CATEGORY_SOCIAL") || labels.contains("CATEGORY_FORUMS")) {
                    return r("digest", true, "social/forums");
                }
                if (labels.contains("CATEGORY_UPDATES") || NOREPLY.matcher(sender).find()) {
                    return r("digest", false, "automated");
                }
                return r("notify", false, "personal mail");
            }
            case "imap":
                if (SvcUtil.truthy(meta.opt("bulk")) || NOREPLY.matcher(sender).find()) return r("digest", false, "automated");
                return r("notify", false, "mail");
            case "drive":
                if (kind.equals("shared")) return r("notify", false, "shared with you");
                return r("digest", false, "edited");
            case "github": {
                String reason = SvcUtil.str(meta, "reason");
                if (reason.equals("security_alert")) return r("urgent", false, "security alert");
                if (reason.equals("ci_activity")) {
                    if (CI_FAIL.matcher(SvcUtil.str(item, "title")).find()) return r("notify", false, "CI failed");
                    return r("digest", true, "CI");
                }
                if (Arrays.asList("review_requested", "mention", "team_mention", "assign", "author", "comment",
                        "invitation", "approval_requested").contains(reason)) {
                    return r("notify", false, reason);
                }
                return r("digest", true, reason.isEmpty() ? "subscribed" : reason);
            }
            case "telegram": {
                JSONObject tg = SvcUtil.obj(SvcUtil.obj(settings, "services"), "telegram");
                if (kind.equals("channel")) return r("digest", true, "channel post");
                if (SvcUtil.truthy(meta.opt("muted")) && !SvcUtil.truthy(meta.opt("mentioned"))) {
                    String mode = tg.has("muted") ? SvcUtil.str(tg, "muted") : "digest";
                    if (mode.equals("ignore")) return r("ignore", true, "muted chat");
                    if (mode.equals("digest")) return r("digest", true, "muted chat");
                }
                if (kind.equals("group")) return r("digest", false, "group chat");
                if (kind.equals("mention")) return r("notify", false, "mention");
                return r("notify", false, "private message");
            }
            case "rss": {
                String interests = SvcUtil.strip(SvcUtil.str(SvcUtil.obj(settings, "notify"), "interests"));
                return r("digest", interests.isEmpty(), "feed");
            }
            case "webhook": {
                String prio = SvcUtil.lower(SvcUtil.str(meta, "priority"));
                if (Arrays.asList("urgent", "critical", "max", "5").contains(prio)) return r("urgent", true, "priority");
                if (Arrays.asList("high", "4").contains(prio)) return r("notify", true, "priority");
                if (Arrays.asList("low", "min", "1", "2").contains(prio)) return r("digest", true, "priority");
                return r("notify", false, "webhook");
            }
            default:
                return r("digest", false, "");
        }
    }

    private static Object[] r(String level, boolean sure, String why) {
        return new Object[]{level, sure, why};
    }

    static String fallbackSummary(JSONObject item) {
        String src = SvcUtil.str(item, "source");
        String title = SvcUtil.strip(SvcUtil.str(item, "title"));
        String text = SvcUtil.collapse(SvcUtil.str(item, "text"));
        if (src.equals("webhook") && !title.isEmpty() && !text.isEmpty() && !title.equals("Webhook")) {
            text = title + ": " + text;
        }
        if ((src.equals("telegram") || src.equals("webhook")) && !text.isEmpty()) {
            return SvcUtil.cpLen(text) <= 200 ? text : SvcUtil.rstrip(SvcUtil.head(text, 199)) + "…";
        }
        return !title.isEmpty() ? title : SvcUtil.head(text, 200);
    }

    /** decide ядра без модели: {id: {level, summary, reason, by}}. */
    static JSONObject decide(List<JSONObject> items, JSONObject settings) {
        JSONArray rules = settings.optJSONArray("rules");
        JSONObject out = new JSONObject();
        for (JSONObject item : items) {
            JSONObject d = new JSONObject();
            JSONObject rule = matchRule(item, rules);
            if (rule != null) {
                SvcUtil.put(d, "level", RULE_LEVEL.get(SvcUtil.str(rule, "action")));
                SvcUtil.put(d, "reason", "rule: " + SvcUtil.str(rule, "match"));
                SvcUtil.put(d, "by", "rule");
            } else {
                Object[] h = heuristic(item, settings);
                SvcUtil.put(d, "level", h[0]);
                SvcUtil.put(d, "reason", h[2]);
                SvcUtil.put(d, "by", "signals");
            }
            SvcUtil.put(d, "summary", fallbackSummary(item));
            SvcUtil.put(out, SvcUtil.str(item, "id"), d);
        }
        return out;
    }

    // ── Лента ───────────────────────────────────────────────────────────

    /** Event.record() ядра. */
    static JSONObject record(JSONObject ev, double now) {
        JSONObject rec = new JSONObject();
        String source = SvcUtil.str(ev, "source");
        SvcUtil.put(rec, "id", SvcUtil.eventId(source, SvcUtil.str(ev, "key")));
        SvcUtil.put(rec, "source", source);
        SvcUtil.put(rec, "kind", SvcUtil.str(ev, "kind"));
        SvcUtil.put(rec, "title", SvcUtil.clip(SvcUtil.str(ev, "title"), 300));
        SvcUtil.put(rec, "sender", SvcUtil.clip(SvcUtil.str(ev, "sender"), 200));
        SvcUtil.put(rec, "text", SvcUtil.clip(SvcUtil.str(ev, "text"), 2000));
        SvcUtil.put(rec, "url", SvcUtil.head(SvcUtil.str(ev, "url"), 1000));
        double ts = SvcUtil.num(ev, "ts", 0);
        SvcUtil.put(rec, "ts", ts != 0 ? ts : now);
        SvcUtil.put(rec, "received", now);
        SvcUtil.put(rec, "meta", SvcUtil.copy(ev.optJSONObject("meta")));
        SvcUtil.put(rec, "group", SvcUtil.clip(SvcUtil.str(ev, "group"), 200));
        SvcUtil.put(rec, "level", null);
        SvcUtil.put(rec, "summary", "");
        SvcUtil.put(rec, "reason", "");
        SvcUtil.put(rec, "by", "");
        SvcUtil.put(rec, "status", "new");
        SvcUtil.put(rec, "notified_at", null);
        SvcUtil.put(rec, "feedback", null);
        return rec;
    }

    /** feed.add ядра: новые записи (повторы по id — прочь). feed — {items, seen}. */
    static List<JSONObject> ingest(JSONObject feed, JSONArray events, double now) {
        JSONArray items = feed.optJSONArray("items");
        if (items == null) {
            items = new JSONArray();
            SvcUtil.put(feed, "items", items);
        }
        JSONArray seen = feed.optJSONArray("seen");
        if (seen == null) {
            seen = new JSONArray();
            SvcUtil.put(feed, "seen", seen);
        }
        Set<String> known = new HashSet<>();
        for (int i = 0; i < seen.length(); i++) known.add(seen.optString(i));
        for (int i = 0; i < items.length(); i++) known.add(SvcUtil.str(items.optJSONObject(i), "id"));
        List<JSONObject> added = new ArrayList<>();
        for (int i = 0; events != null && i < events.length(); i++) {
            JSONObject rec = record(events.optJSONObject(i), now);
            String id = SvcUtil.str(rec, "id");
            if (!known.add(id)) continue;
            items.put(rec);
            seen.put(id);
            added.add(rec);
        }
        trim(feed);
        return added;
    }

    /** Feed._save ядра: сначала срезается то, что уже не ждёт действий. */
    static void trim(JSONObject feed) {
        JSONArray items = feed.optJSONArray("items");
        if (items != null && items.length() > MAX_ITEMS) {
            Set<String> waiting = new HashSet<>(Arrays.asList("new", "queued", "digest"));
            int drop = items.length() - MAX_ITEMS;
            List<JSONObject> keep = new ArrayList<>();
            for (int i = 0; i < items.length(); i++) {
                JSONObject it = items.optJSONObject(i);
                if (drop > 0 && !waiting.contains(SvcUtil.str(it, "status"))) {
                    drop--;
                    continue;
                }
                keep.add(it);
            }
            JSONArray out = new JSONArray();
            for (int i = Math.max(0, keep.size() - MAX_ITEMS); i < keep.size(); i++) out.put(keep.get(i));
            SvcUtil.put(feed, "items", out);
        }
        JSONArray seen = feed.optJSONArray("seen");
        if (seen != null && seen.length() > MAX_SEEN) {
            JSONArray out = new JSONArray();
            for (int i = seen.length() - MAX_SEEN; i < seen.length(); i++) out.put(seen.opt(i));
            SvcUtil.put(feed, "seen", out);
        }
    }

    static List<JSONObject> withStatus(JSONObject feed, String... statuses) {
        List<String> want = Arrays.asList(statuses);
        List<JSONObject> out = new ArrayList<>();
        JSONArray items = feed.optJSONArray("items");
        for (int i = 0; items != null && i < items.length(); i++) {
            JSONObject it = items.optJSONObject(i);
            if (it != null && want.contains(SvcUtil.str(it, "status"))) out.add(it);
        }
        return out;
    }

    /** Notifier.accept ядра: решения — в записи (устаревшее — в сводку). */
    static void accept(JSONObject decisions, List<JSONObject> items, double now) {
        for (JSONObject item : items) {
            JSONObject d = decisions.optJSONObject(SvcUtil.str(item, "id"));
            if (d == null) continue;
            String level = SvcUtil.str(d, "level");
            String reason = SvcUtil.str(d, "reason");
            double ts = SvcUtil.num(item, "ts", 0);
            if ((level.equals("urgent") || level.equals("notify")) && now - (ts != 0 ? ts : now) > STALE_SEC) {
                level = "digest";
                reason = (reason.isEmpty() ? "" : reason + "; ") + "stale";
            }
            String status = level.equals("digest") ? "digest" : level.equals("ignore") ? "ignored" : "queued";
            SvcUtil.put(item, "level", level);
            SvcUtil.put(item, "summary", SvcUtil.str(d, "summary"));
            SvcUtil.put(item, "reason", reason);
            SvcUtil.put(item, "by", SvcUtil.str(d, "by"));
            SvcUtil.put(item, "status", status);
        }
    }

    // ── Время ───────────────────────────────────────────────────────────

    private static int minutes(String hhmm) {
        String[] p = hhmm.split(":");
        return Integer.parseInt(p[0]) * 60 + Integer.parseInt(p[1]);
    }

    static boolean inQuiet(JSONObject settings, double now) {
        JSONObject q = SvcUtil.obj(SvcUtil.obj(settings, "notify"), "quiet");
        if (!SvcUtil.bool(q, "enabled", false)) return false;
        Calendar c = Calendar.getInstance();
        c.setTimeInMillis((long) (now * 1000));
        int cur = c.get(Calendar.HOUR_OF_DAY) * 60 + c.get(Calendar.MINUTE);
        int start = minutes(SvcUtil.str(q, "from"));
        int end = minutes(SvcUtil.str(q, "to"));
        if (start == end) return false;
        if (start < end) return start <= cur && cur < end;
        return cur >= start || cur < end;
    }

    static boolean dndActive(JSONObject settings, double now) {
        return SvcUtil.num(settings, "dnd_until", 0) > now;
    }

    /** Последний момент сводки по расписанию ≤ now (сегодня или вчера); null — нет. */
    static Double lastDigestSlot(JSONObject settings, double now) {
        JSONArray times = SvcUtil.obj(settings, "notify").optJSONArray("digest_times");
        if (times == null || times.length() == 0) return null;
        Double best = null;
        for (int shift = 0; shift <= 1; shift++) {
            Calendar day = Calendar.getInstance();
            day.setTimeInMillis((long) (now * 1000));
            day.add(Calendar.DAY_OF_MONTH, -shift);
            for (int i = 0; i < times.length(); i++) {
                String[] hm = times.optString(i).split(":");
                Calendar c = Calendar.getInstance();
                c.clear();
                c.set(day.get(Calendar.YEAR), day.get(Calendar.MONTH), day.get(Calendar.DAY_OF_MONTH),
                        Integer.parseInt(hm[0]), Integer.parseInt(hm[1]), 0);
                double slot = c.getTimeInMillis() / 1000.0;
                if (slot <= now && (best == null || slot > best)) best = slot;
            }
            if (best != null) break;
        }
        return best;
    }

    // ── Тексты ──────────────────────────────────────────────────────────

    private static final Map<String, Map<String, String>> LABELS = new HashMap<>();
    private static final Map<String, Map<String, String>> T = new HashMap<>();

    static {
        Map<String, String> ru = new HashMap<>();
        ru.put("gmail", "Почта");
        ru.put("imap", "Почта");
        ru.put("drive", "Google Диск");
        ru.put("github", "GitHub");
        ru.put("telegram", "Telegram");
        ru.put("rss", "Ленты");
        ru.put("webhook", "Сервис");
        Map<String, String> en = new HashMap<>();
        en.put("gmail", "Mail");
        en.put("imap", "Mail");
        en.put("drive", "Google Drive");
        en.put("github", "GitHub");
        en.put("telegram", "Telegram");
        en.put("rss", "Feeds");
        en.put("webhook", "Service");
        LABELS.put("ru", ru);
        LABELS.put("en", en);
        Map<String, String> tru = new HashMap<>();
        tru.put("urgent", "Срочно");
        tru.put("new", "Новое ({n})");
        tru.put("more", "…и ещё {n}");
        tru.put("digest", "Сводка: {n} {events}");
        tru.put("digest_more", "…и ещё {n}");
        tru.put("feed", "Всё — в разделе «Сервисы».");
        tru.put("in", "в");
        tru.put("test", "Проверка уведомлений: всё работает.");
        tru.put("auth", "Не могу проверить {name}: доступ истёк или отозван. Подключите заново в разделе «Сервисы».");
        Map<String, String> ten = new HashMap<>();
        ten.put("urgent", "Urgent");
        ten.put("new", "New ({n})");
        ten.put("more", "…and {n} more");
        ten.put("digest", "Digest: {n} {events}");
        ten.put("digest_more", "…and {n} more");
        ten.put("feed", "Everything is in Services.");
        ten.put("in", "in");
        ten.put("test", "Notification check: everything works.");
        ten.put("auth", "I can't check {name}: access expired or was revoked. Reconnect it in Services.");
        T.put("ru", tru);
        T.put("en", ten);
    }

    static String tr(String lang) {
        return "en".equals(lang) ? "en" : "ru";
    }

    static String text(String lang, String key) {
        return T.get(tr(lang)).get(key);
    }

    /** Имя сервиса в сообщении «доступ пропал» (hub._SERVICE_NAMES). */
    static String serviceName(String lang, String account) {
        boolean ru = tr(lang).equals("ru");
        switch (account) {
            case "google":
                return ru ? "почту Google" : "Google mail";
            case "imap":
                return ru ? "почту" : "mail";
            case "rss":
                return ru ? "ленты" : "feeds";
            case "github":
                return "GitHub";
            default:
                return account;
        }
    }

    private static String plural(int n, String lang) {
        if (tr(lang).equals("en")) return n == 1 ? "event" : "events";
        if (n % 10 == 1 && n % 100 != 11) return "событие";
        if (n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14)) return "события";
        return "событий";
    }

    private static String who(JSONObject item, String lang) {
        String sender = SvcUtil.strip(SvcUtil.str(item, "sender"));
        String group = SvcUtil.strip(SvcUtil.str(item, "group"));
        String src = SvcUtil.str(item, "source");
        if (src.equals("telegram") && !group.isEmpty() && !group.equals(sender)) {
            return !sender.isEmpty() ? sender + " " + text(lang, "in") + " «" + group + "»" : "«" + group + "»";
        }
        if ((src.equals("github") || src.equals("rss")) && !group.isEmpty()) return group;
        return !sender.isEmpty() ? sender : group;
    }

    private static String label(JSONObject item, String lang) {
        String src = SvcUtil.str(item, "source");
        String l = LABELS.get(tr(lang)).get(src);
        return l == null ? src : l;
    }

    private static String summaryOf(JSONObject item) {
        String s = SvcUtil.str(item, "summary");
        if (s.isEmpty()) s = SvcUtil.str(item, "title");
        return SvcUtil.strip(s);
    }

    static String line(JSONObject item, String lang) {
        String w = who(item, lang);
        String summary = summaryOf(item);
        String head = w.isEmpty() ? label(item, lang) : label(item, lang) + " · " + w;
        return summary.isEmpty() ? head : head + ": " + summary;
    }

    private static boolean urgent(JSONObject it) {
        return "urgent".equals(SvcUtil.str(it, "level"));
    }

    static String composeNotification(List<JSONObject> items, String lang) {
        lang = tr(lang);
        boolean anyUrgent = false;
        for (JSONObject it : items) anyUrgent |= urgent(it);
        if (items.size() == 1) {
            JSONObject it = items.get(0);
            String t = line(it, lang);
            if (anyUrgent) t = text(lang, "urgent") + " — " + t;
            String url = SvcUtil.str(it, "url");
            if (!url.isEmpty()) t += "\n" + url;
            return t;
        }
        List<JSONObject> sorted = new ArrayList<>(items);
        Collections.sort(sorted, (a, b) -> Integer.compare(urgent(a) ? 0 : 1, urgent(b) ? 0 : 1));
        String head = text(lang, "new").replace("{n}", String.valueOf(sorted.size()));
        if (anyUrgent) head = text(lang, "urgent") + " — " + head;
        StringBuilder sb = new StringBuilder(head);
        for (int i = 0; i < Math.min(MAX_LINES, sorted.size()); i++) {
            JSONObject it = sorted.get(i);
            String mark = urgent(it) && sorted.size() > 1 ? "! " : "";
            sb.append("\n— ").append(mark).append(line(it, lang));
            String url = SvcUtil.str(it, "url");
            if (!url.isEmpty() && sorted.size() <= 3) sb.append("\n  ").append(url);
        }
        if (sorted.size() > MAX_LINES) {
            sb.append("\n").append(text(lang, "more").replace("{n}", String.valueOf(sorted.size() - MAX_LINES)));
        }
        return sb.toString();
    }

    static String composeDigest(List<JSONObject> items, String lang) {
        lang = tr(lang);
        Map<String, List<JSONObject>> bySrc = new LinkedHashMap<>();
        for (JSONObject it : items) {
            String l = label(it, lang);
            List<JSONObject> g = bySrc.get(l);
            if (g == null) {
                g = new ArrayList<>();
                bySrc.put(l, g);
            }
            g.add(it);
        }
        List<String> parts = new ArrayList<>();
        parts.add(text(lang, "digest").replace("{n}", String.valueOf(items.size()))
                .replace("{events}", plural(items.size(), lang)));
        List<Map.Entry<String, List<JSONObject>>> groups = new ArrayList<>(bySrc.entrySet());
        Collections.sort(groups, (a, b) -> Integer.compare(b.getValue().size(), a.getValue().size()));
        for (Map.Entry<String, List<JSONObject>> e : groups) {
            List<JSONObject> group = e.getValue();
            StringBuilder rows = new StringBuilder();
            for (int i = group.size() - 1; i >= Math.max(0, group.size() - DIGEST_PER_SOURCE); i--) {
                JSONObject it = group.get(i);
                String w = who(it, lang);
                String summary = summaryOf(it);
                if (rows.length() > 0) rows.append("\n");
                rows.append(w.isEmpty() ? "— " + summary : "— " + w + ": " + summary);
            }
            if (group.size() > DIGEST_PER_SOURCE) {
                rows.append("\n").append(text(lang, "digest_more")
                        .replace("{n}", String.valueOf(group.size() - DIGEST_PER_SOURCE)));
            }
            parts.add(e.getKey() + " (" + group.size() + "):\n" + rows);
        }
        parts.add(text(lang, "feed"));
        StringBuilder sb = new StringBuilder();
        for (String p : parts) {
            if (sb.length() > 0) sb.append("\n\n");
            sb.append(p);
        }
        return sb.toString();
    }

    // ── Тик уведомителя ─────────────────────────────────────────────────

    interface Deliver {
        /** true — доставлено, false — нет (повтор при следующей проверке). */
        boolean deliver(String text, List<JSONObject> items, String kind);
    }

    interface Gone {
        /** id тех, о ком сообщать уже не нужно (прочитано в самом сервисе). */
        Set<String> gone(List<JSONObject> batch);
    }

    /** Notifier.tick ядра без окна склейки. nstate — {sent: [...], last_digest}. → {notified, digested, skipped}. */
    static JSONObject tick(JSONObject settings, JSONObject feed, JSONObject nstate, double now, String lang,
                           Deliver deliver, Gone gone) {
        JSONObject done = new JSONObject();
        JSONArray notified = new JSONArray();
        JSONArray digested = new JSONArray();
        JSONArray skipped = new JSONArray();
        SvcUtil.put(done, "notified", notified);
        SvcUtil.put(done, "digested", digested);
        SvcUtil.put(done, "skipped", skipped);
        boolean quiet = inQuiet(settings, now) || dndActive(settings, now);
        boolean urgentOk = !quiet || SvcUtil.bool(SvcUtil.obj(SvcUtil.obj(settings, "notify"), "quiet"), "urgent", true);
        List<Double> sent = new ArrayList<>();
        JSONArray sa = nstate.optJSONArray("sent");
        for (int i = 0; sa != null && i < sa.length(); i++) {
            double t = sa.optDouble(i, 0);
            if (now - t < 3600) sent.add(t);
        }
        int limit = (int) SvcUtil.num(SvcUtil.obj(settings, "notify"), "max_per_hour", 6);

        List<JSONObject> queued = withStatus(feed, "queued");
        List<JSONObject> batch = new ArrayList<>();
        List<JSONObject> normal = new ArrayList<>();
        for (JSONObject q : queued) {
            if (urgent(q)) {
                if (urgentOk) batch.add(q);
            } else {
                normal.add(q);
            }
        }
        if (!normal.isEmpty() && !quiet && sent.size() < limit) batch.addAll(normal);
        if (!batch.isEmpty() && gone != null) {
            Set<String> g;
            try {
                g = gone.gone(batch);
            } catch (RuntimeException e) {
                g = Collections.emptySet();
            }
            if (g != null && !g.isEmpty()) {
                List<JSONObject> keep = new ArrayList<>();
                for (JSONObject b : batch) {
                    if (g.contains(SvcUtil.str(b, "id"))) {
                        SvcUtil.put(b, "status", "skipped");
                        SvcUtil.put(b, "reason", "read elsewhere");
                        skipped.put(SvcUtil.str(b, "id"));
                    } else {
                        keep.add(b);
                    }
                }
                batch = keep;
            }
        }
        if (!batch.isEmpty()) {
            Collections.sort(batch, (a, b) -> Double.compare(SvcUtil.num(a, "received", 0), SvcUtil.num(b, "received", 0)));
            if (deliver.deliver(composeNotification(batch, lang), batch, "notify")) {
                for (JSONObject b : batch) {
                    SvcUtil.put(b, "status", "notified");
                    SvcUtil.put(b, "notified_at", now);
                    notified.put(SvcUtil.str(b, "id"));
                }
                sent.add(now);
            }
        }
        Double slot = lastDigestSlot(settings, now);
        if (slot != null && !quiet && SvcUtil.num(nstate, "last_digest", 0) < slot) {
            List<JSONObject> pending = withStatus(feed, "digest");
            if (!pending.isEmpty()) {
                if (deliver.deliver(composeDigest(pending, lang), pending, "digest")) {
                    for (JSONObject p : pending) {
                        SvcUtil.put(p, "status", "digested");
                        SvcUtil.put(p, "notified_at", now);
                        digested.put(SvcUtil.str(p, "id"));
                    }
                    SvcUtil.put(nstate, "last_digest", now);
                }
            } else {
                SvcUtil.put(nstate, "last_digest", now);
            }
        }
        JSONArray outSent = new JSONArray();
        for (double t : sent) outSent.put(Double.valueOf(t));
        SvcUtil.put(nstate, "sent", outSent);
        return done;
    }

    /** «Прислать сводку сейчас»: ждущее сводки и отправки — одним сообщением. */
    static JSONObject digestNow(JSONObject feed, double now, String lang, Deliver deliver) {
        List<JSONObject> pending = withStatus(feed, "digest", "queued");
        JSONObject out = new JSONObject();
        SvcUtil.put(out, "sent", 0);
        if (pending.isEmpty()) return out;
        if (deliver.deliver(composeDigest(pending, lang), pending, "digest")) {
            for (JSONObject p : pending) {
                SvcUtil.put(p, "status", "digested");
                SvcUtil.put(p, "notified_at", now);
            }
            SvcUtil.put(out, "sent", pending.size());
        }
        return out;
    }
}
