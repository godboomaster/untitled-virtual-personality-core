package io.vpcore.app;

import org.json.JSONArray;
import org.json.JSONObject;

import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/**
 * Обвязка для scripts/test_phone_services.py: гоняет классы Svc* сервисов
 * телефона (web/android/.../io/vpcore/app/Svc*.java) обычной JVM — без
 * Android — по списку операций из JSON и пишет результаты в JSON. Тест
 * сравнивает их с тем, что на тех же данных делает ядро (app/integrations).
 *
 *   java -cp <json.jar>:<классы> io.vpcore.app.SvcHarness in.json out.json
 */
public final class SvcHarness {

    private SvcHarness() {
    }

    public static void main(String[] args) throws Exception {
        JSONArray ops = new JSONArray(new String(Files.readAllBytes(Paths.get(args[0])), StandardCharsets.UTF_8));
        JSONArray out = new JSONArray();
        for (int i = 0; i < ops.length(); i++) {
            JSONObject op = ops.getJSONObject(i);
            try {
                out.put(run(op));
            } catch (SvcSources.SourceError e) {
                JSONObject err = new JSONObject();
                err.put("__error", e.getMessage());
                err.put("__auth", e instanceof SvcSources.AuthError);
                out.put(err);
            } catch (Exception e) {
                JSONObject err = new JSONObject();
                err.put("__error", e.getClass().getSimpleName() + ": " + e.getMessage());
                err.put("__auth", false);
                out.put(err);
            }
        }
        Files.write(Paths.get(args[1]), out.toString().getBytes(StandardCharsets.UTF_8));
    }

    private static List<JSONObject> list(JSONArray a) {
        List<JSONObject> out = new ArrayList<>();
        for (int i = 0; a != null && i < a.length(); i++) out.add(a.getJSONObject(i));
        return out;
    }

    private static Object orNull(Object v) {
        return v == null ? JSONObject.NULL : v;
    }

    private static void endpoints(JSONObject op) {
        if (op.has("github_api")) SvcSources.githubApi = op.getString("github_api");
        if (op.has("token_url")) SvcSources.googleToken = op.getString("token_url");
        if (op.has("gmail_api")) SvcSources.gmailApi = op.getString("gmail_api");
    }

    private static final SvcImap.Opener PLAIN = Socket::new;

    static Object run(JSONObject op) throws Exception {
        endpoints(op);
        double now = op.optDouble("now", System.currentTimeMillis() / 1000.0);
        switch (op.getString("op")) {
            case "decide":
                return SvcTriage.decide(list(op.getJSONArray("items")), op.getJSONObject("settings"));
            case "heuristic": {
                Object[] h = SvcTriage.heuristic(op.getJSONObject("item"), op.getJSONObject("settings"));
                return new JSONArray().put(h[0]).put(h[1]).put(h[2]);
            }
            case "match": {
                JSONObject r = SvcTriage.matchRule(op.getJSONObject("item"), op.getJSONArray("rules"));
                return orNull(r == null ? null : r.getString("id"));
            }
            case "stem":
                return SvcTriage.stem(op.getString("word"));
            case "summary":
                return SvcTriage.fallbackSummary(op.getJSONObject("item"));
            case "notification":
                return SvcTriage.composeNotification(list(op.getJSONArray("items")), op.getString("lang"));
            case "digest":
                return SvcTriage.composeDigest(list(op.getJSONArray("items")), op.getString("lang"));
            case "quiet":
                return SvcTriage.inQuiet(op.getJSONObject("settings"), now);
            case "slot":
                return orNull(SvcTriage.lastDigestSlot(op.getJSONObject("settings"), now));
            case "accept": {
                List<JSONObject> items = list(op.getJSONArray("items"));
                SvcTriage.accept(op.getJSONObject("decisions"), items, now);
                return new JSONArray(items);
            }
            case "tick": {
                JSONObject feed = op.getJSONObject("feed");
                JSONObject nstate = op.getJSONObject("nstate");
                JSONArray deliveries = new JSONArray();
                JSONArray goneIds = op.optJSONArray("gone");
                JSONObject done = SvcTriage.tick(op.getJSONObject("settings"), feed, nstate, now, op.getString("lang"),
                        (text, items, kind) -> {
                            JSONArray ids = new JSONArray();
                            for (JSONObject it : items) ids.put(it.getString("id"));
                            deliveries.put(new JSONObject().put("text", text).put("ids", ids).put("kind", kind));
                            return true;
                        },
                        batch -> {
                            Set<String> g = new java.util.HashSet<>();
                            for (int i = 0; goneIds != null && i < goneIds.length(); i++) g.add(goneIds.getString(i));
                            return g;
                        });
                return new JSONObject().put("done", done).put("deliveries", deliveries).put("feed", feed).put("nstate", nstate);
            }
            case "record":
                return SvcTriage.record(op.getJSONObject("event"), now);
            case "ingest": {
                JSONObject feed = op.getJSONObject("feed");
                List<JSONObject> added = SvcTriage.ingest(feed, op.getJSONArray("events"), now);
                return new JSONObject().put("added", new JSONArray(added)).put("feed", feed);
            }
            case "normalize":
                return SvcTriage.normalizeSettings(op.opt("settings"));
            case "rule":
                return orNull(SvcTriage.normalizeRule(op.opt("rule")));
            case "event_id":
                return SvcUtil.eventId(op.getString("source"), op.getString("key"));
            case "feed": {
                byte[] body = SvcUtil.b64decode(op.getString("b64"));
                try {
                    return SvcParse.parseFeed(body);
                } catch (SvcParse.ParseError e) {
                    return new JSONObject().put("__error", e.getMessage()).put("__auth", false);
                }
            }
            case "rss_key":
                return SvcParse.rssKey(op.getString("url"), op.getString("id"));
            case "strip_html":
                return SvcUtil.stripHtml(op.getString("text"));
            case "unescape":
                return SvcUtil.unescape(op.getString("text"));
            case "date":
                return SvcUtil.feedDate(op.getString("text"));
            case "iso":
                return SvcUtil.isoTs(op.getString("text"));
            case "clip":
                return SvcUtil.clip(op.getString("text"), op.getInt("limit"));
            case "gmail_event":
                return orNull(SvcParse.gmailEvent(op.getJSONObject("msg"), op.optString("account", "")));
            case "gmail_body":
                return SvcParse.gmailBody(op.getJSONObject("payload"));
            case "mail_event":
                return SvcParse.mailEvent(SvcUtil.b64decode(op.getString("b64")), op.getString("key"), op.optString("flags", ""));
            case "decode_header":
                return SvcParse.decodeHeader(op.getString("text"));
            case "parseaddr": {
                String[] a = SvcParse.parseaddr(op.getString("text"));
                return new JSONArray().put(a[0]).put(a[1]);
            }
            case "html_url":
                return SvcParse.htmlUrl(op.getJSONObject("subject"), op.getString("repo"));
            case "github_event":
                return SvcParse.githubEvent(op.getJSONObject("thread"), op.optJSONObject("comment"));
            case "github_check":
                return SvcSources.githubCheck(op.getString("token"));
            case "github_poll": {
                JSONObject state = op.getJSONObject("state");
                JSONArray events = SvcSources.githubPoll(op.getString("token"), state, now);
                return new JSONObject().put("events", events).put("state", state);
            }
            case "github_gone":
                return new JSONArray(SvcSources.githubGone(op.getString("token"), list(op.getJSONArray("items"))));
            case "rss_check":
                return SvcSources.checkFeed(op.getString("url"));
            case "rss_poll": {
                JSONObject state = op.getJSONObject("state");
                JSONArray events = SvcSources.pollRss(op.getJSONArray("feeds"), state, now);
                return new JSONObject().put("events", events).put("state", state);
            }
            case "imap_normalize":
                return SvcImap.normalizeAccount(op.getJSONObject("acc"));
            case "imap_check":
                return SvcImap.check(op.getJSONObject("acc"), PLAIN);
            case "imap_poll": {
                JSONObject state = op.getJSONObject("state");
                JSONArray events = SvcImap.poll(op.getJSONObject("acc"), state, PLAIN);
                return new JSONObject().put("events", events).put("state", state);
            }
            case "gmail_poll": {
                JSONObject state = op.getJSONObject("state");
                JSONObject sec = op.getJSONObject("sec");
                JSONArray events = SvcSources.gmailPoll(sec, state, now);
                return new JSONObject().put("events", events).put("state", state).put("sec", sec);
            }
            case "gmail_gone":
                return new JSONArray(SvcSources.gmailGone(op.getJSONObject("sec"), list(op.getJSONArray("items")), now));
            case "parse_client":
                return SvcGoogleLogin.parseClient(op.optString("json_text", ""), op.optString("client_id", ""),
                        op.optString("client_secret", ""));
            case "google_url":
                return SvcGoogleLogin.loginUrl(op.getString("client_id"), op.getJSONObject("pending"));
            case "google_finish":
                return SvcGoogleLogin.finish(op.optJSONObject("pending"), op.getJSONObject("client"), op.optString("code", ""),
                        op.optString("state", ""), op.optString("error", ""), now);
            case "google_paste":
                return SvcGoogleLogin.finishFromUrl(op.optJSONObject("pending"), op.getJSONObject("client"),
                        op.getString("pasted"), now);
            case "bundle":
                return SvcTransfer.bundle(op.getJSONObject("settings"), op.getJSONObject("secrets"),
                        op.getJSONObject("state"), op.getJSONObject("feed"));
            case "google_serve": {
                // Вход целиком: свой сервер на 127.0.0.1, тест «браузером» шлёт код
                SvcGoogleLogin.Server server = SvcGoogleLogin.open();
                JSONObject pending = SvcGoogleLogin.pending(server.port(), now);
                JSONObject client = op.getJSONObject("client");
                String url = SvcGoogleLogin.loginUrl(client.getString("client_id"), pending);
                Files.write(Paths.get(op.getString("ready")), new JSONObject().put("url", url)
                        .put("port", server.port()).toString().getBytes(StandardCharsets.UTF_8));
                JSONObject[] result = new JSONObject[1];
                SvcGoogleLogin.serve(server, (code, state, error) -> {
                    result[0] = SvcGoogleLogin.finish(pending, client, code, state, error, System.currentTimeMillis() / 1000.0);
                    return result[0];
                }, "ru");
                return result[0] == null ? JSONObject.NULL : result[0];
            }
            default:
                throw new IllegalArgumentException("нет операции " + op.getString("op"));
        }
    }
}
