package io.vpcore.app;

import org.json.JSONArray;
import org.json.JSONObject;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import javax.xml.parsers.DocumentBuilder;
import javax.xml.parsers.DocumentBuilderFactory;

/**
 * Сервисы на телефоне: разбор ответов источников — порт ядра
 * (app/integrations/sources: rss.parse_feed, github._event/html_url,
 * google.message_event/message_body, imap_mail.parse_message). Событие —
 * JSONObject как Event ядра: {source, key, kind, title, sender, text, url,
 * ts, meta, group}.
 */
final class SvcParse {

    private SvcParse() {
    }

    static class ParseError extends Exception {
        ParseError(String msg) {
            super(msg);
        }
    }

    static JSONObject event(String source, String key, String kind, String title, String sender, String text,
                            String url, double ts, JSONObject meta, String group) {
        JSONObject e = new JSONObject();
        SvcUtil.put(e, "source", source);
        SvcUtil.put(e, "key", key);
        SvcUtil.put(e, "kind", kind);
        SvcUtil.put(e, "title", title);
        SvcUtil.put(e, "sender", sender);
        SvcUtil.put(e, "text", text);
        SvcUtil.put(e, "url", url);
        SvcUtil.put(e, "ts", ts);
        SvcUtil.put(e, "meta", meta == null ? new JSONObject() : meta);
        SvcUtil.put(e, "group", group);
        return e;
    }

    // ── RSS и Atom ──────────────────────────────────────────────────────

    // DOCTYPE не нужен лентам и опасен (сущности из DTD) — вырезается до разбора
    private static final Pattern DOCTYPE = Pattern.compile("(?is)<!DOCTYPE[^\\[>]*(\\[.*?\\])?\\s*>");

    private static String local(Node n) {
        String name = n.getLocalName();
        if (name == null) {
            name = n.getNodeName();
            int i = name.indexOf(':');
            if (i >= 0) name = name.substring(i + 1);
        }
        return name.toLowerCase(Locale.ROOT);
    }

    private static Element child(Element el, String... names) {
        if (el == null) return null;
        NodeList kids = el.getChildNodes();
        for (int i = 0; i < kids.getLength(); i++) {
            Node k = kids.item(i);
            if (k.getNodeType() != Node.ELEMENT_NODE) continue;
            String l = local(k);
            for (String n : names) if (n.equals(l)) return (Element) k;
        }
        return null;
    }

    /** .text ElementTree: текст до первого дочернего элемента. */
    private static String ownText(Element el) {
        StringBuilder sb = new StringBuilder();
        NodeList kids = el.getChildNodes();
        for (int i = 0; i < kids.getLength(); i++) {
            Node k = kids.item(i);
            short t = k.getNodeType();
            if (t == Node.ELEMENT_NODE) break;
            if (t == Node.TEXT_NODE || t == Node.CDATA_SECTION_NODE) sb.append(k.getNodeValue());
        }
        return sb.toString();
    }

    private static String text(Element el, String... names) {
        Element ch = child(el, names);
        return ch == null ? "" : SvcUtil.strip(ownText(ch));
    }

    private static List<Element> children(Element el) {
        List<Element> out = new ArrayList<>();
        NodeList kids = el.getChildNodes();
        for (int i = 0; i < kids.getLength(); i++) {
            if (kids.item(i).getNodeType() == Node.ELEMENT_NODE) out.add((Element) kids.item(i));
        }
        return out;
    }

    private static Document parseXml(byte[] content) throws ParseError {
        try {
            DocumentBuilderFactory f = DocumentBuilderFactory.newInstance();
            f.setNamespaceAware(true);
            f.setExpandEntityReferences(false);
            for (String feature : new String[]{"http://apache.org/xml/features/disallow-doctype-decl",
                    "http://xml.org/sax/features/external-general-entities",
                    "http://xml.org/sax/features/external-parameter-entities"}) {
                try {
                    f.setFeature(feature, feature.endsWith("doctype-decl"));
                } catch (Exception ignored) {
                    // парсер Android знает не все признаки — DOCTYPE и так вырезан
                }
            }
            byte[] body = content;
            String head = new String(content, 0, Math.min(content.length, 4096), StandardCharsets.ISO_8859_1);
            Matcher m = DOCTYPE.matcher(head);
            if (m.find()) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                out.write(content, 0, m.start());
                out.write(content, m.end(), content.length - m.end());
                body = out.toByteArray();
            }
            DocumentBuilder b = f.newDocumentBuilder();
            b.setErrorHandler(null);
            return b.parse(new ByteArrayInputStream(body));
        } catch (Exception e) {
            throw new ParseError("rss: не XML (" + e.getClass().getSimpleName() + ")");
        }
    }

    /** rss.parse_feed ядра: {title, items: [{id, title, link, summary, ts}]}. */
    static JSONObject parseFeed(byte[] content) throws ParseError {
        Element root = parseXml(content).getDocumentElement();
        String kind = local(root);
        JSONArray items = new JSONArray();
        String title;
        if (kind.equals("feed")) {
            title = text(root, "title");
            for (Element entry : children(root)) {
                if (!local(entry).equals("entry")) continue;
                String link = "";
                for (Element ch : children(entry)) {
                    String rel = ch.hasAttribute("rel") ? ch.getAttribute("rel") : "alternate";
                    if (local(ch).equals("link") && rel.equals("alternate")) {
                        link = ch.getAttribute("href");
                        break;
                    }
                }
                String summary = text(entry, "summary");
                if (summary.isEmpty()) summary = text(entry, "content");
                String id = text(entry, "id");
                String date = text(entry, "published");
                if (date.isEmpty()) date = text(entry, "updated");
                items.put(feedItem(id.isEmpty() ? link : id, text(entry, "title"), link, summary,
                        SvcUtil.feedDate(date)));
            }
        } else {
            Element channel = child(root, "channel");
            title = channel != null ? text(channel, "title") : "";
            List<Element> parents = new ArrayList<>();
            if (channel != null) parents.add(channel);
            parents.add(root);
            for (Element parent : parents) {
                for (Element item : children(parent)) {
                    if (!local(item).equals("item")) continue;
                    String link = text(item, "link");
                    String id = text(item, "guid");
                    if (id.isEmpty()) id = link;
                    if (id.isEmpty()) id = text(item, "title");
                    items.put(feedItem(id, text(item, "title"), link, text(item, "description", "encoded"),
                            SvcUtil.feedDate(text(item, "pubdate", "date"))));
                }
            }
        }
        JSONArray out = new JSONArray();
        for (int i = 0; i < items.length(); i++) {
            JSONObject it = items.optJSONObject(i);
            SvcUtil.put(it, "summary", SvcUtil.head(SvcUtil.stripHtml(SvcUtil.str(it, "summary")), 800));
            if (!SvcUtil.str(it, "id").isEmpty()) out.put(it);
        }
        JSONObject res = new JSONObject();
        SvcUtil.put(res, "title", title);
        SvcUtil.put(res, "items", out);
        return res;
    }

    private static JSONObject feedItem(String id, String title, String link, String summary, double ts) {
        JSONObject o = new JSONObject();
        SvcUtil.put(o, "id", id);
        SvcUtil.put(o, "title", title);
        SvcUtil.put(o, "link", link);
        SvcUtil.put(o, "summary", summary);
        SvcUtil.put(o, "ts", ts);
        return o;
    }

    /** _key ленты: тот же у ядра — перенос «уже видел» на компьютер. */
    static String rssKey(String url, String itemId) {
        return SvcUtil.sha1Hex(url + "\n" + itemId).substring(0, 20);
    }

    // ── GitHub ──────────────────────────────────────────────────────────

    static final List<String> COMMENT_REASONS = java.util.Arrays.asList(
            "mention", "team_mention", "comment", "author", "review_requested", "assign", "manual");
    private static final Pattern API_SUBJECT = Pattern.compile(
            "^https://api\\.github\\.com/repos/([^/]+/[^/]+)/(pulls|issues|commits|releases)/([^/]+)$");

    static String htmlUrl(JSONObject subject, String repo) {
        String url = SvcUtil.str(subject, "url");
        Matcher m = API_SUBJECT.matcher(url);
        if (m.matches()) {
            String kind = m.group(2);
            if (kind.equals("releases")) return "https://github.com/" + m.group(1) + "/releases";
            String k = kind.equals("pulls") ? "pull" : kind.equals("commits") ? "commit" : "issues";
            return "https://github.com/" + m.group(1) + "/" + k + "/" + m.group(3);
        }
        String type = SvcUtil.str(subject, "type");
        if (type.equals("CheckSuite")) return "https://github.com/" + repo + "/actions";
        if (type.equals("RepositoryVulnerabilityAlert")) return "https://github.com/" + repo + "/security";
        if (type.equals("Discussion")) return "https://github.com/" + repo + "/discussions";
        return !repo.isEmpty() ? "https://github.com/" + repo : "https://github.com/notifications";
    }

    /** _event GitHub; comment — ответ на latest_comment_url (или null). */
    static JSONObject githubEvent(JSONObject th, JSONObject comment) {
        JSONObject subject = SvcUtil.obj(th, "subject");
        String repo = SvcUtil.str(SvcUtil.obj(th, "repository"), "full_name");
        String reason = SvcUtil.str(th, "reason");
        String sender = "";
        String text = "";
        String url = htmlUrl(subject, repo);
        if (comment != null) {
            sender = SvcUtil.str(SvcUtil.obj(comment, "user"), "login");
            text = SvcUtil.head(SvcUtil.str(comment, "body"), 1500);
            String h = SvcUtil.str(comment, "html_url");
            if (!h.isEmpty()) url = h;
        }
        Object id = th.opt("id");
        String idStr = SvcUtil.isNull(id) ? "None" : (id instanceof Number ? SvcUtil.numStr((Number) id) : id.toString());
        Object updated = th.opt("updated_at");
        String updStr = SvcUtil.isNull(updated) ? "None" : updated.toString();
        JSONObject meta = new JSONObject();
        SvcUtil.put(meta, "reason", reason);
        SvcUtil.put(meta, "type", subject.has("type") ? subject.opt("type") : null);
        SvcUtil.put(meta, "repo", repo);
        SvcUtil.put(meta, "thread", idStr);
        return event("github", idStr + ":" + updStr, reason.isEmpty() ? "update" : reason,
                SvcUtil.isNull(subject.opt("title")) ? "" : subject.opt("title").toString(), sender, text, url,
                SvcUtil.isoTs(SvcUtil.isNull(updated) ? null : updated.toString()), meta, repo);
    }

    /** Нужен ли текст комментария для этого уведомления. */
    static String commentUrl(JSONObject th) {
        JSONObject subject = SvcUtil.obj(th, "subject");
        String cu = SvcUtil.str(subject, "latest_comment_url");
        if (!cu.isEmpty() && COMMENT_REASONS.contains(SvcUtil.str(th, "reason"))
                && !cu.equals(SvcUtil.str(subject, "url"))) {
            return cu;
        }
        return null;
    }

    // ── Gmail ───────────────────────────────────────────────────────────

    private static final Pattern QUOTE = Pattern.compile(
            "\\n(?:>|On .{5,80} wrote:|\\d{1,2}\\.\\d{1,2}\\.\\d{2,4}.{0,60}пишет:)");

    private static String b64text(String data) {
        return new String(SvcUtil.b64decode(data), StandardCharsets.UTF_8);
    }

    private static void walk(JSONObject part, List<String> plain, List<String> rich) {
        String mime = SvcUtil.str(part, "mimeType");
        String data = SvcUtil.str(SvcUtil.obj(part, "body"), "data");
        if (!data.isEmpty() && mime.equals("text/plain")) plain.add(b64text(data));
        else if (!data.isEmpty() && mime.equals("text/html")) rich.add(b64text(data));
        JSONArray parts = part.optJSONArray("parts");
        for (int i = 0; parts != null && i < parts.length(); i++) {
            JSONObject sub = parts.optJSONObject(i);
            if (sub != null) walk(sub, plain, rich);
        }
    }

    private static String join(List<String> parts) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < parts.size(); i++) {
            if (i > 0) sb.append("\n");
            sb.append(parts.get(i));
        }
        return sb.toString();
    }

    /** google.message_body ядра. */
    static String gmailBody(JSONObject payload) {
        List<String> plain = new ArrayList<>();
        List<String> rich = new ArrayList<>();
        walk(payload == null ? new JSONObject() : payload, plain, rich);
        String text = SvcUtil.strip(join(plain));
        if (text.isEmpty()) text = SvcUtil.stripHtml(join(rich));
        Matcher m = QUOTE.matcher(text);
        if (m.find()) text = text.substring(0, m.start());
        return SvcUtil.strip(SvcUtil.head(text, 4000));
    }

    static String gmailLink(String threadId, String account) {
        String auth = account.isEmpty() ? "" : "?authuser=" + quote(account);
        return "https://mail.google.com/mail/u/0/" + auth + "#inbox/" + threadId;
    }

    /** urllib.parse.quote(s) (safe="/"). */
    static String quote(String s) {
        StringBuilder sb = new StringBuilder();
        for (byte b : s.getBytes(StandardCharsets.UTF_8)) {
            int c = b & 0xff;
            if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
                    || c == '_' || c == '.' || c == '-' || c == '~' || c == '/') {
                sb.append((char) c);
            } else {
                sb.append('%').append(String.format("%02X", c));
            }
        }
        return sb.toString();
    }

    /** google.message_event ядра; null — не письмо во «Входящих». */
    static JSONObject gmailEvent(JSONObject msg, String account) {
        JSONArray la = msg.optJSONArray("labelIds");
        JSONArray labels = la == null ? new JSONArray() : la;
        List<String> ls = new ArrayList<>();
        for (int i = 0; i < labels.length(); i++) ls.add(String.valueOf(labels.opt(i)));
        if (!ls.contains("INBOX") || ls.contains("SENT") || ls.contains("DRAFT")) return null;
        JSONObject payload = SvcUtil.obj(msg, "payload");
        Map<String, String> h = new LinkedHashMap<>();
        JSONArray headers = payload.optJSONArray("headers");
        for (int i = 0; headers != null && i < headers.length(); i++) {
            JSONObject x = headers.optJSONObject(i);
            if (x == null) continue;
            h.put(SvcUtil.lower(SvcUtil.str(x, "name")), SvcUtil.isNull(x.opt("value")) ? "" : x.opt("value").toString());
        }
        String[] na = parseaddr(get(h, "from"));
        String name = na[0];
        String email = na[1];
        String subject = SvcUtil.strip(get(h, "subject"));
        String body = gmailBody(payload);
        if (body.isEmpty()) body = SvcUtil.str(msg, "snippet");
        String id = SvcUtil.str(msg, "id");
        String thread = SvcUtil.str(msg, "threadId");
        double internal = SvcUtil.num(msg, "internalDate", 0) / 1000;
        double ts = internal != 0 ? internal : mailTs(get(h, "date"));
        JSONObject meta = new JSONObject();
        SvcUtil.put(meta, "labels", new JSONArray(ls));
        SvcUtil.put(meta, "email", SvcUtil.lower(email));
        SvcUtil.put(meta, "unread", ls.contains("UNREAD"));
        SvcUtil.put(meta, "message_id", id);
        String prec = get(h, "precedence");
        SvcUtil.put(meta, "bulk", !get(h, "list-unsubscribe").isEmpty() || prec.equals("bulk") || prec.equals("list"));
        SvcUtil.put(meta, "thread", msg.has("threadId") ? msg.opt("threadId") : null);
        return event("gmail", id, "message", subject.isEmpty() ? "(без темы)" : subject,
                name.isEmpty() ? email : name, body, gmailLink(thread.isEmpty() ? id : thread, account), ts, meta,
                SvcUtil.lower(email));
    }

    private static String get(Map<String, String> h, String key) {
        String v = h.get(key);
        return v == null ? "" : v;
    }

    private static double mailTs(String value) {
        Double t = SvcUtil.rfc2822(value);
        return t == null ? System.currentTimeMillis() / 1000.0 : t;
    }

    /** email.utils.parseaddr для обычных форм: «Имя <адрес>», «адрес», «адрес (Имя)». */
    static String[] parseaddr(String value) {
        String v = SvcUtil.strip(value == null ? "" : value);
        if (v.isEmpty()) return new String[]{"", ""};
        int lt = v.lastIndexOf('<');
        int gt = v.lastIndexOf('>');
        if (lt >= 0 && gt > lt) {
            String addr = SvcUtil.strip(v.substring(lt + 1, gt));
            String name = SvcUtil.strip(v.substring(0, lt));
            if (name.length() >= 2 && name.startsWith("\"") && name.endsWith("\"")) {
                name = name.substring(1, name.length() - 1).replace("\\\"", "\"").replace("\\\\", "\\");
            }
            return new String[]{name, addr};
        }
        int lp = v.indexOf('(');
        int rp = v.lastIndexOf(')');
        if (lp > 0 && rp > lp) {
            return new String[]{SvcUtil.strip(v.substring(lp + 1, rp)), SvcUtil.strip(v.substring(0, lp))};
        }
        if (v.contains(" ") && !v.contains("@")) return new String[]{"", ""};
        return new String[]{"", v};
    }

    // ── Письмо целиком (IMAP) ───────────────────────────────────────────

    /** Заголовки и тело одной части письма. */
    static final class Mime {
        final List<String[]> headers = new ArrayList<>();
        byte[] body = new byte[0];

        String header(String name) {
            for (String[] h : headers) if (h[0].equalsIgnoreCase(name)) return h[1];
            return null;
        }

        String contentType() {
            String ct = header("content-type");
            if (ct == null) return "text/plain";
            String t = SvcUtil.lower(SvcUtil.strip(ct.split(";", 2)[0]));
            int slashes = 0;
            for (int i = 0; i < t.length(); i++) if (t.charAt(i) == '/') slashes++;
            return slashes == 1 ? t : "text/plain";
        }

        String param(String name) {
            String ct = header("content-type");
            return ct == null ? null : headerParam(ct, name);
        }

        String charset() {
            String c = param("charset");
            return c == null ? null : SvcUtil.lower(c);
        }

        /** part.get_filename() Python: имя файла — вложение. */
        boolean isAttachment() {
            String cd = header("content-disposition");
            String fn = cd == null ? null : headerParam(cd, "filename");
            if (fn == null) fn = param("name");
            return fn != null && !SvcUtil.strip(fn).isEmpty();
        }
    }

    static String headerParam(String header, String name) {
        Matcher m = Pattern.compile("(?i);\\s*" + Pattern.quote(name) + "\\*?\\s*=\\s*(\"((?:[^\"\\\\]|\\\\.)*)\"|[^;\\s]+)")
                .matcher(header);
        if (!m.find()) return null;
        return m.group(2) != null ? m.group(2).replaceAll("\\\\(.)", "$1") : m.group(1);
    }

    private static int indexOf(byte[] data, byte[] pat, int from) {
        outer:
        for (int i = Math.max(0, from); i <= data.length - pat.length; i++) {
            for (int j = 0; j < pat.length; j++) if (data[i + j] != pat[j]) continue outer;
            return i;
        }
        return -1;
    }

    /** Разбор части: заголовки (развёрнутые) до пустой строки, дальше тело. */
    static Mime parseMime(byte[] raw) {
        Mime m = new Mime();
        int end = -1;
        int bodyAt = raw.length;
        int lineStart = 0;
        // Заголовки кончаются первой пустой строкой
        for (int i = 0; i < raw.length; i++) {
            if (raw[i] == '\n') {
                int len = i - lineStart;
                if (len == 0 || (len == 1 && raw[lineStart] == '\r')) {
                    end = lineStart;
                    bodyAt = i + 1;
                    break;
                }
                lineStart = i + 1;
            }
        }
        if (end < 0) end = raw.length;
        // Строки заголовков с их переводами строк: как Compat32 Python,
        // перенесённый заголовок хранит перевод строки внутри значения
        List<String> lines = new ArrayList<>();
        int s = 0;
        for (int i = 0; i < end; i++) {
            if (raw[i] == '\n') {
                lines.add(new String(raw, s, i + 1 - s, StandardCharsets.ISO_8859_1));
                s = i + 1;
            }
        }
        if (s < end) lines.add(new String(raw, s, end - s, StandardCharsets.ISO_8859_1));
        String name = null;
        StringBuilder value = new StringBuilder();
        for (String line : lines) {
            if (line.isEmpty()) continue;
            if ((line.charAt(0) == ' ' || line.charAt(0) == '\t') && name != null) {
                value.append(line);
                continue;
            }
            if (name != null) m.headers.add(new String[]{name, value.toString().replaceAll("[\\r\\n]+$", "")});
            int c = line.indexOf(':');
            if (c <= 0) {
                name = null;
                continue;
            }
            name = line.substring(0, c).trim();
            value = new StringBuilder(line.substring(c + 1).replaceFirst("^[ \\t]+", ""));
        }
        if (name != null) m.headers.add(new String[]{name, value.toString().replaceAll("[\\r\\n]+$", "")});
        // Заголовки в 8 битах (UTF-8 без кодирования) — как их видит Python
        for (String[] h : m.headers) h[1] = new String(h[1].getBytes(StandardCharsets.ISO_8859_1), StandardCharsets.UTF_8);
        m.body = bodyAt >= raw.length ? new byte[0] : java.util.Arrays.copyOfRange(raw, bodyAt, raw.length);
        return m;
    }

    /** Части multipart (вложенные — тоже), как msg.walk() Python. */
    static void walkMime(Mime m, List<Mime> out, int depth) {
        out.add(m);
        if (depth > 8 || !m.contentType().startsWith("multipart/")) return;
        String boundary = m.param("boundary");
        if (boundary == null || boundary.isEmpty()) return;
        byte[] delim = ("--" + boundary).getBytes(StandardCharsets.ISO_8859_1);
        byte[] b = m.body;
        List<int[]> spans = new ArrayList<>();
        int pos = 0;
        int partStart = -1;
        while (true) {
            int at = indexOf(b, delim, pos);
            if (at < 0) break;
            boolean lineStart = at == 0 || b[at - 1] == '\n';
            if (!lineStart) {
                pos = at + 1;
                continue;
            }
            int after = at + delim.length;
            boolean closing = after + 1 < b.length && b[after] == '-' && b[after + 1] == '-';
            if (partStart >= 0) {
                // Перевод строки перед разделителем принадлежит разделителю
                int stop = at;
                if (stop > 0 && b[stop - 1] == '\n') stop--;
                if (stop > 0 && b[stop - 1] == '\r') stop--;
                spans.add(new int[]{partStart, Math.max(partStart, stop)});
            }
            if (closing) break;
            int nl = after;
            while (nl < b.length && b[nl] != '\n') nl++;
            partStart = Math.min(b.length, nl + 1);
            pos = partStart;
        }
        for (int[] s : spans) walkMime(parseMime(java.util.Arrays.copyOfRange(b, s[0], s[1])), out, depth + 1);
    }

    static byte[] decodeBody(Mime m) {
        String cte = m.header("content-transfer-encoding");
        cte = cte == null ? "" : SvcUtil.lower(SvcUtil.strip(cte));
        if (cte.equals("base64")) return SvcUtil.b64decode(new String(m.body, StandardCharsets.ISO_8859_1));
        if (cte.equals("quoted-printable")) return qpDecode(m.body, false);
        return m.body;
    }

    static byte[] qpDecode(byte[] in, boolean header) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        for (int i = 0; i < in.length; i++) {
            int c = in[i] & 0xff;
            if (header && c == '_') {
                out.write(' ');
            } else if (c == '=') {
                if (i + 1 < in.length && in[i + 1] == '\n') {
                    i += 1;
                } else if (i + 2 < in.length && in[i + 1] == '\r' && in[i + 2] == '\n') {
                    i += 2;
                } else if (i + 2 < in.length && hex(in[i + 1]) >= 0 && hex(in[i + 2]) >= 0) {
                    out.write(hex(in[i + 1]) * 16 + hex(in[i + 2]));
                    i += 2;
                } else if (i == in.length - 1) {
                    // «=» в конце — мягкий перенос без перевода строки
                } else {
                    out.write(c);
                }
            } else {
                out.write(c);
            }
        }
        return out.toByteArray();
    }

    private static int hex(byte b) {
        int c = b & 0xff;
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        return -1;
    }

    static Charset charset(String name) {
        try {
            return Charset.forName(name);
        } catch (Exception e) {
            return null;
        }
    }

    // ecre из email.header Python
    private static final Pattern ENCODED_WORD = Pattern.compile("=\\?([^?]*?)\\?([qQbB])\\?(.*?)\\?=");
    private static final Pattern LINES = Pattern.compile("\\r\\n|[\\n\\r\\x0B\\x0C\\x1C\\x1D\\x1E\\x85\\u2028\\u2029]");
    private static final Pattern Q_HEX = Pattern.compile("=([a-fA-F0-9]{2})");

    private static String lstrip(String s) {
        int a = 0;
        while (a < s.length() && SvcUtil.isPySpace(s.charAt(a))) a++;
        return s.substring(a);
    }

    private static boolean nonctext(char c) {
        return SvcUtil.isPySpace(c) || c == '(' || c == ')' || c == '\\';
    }

    private static boolean ascii(String cs) {
        return cs == null || cs.equals("us-ascii");
    }

    /** str(make_header(decode_header(value))) Python; не разобралось — value как есть. */
    static String decodeHeader(String value) {
        if (value == null || value.isEmpty()) return "";
        if (!ENCODED_WORD.matcher(value).find()) return value;
        // Слова: {текст, кодировка (null — без), charset}
        List<String[]> words = new ArrayList<>();
        for (String line : LINES.split(value, -1)) {
            Matcher m = ENCODED_WORD.matcher(line);
            int pos = 0;
            boolean first = true;
            while (m.find()) {
                String unenc = line.substring(pos, m.start());
                if (first) {
                    unenc = lstrip(unenc);
                    first = false;
                }
                if (!unenc.isEmpty()) words.add(new String[]{unenc, null, null});
                words.add(new String[]{m.group(3), SvcUtil.lower(m.group(2)), SvcUtil.lower(m.group(1))});
                pos = m.end();
            }
            String tail = line.substring(pos);
            if (first) tail = lstrip(tail);
            if (!tail.isEmpty()) words.add(new String[]{tail, null, null});
        }
        // Пробелы между двумя кодированными словами — прочь
        List<Integer> drop = new ArrayList<>();
        for (int n = 2; n < words.size(); n++) {
            if (words.get(n)[1] != null && words.get(n - 2)[1] != null
                    && SvcUtil.strip(words.get(n - 1)[0]).isEmpty() && !words.get(n - 1)[0].isEmpty()) {
                drop.add(n - 1);
            }
        }
        for (int i = drop.size() - 1; i >= 0; i--) words.remove((int) drop.get(i));
        // Декодирование и склейка подряд идущих с одним charset
        List<Object[]> collapsed = new ArrayList<>();
        ByteArrayOutputStream last = null;
        String lastCs = null;
        for (String[] w : words) {
            byte[] word;
            if (w[1] == null) {
                word = w[0].getBytes(StandardCharsets.ISO_8859_1);
            } else if (w[1].equals("q")) {
                Matcher q = Q_HEX.matcher(w[0].replace('_', ' '));
                StringBuffer sb = new StringBuffer();
                while (q.find()) q.appendReplacement(sb, Matcher.quoteReplacement(
                        String.valueOf((char) Integer.parseInt(q.group(1), 16))));
                q.appendTail(sb);
                word = sb.toString().getBytes(StandardCharsets.ISO_8859_1);
            } else {
                word = SvcUtil.b64decode(w[0]);
            }
            String cs = w[2];
            if (last == null) {
                last = new ByteArrayOutputStream();
                last.write(word, 0, word.length);
                lastCs = cs;
            } else if (cs == null ? lastCs != null : !cs.equals(lastCs)) {
                collapsed.add(new Object[]{last.toByteArray(), lastCs});
                last = new ByteArrayOutputStream();
                last.write(word, 0, word.length);
                lastCs = cs;
            } else if (lastCs == null) {
                last.write(' ');
                last.write(word, 0, word.length);
            } else {
                last.write(word, 0, word.length);
            }
        }
        if (last != null) collapsed.add(new Object[]{last.toByteArray(), lastCs});
        // Header.append: байты → текст (простой текст — us-ascii); _normalize:
        // соседние куски одной кодировки — через пробел
        List<String[]> chunks = new ArrayList<>();
        for (Object[] c : collapsed) {
            String cs = c[1] == null ? "us-ascii" : (String) c[1];
            Charset charset = charset(cs);
            if (charset == null) return value;
            String s;
            try {
                s = charset.newDecoder()
                        .onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
                        .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPORT)
                        .decode(java.nio.ByteBuffer.wrap((byte[]) c[0])).toString();
            } catch (Exception e) {
                return value;
            }
            if (!chunks.isEmpty() && chunks.get(chunks.size() - 1)[1].equals(cs)) {
                String[] prev = chunks.get(chunks.size() - 1);
                prev[0] = prev[0] + " " + s;
            } else {
                chunks.add(new String[]{s, cs});
            }
        }
        // Header.__str__: пробел на границе кодированного и простого текста
        StringBuilder out = new StringBuilder();
        String prevCs = null;
        boolean prevSpace = false;
        boolean firstChunk = true;
        for (String[] c : chunks) {
            String s = c[0];
            String cs = c[1];
            String next = ascii(cs) ? null : cs;
            if (!firstChunk) {
                boolean hasSpace = !s.isEmpty() && nonctext(s.charAt(0));
                if (!ascii(prevCs)) {
                    if (next == null && !hasSpace) out.append(' ');
                } else if (next != null && !prevSpace) {
                    out.append(' ');
                }
            }
            prevSpace = !s.isEmpty() && nonctext(s.charAt(s.length() - 1));
            prevCs = next;
            out.append(s);
            firstChunk = false;
        }
        return out.toString();
    }

    /** imap_mail._body ядра. */
    static String mailBody(Mime msg) {
        List<Mime> parts = new ArrayList<>();
        walkMime(msg, parts, 0);
        List<String> plain = new ArrayList<>();
        List<String> rich = new ArrayList<>();
        for (Mime p : parts) {
            String ct = p.contentType();
            if (ct.startsWith("multipart/") || p.isAttachment()) continue;
            String cs = p.charset();
            Charset c = charset(cs == null ? "utf-8" : cs);
            if (c == null) continue;
            String text = new String(decodeBody(p), c);
            if (ct.equals("text/plain")) plain.add(text);
            else if (ct.equals("text/html")) rich.add(text);
        }
        String text = SvcUtil.strip(join(plain));
        if (text.isEmpty()) text = SvcUtil.stripHtml(join(rich));
        return SvcUtil.strip(SvcUtil.head(text, 4000));
    }

    /** imap_mail.parse_message ядра. */
    static JSONObject mailEvent(byte[] raw, String key, String flags) {
        Mime msg = parseMime(raw);
        String[] na = parseaddr(decodeHeader(msg.header("from")));
        String name = na[0];
        String addr = na[1];
        Double ts = SvcUtil.rfc2822(msg.header("date"));
        String subject = SvcUtil.strip(decodeHeader(msg.header("subject")));
        JSONObject meta = new JSONObject();
        SvcUtil.put(meta, "email", SvcUtil.lower(addr));
        SvcUtil.put(meta, "unread", !flags.contains("\\Seen"));
        String prec = msg.header("precedence");
        prec = prec == null ? "" : SvcUtil.lower(prec);
        SvcUtil.put(meta, "bulk", (msg.header("list-unsubscribe") != null && !msg.header("list-unsubscribe").isEmpty())
                || prec.equals("bulk") || prec.equals("list"));
        return event("imap", key, "message", subject.isEmpty() ? "(без темы)" : subject,
                name.isEmpty() ? addr : name, mailBody(msg), "", ts == null ? 0.0 : ts, meta, SvcUtil.lower(addr));
    }
}
