package io.vpcore.app;

import org.json.JSONArray;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Calendar;
import java.util.HashMap;
import java.util.Map;
import java.util.TimeZone;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Сервисы на телефоне без компьютера (PhoneServices): общие мелочи, чтобы
 * порт с ядра (app/integrations) вёл себя как Python — пробелы как у
 * str.isspace(), длина и срезы по символам, а не по UTF-16, даты как
 * email.utils.parsedate_to_datetime и datetime.fromisoformat, HTML-сущности
 * как html.unescape.
 *
 * Без android.*: классы Svc* собираются и проверяются обычным javac
 * (scripts/test_phone_services.py). Только API Android 7: ни java.time, ни
 * java.util.Base64.
 */
final class SvcUtil {

    private SvcUtil() {
    }

    // ── JSON: одинаково для org.json Android и org.json с Maven ─────────

    static boolean isNull(Object v) {
        return v == null || v == JSONObject.NULL;
    }

    /** str(x or "") Python: нет ключа, null, false, пустое — "". */
    static String str(JSONObject o, String key) {
        if (o == null) return "";
        Object v = o.opt(key);
        if (isNull(v)) return "";
        if (v instanceof Boolean) return ((Boolean) v) ? "True" : "";
        if (v instanceof Number) {
            double d = ((Number) v).doubleValue();
            if (d == 0) return "";
            return numStr((Number) v);
        }
        return v.toString();
    }

    /** Число как str() Python: 5 → "5", 5.5 → "5.5". */
    static String numStr(Number n) {
        if (n instanceof Integer || n instanceof Long) return n.toString();
        double d = n.doubleValue();
        if (d == Math.rint(d) && Math.abs(d) < 1e15) {
            // JSON «5» org.json отдаёт целым, «5.0» — дробным: как Python
            return n instanceof Double || n instanceof Float ? ((long) d) + ".0" : Long.toString((long) d);
        }
        return Double.toString(d);
    }

    static double num(JSONObject o, String key, double def) {
        if (o == null) return def;
        Object v = o.opt(key);
        if (v instanceof Number) return ((Number) v).doubleValue();
        if (v instanceof String) {
            try {
                return Double.parseDouble(((String) v).trim());
            } catch (NumberFormatException e) {
                return def;
            }
        }
        return def;
    }

    static boolean bool(JSONObject o, String key, boolean def) {
        if (o == null) return def;
        Object v = o.opt(key);
        if (v instanceof Boolean) return (Boolean) v;
        return def;
    }

    /** Истинность значения по-питоновски (meta.get("bulk") и т. п.). */
    static boolean truthy(Object v) {
        if (isNull(v)) return false;
        if (v instanceof Boolean) return (Boolean) v;
        if (v instanceof Number) return ((Number) v).doubleValue() != 0;
        if (v instanceof String) return !((String) v).isEmpty();
        if (v instanceof JSONArray) return ((JSONArray) v).length() > 0;
        if (v instanceof JSONObject) return ((JSONObject) v).length() > 0;
        return true;
    }

    static JSONObject obj(JSONObject o, String key) {
        JSONObject v = o == null ? null : o.optJSONObject(key);
        return v == null ? new JSONObject() : v;
    }

    static JSONArray arr(JSONObject o, String key) {
        JSONArray v = o == null ? null : o.optJSONArray(key);
        return v == null ? new JSONArray() : v;
    }

    static JSONObject copy(JSONObject o) {
        try {
            return o == null ? new JSONObject() : new JSONObject(o.toString());
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    static void put(JSONObject o, String key, Object value) {
        try {
            o.put(key, value == null ? JSONObject.NULL : value);
        } catch (Exception ignored) {
            // put строк и чисел не падает (NaN не бывает)
        }
    }

    // ── Строки как в Python ─────────────────────────────────────────────

    /** str.isspace() Python для одного символа (BMP). */
    static boolean isPySpace(int c) {
        return (c >= 0x09 && c <= 0x0d) || (c >= 0x1c && c <= 0x20) || c == 0x85 || c == 0xa0
                || c == 0x1680 || (c >= 0x2000 && c <= 0x200a) || c == 0x2028 || c == 0x2029
                || c == 0x202f || c == 0x205f || c == 0x3000;
    }

    /** Класс символов регулярки = \s Python (для Java и ICU одинаково). */
    static final String WS = "[\\t\\n\\x0B\\f\\r\\x1C-\\x20\\x85\\xA0\\u1680\\u2000-\\u200A\\u2028\\u2029\\u202F\\u205F\\u3000]";
    private static final Pattern WS_RUN = Pattern.compile(WS + "+");

    static String strip(String s) {
        if (s == null) return "";
        int a = 0;
        int b = s.length();
        while (a < b && isPySpace(s.charAt(a))) a++;
        while (b > a && isPySpace(s.charAt(b - 1))) b--;
        return s.substring(a, b);
    }

    static String rstrip(String s) {
        int b = s.length();
        while (b > 0 && isPySpace(s.charAt(b - 1))) b--;
        return s.substring(0, b);
    }

    /** re.sub(r"\s+", " ", s).strip() */
    static String collapse(String s) {
        return strip(WS_RUN.matcher(s == null ? "" : s).replaceAll(" "));
    }

    /** String.join есть только с Android 8. */
    static String join(String sep, java.util.List<String> parts) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < parts.size(); i++) {
            if (i > 0) sb.append(sep);
            sb.append(parts.get(i));
        }
        return sb.toString();
    }

    static int cpLen(String s) {
        return s.codePointCount(0, s.length());
    }

    /** s[:n] Python (по символам). */
    static String head(String s, int n) {
        if (n <= 0) return "";
        if (cpLen(s) <= n) return s;
        return s.substring(0, s.offsetByCodePoints(0, n));
    }

    /** feed._clip ядра: обрезка с «…». */
    static String clip(String text, int limit) {
        String t = strip(text == null ? "" : text);
        if (cpLen(t) <= limit) return t;
        return rstrip(head(t, limit - 1)) + "…";
    }

    static String lower(String s) {
        return s == null ? "" : s.toLowerCase(java.util.Locale.ROOT);
    }

    static String sha1Hex(String s) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-1");
            byte[] d = md.digest(s.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(d.length * 2);
            for (byte b : d) sb.append(String.format("%02x", b & 0xff));
            return sb.toString();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    static String sha256B64Url(String s) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            return b64urlEncode(md.digest(s.getBytes(StandardCharsets.US_ASCII)));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** id записи ленты — как feed.event_id ядра. */
    static String eventId(String source, String key) {
        return sha1Hex(source + ":" + key).substring(0, 16);
    }

    static String randomHex(int bytes) {
        byte[] b = new byte[bytes];
        new java.security.SecureRandom().nextBytes(b);
        StringBuilder sb = new StringBuilder();
        for (byte x : b) sb.append(String.format("%02x", x & 0xff));
        return sb.toString();
    }

    /** secrets.token_urlsafe(n) Python. */
    static String tokenUrlsafe(int bytes) {
        byte[] b = new byte[bytes];
        new java.security.SecureRandom().nextBytes(b);
        return b64urlEncode(b);
    }

    // ── base64 (java.util.Base64 — только с Android 8) ──────────────────

    private static final String B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

    /** Терпеливое декодирование: обычный и URL-алфавит, без «=» и мусора. */
    static byte[] b64decode(String s) {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        int buf = 0;
        int bits = 0;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            int v;
            if (c == '-') v = 62;
            else if (c == '_') v = 63;
            else if (c == '=') break;
            else v = B64.indexOf(c);
            if (v < 0) continue;
            buf = (buf << 6) | v;
            bits += 6;
            if (bits >= 8) {
                bits -= 8;
                out.write((buf >> bits) & 0xff);
            }
        }
        return out.toByteArray();
    }

    static String b64urlEncode(byte[] data) {
        StringBuilder sb = new StringBuilder();
        int i = 0;
        while (i + 2 < data.length) {
            int n = ((data[i] & 0xff) << 16) | ((data[i + 1] & 0xff) << 8) | (data[i + 2] & 0xff);
            sb.append(B64.charAt((n >> 18) & 63)).append(B64.charAt((n >> 12) & 63))
                    .append(B64.charAt((n >> 6) & 63)).append(B64.charAt(n & 63));
            i += 3;
        }
        int rest = data.length - i;
        if (rest == 1) {
            int n = (data[i] & 0xff) << 16;
            sb.append(B64.charAt((n >> 18) & 63)).append(B64.charAt((n >> 12) & 63));
        } else if (rest == 2) {
            int n = ((data[i] & 0xff) << 16) | ((data[i + 1] & 0xff) << 8);
            sb.append(B64.charAt((n >> 18) & 63)).append(B64.charAt((n >> 12) & 63)).append(B64.charAt((n >> 6) & 63));
        }
        return sb.toString().replace('+', '-').replace('/', '_');
    }

    // ── Даты ────────────────────────────────────────────────────────────

    private static final String[] MONTHS = {"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
            "nov", "dec", "january", "february", "march", "april", "may", "june", "july", "august", "september",
            "october", "november", "december"};
    private static final String[] DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"};
    private static final Map<String, Integer> ZONES = new HashMap<>();

    static {
        ZONES.put("UT", 0);
        ZONES.put("UTC", 0);
        ZONES.put("GMT", 0);
        ZONES.put("Z", 0);
        ZONES.put("AST", -400);
        ZONES.put("ADT", -300);
        ZONES.put("EST", -500);
        ZONES.put("EDT", -400);
        ZONES.put("CST", -600);
        ZONES.put("CDT", -500);
        ZONES.put("MST", -700);
        ZONES.put("MDT", -600);
        ZONES.put("PST", -800);
        ZONES.put("PDT", -700);
    }

    private static int monthIndex(String m) {
        for (int i = 0; i < MONTHS.length; i++) if (MONTHS[i].equals(m)) return i;
        return -1;
    }

    private static boolean isDay(String d) {
        for (String x : DAYS) if (x.equals(d)) return true;
        return false;
    }

    private static int pyInt(String s) {
        // int() Python: пробелы по краям, знак, только цифры
        String t = strip(s);
        if (t.isEmpty()) throw new NumberFormatException();
        return Integer.parseInt(t.startsWith("+") ? t.substring(1) : t);
    }

    /** Секунды эпохи по стенным часам; tz — смещение в секундах или null (местное время). */
    static Double epoch(int y, int mo, int d, int h, int mi, int s, double frac, Integer tzSec) {
        if (mo < 1 || mo > 12 || d < 1 || h < 0 || h > 23 || mi < 0 || mi > 59 || s < 0 || s > 59 || y < 1) {
            return null;
        }
        Calendar cal = Calendar.getInstance(tzSec == null ? TimeZone.getDefault() : TimeZone.getTimeZone("UTC"));
        cal.clear();
        cal.set(y, mo - 1, 1, 0, 0, 0);
        if (d > cal.getActualMaximum(Calendar.DAY_OF_MONTH)) return null;
        cal.set(y, mo - 1, d, h, mi, s);
        double t = cal.getTimeInMillis() / 1000.0 + frac;
        if (tzSec != null) t -= tzSec;
        return t;
    }

    /** email.utils.parsedate_to_datetime(...).timestamp(); null — не дата. */
    static Double rfc2822(String data) {
        if (data == null) return null;
        String[] parts = strip(data).split(WS + "+");
        java.util.List<String> d = new java.util.ArrayList<>();
        for (String p : parts) if (!p.isEmpty()) d.add(p);
        if (d.isEmpty()) return null;
        if (d.get(0).endsWith(",") || isDay(lower(d.get(0)))) {
            d.remove(0);
        } else {
            int i = d.get(0).lastIndexOf(',');
            if (i >= 0) d.set(0, d.get(0).substring(i + 1));
        }
        if (d.size() == 3) {
            String[] stuff = d.get(0).split("-", -1);
            if (stuff.length == 3) {
                java.util.List<String> nd = new java.util.ArrayList<>(java.util.Arrays.asList(stuff));
                nd.addAll(d.subList(1, d.size()));
                d = nd;
            }
        }
        if (d.size() == 4) {
            String s = d.get(3);
            int i = s.indexOf('+');
            if (i == -1) i = s.indexOf('-');
            if (i > 0) {
                d.set(3, s.substring(0, i));
                d.add(s.substring(i));
            } else {
                d.add("");
            }
        }
        if (d.size() < 5) return null;
        String dd = d.get(0);
        String mm = d.get(1);
        String yy = d.get(2);
        String tm = d.get(3);
        String tz = d.get(4);
        if (dd.isEmpty() || mm.isEmpty() || yy.isEmpty()) return null;
        mm = lower(mm);
        if (monthIndex(mm) < 0) {
            String t = dd;
            dd = mm;
            mm = lower(t);
            if (monthIndex(mm) < 0) return null;
        }
        int month = monthIndex(mm) + 1;
        if (month > 12) month -= 12;
        if (dd.endsWith(",")) dd = dd.substring(0, dd.length() - 1);
        int i = yy.indexOf(':');
        if (i > 0) {
            String t = yy;
            yy = tm;
            tm = t;
        }
        if (yy.endsWith(",")) {
            yy = yy.substring(0, yy.length() - 1);
            if (yy.isEmpty()) return null;
        }
        if (yy.isEmpty() || !Character.isDigit(yy.charAt(0))) {
            String t = yy;
            yy = tz;
            tz = t;
        }
        if (tm.endsWith(",")) tm = tm.substring(0, tm.length() - 1);
        String[] t = tm.split(":", -1);
        String thh;
        String tmm;
        String tss;
        if (t.length == 2) {
            thh = t[0];
            tmm = t[1];
            tss = "0";
        } else if (t.length == 3) {
            thh = t[0];
            tmm = t[1];
            tss = t[2];
        } else if (t.length == 1 && t[0].contains(".")) {
            String[] u = t[0].split("\\.", -1);
            if (u.length == 2) {
                thh = u[0];
                tmm = u[1];
                tss = "0";
            } else if (u.length == 3) {
                thh = u[0];
                tmm = u[1];
                tss = u[2];
            } else {
                return null;
            }
        } else {
            return null;
        }
        int year;
        int day;
        int hh;
        int mi;
        int ss;
        try {
            year = pyInt(yy);
            day = pyInt(dd);
            hh = pyInt(thh);
            mi = pyInt(tmm);
            ss = pyInt(tss);
        } catch (NumberFormatException e) {
            return null;
        }
        if (year < 100) year += year > 68 ? 1900 : 2000;
        Integer off = null;
        String tzu = tz.toUpperCase(java.util.Locale.ROOT);
        if (ZONES.containsKey(tzu)) {
            off = ZONES.get(tzu);
        } else {
            try {
                off = pyInt(tzu);
            } catch (NumberFormatException ignored) {
                off = null;
            }
            if (off != null && off == 0 && tzu.startsWith("-")) off = null;
        }
        Integer sec = null;
        if (off != null) {
            int sign = off < 0 ? -1 : 1;
            int a = Math.abs(off);
            sec = sign * ((a / 100) * 3600 + (a % 100) * 60);
        }
        return epoch(year, month, day, hh, mi, ss, 0, sec);
    }

    private static final Pattern ISO = Pattern.compile(
            "^(\\d{4})-?(\\d{2})-?(\\d{2})(?:[T ](\\d{2})(?::?(\\d{2})(?::?(\\d{2})(?:[.,](\\d+))?)?)?)?"
                    + "(Z|[+-]\\d{2}(?::?\\d{2}(?::?\\d{2}(?:\\.\\d+)?)?)?)?$");

    /** datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp(); null — не дата. */
    static Double iso(String value) {
        if (value == null) return null;
        Matcher m = ISO.matcher(value);
        if (!m.matches()) return null;
        int y = Integer.parseInt(m.group(1));
        int mo = Integer.parseInt(m.group(2));
        int d = Integer.parseInt(m.group(3));
        int h = m.group(4) == null ? 0 : Integer.parseInt(m.group(4));
        int mi = m.group(5) == null ? 0 : Integer.parseInt(m.group(5));
        int s = m.group(6) == null ? 0 : Integer.parseInt(m.group(6));
        double frac = 0;
        if (m.group(7) != null) {
            String f = m.group(7);
            if (f.length() > 6) f = f.substring(0, 6);
            frac = Integer.parseInt(f) / Math.pow(10, f.length());
        }
        Integer off = null;
        String z = m.group(8);
        if (z != null) {
            if (z.equals("Z")) {
                off = 0;
            } else {
                int sign = z.charAt(0) == '-' ? -1 : 1;
                String digits = z.substring(1).replace(":", "");
                int oh = Integer.parseInt(digits.substring(0, 2));
                int om = digits.length() >= 4 ? Integer.parseInt(digits.substring(2, 4)) : 0;
                int os = digits.length() >= 6 ? Integer.parseInt(digits.substring(4, 6)) : 0;
                if (oh > 23 || om > 59 || os > 59) return null;
                off = sign * (oh * 3600 + om * 60 + os);
            }
        }
        return epoch(y, mo, d, h, mi, s, frac, off);
    }

    /** _date ленты: RFC 2822, иначе ISO, иначе 0. */
    static double feedDate(String value) {
        String v = strip(value);
        if (v.isEmpty()) return 0.0;
        Double t = rfc2822(v);
        if (t != null) return t;
        t = iso(v.replace("Z", "+00:00").replace("+00:00", "Z"));
        return t == null ? 0.0 : t;
    }

    /** _iso_ts GitHub: fromisoformat(...replace("Z", "+00:00")), иначе 0. */
    static double isoTs(String value) {
        if (value == null) return 0.0;
        Double t = iso(value.replace("Z", "+00:00").replace("+00:00", "Z"));
        return t == null ? 0.0 : t;
    }

    // ── HTML ────────────────────────────────────────────────────────────

    private static final Pattern HTML_BLOCKS = Pattern.compile("(?is)<(script|style|head)\\b.*?</\\1>");
    private static final Pattern HTML_BREAKS = Pattern.compile("(?i)<br" + WS + "*/?>|</p>|</div>|</li>");
    private static final Pattern HTML_TAGS = Pattern.compile("<[^>]+>");
    private static final Pattern HSPACE = Pattern.compile("[ \\t\\r\\f\\x0B]+");
    private static final Pattern BLANK_LINES = Pattern.compile("\\n" + WS + "*\\n+");

    /** strip_html ядра (app/integrations/sources/google.py). */
    static String stripHtml(String text) {
        String t = HTML_BLOCKS.matcher(text).replaceAll(" ");
        t = HTML_BREAKS.matcher(t).replaceAll("\n");
        t = HTML_TAGS.matcher(t).replaceAll(" ");
        t = unescape(t);
        t = HSPACE.matcher(t).replaceAll(" ");
        return strip(BLANK_LINES.matcher(t).replaceAll("\n"));
    }

    private static final Pattern CHARREF = Pattern.compile("&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\\t\\n\\f <&#;]{1,32};?)");
    private static final Map<String, String> ENTITIES = new HashMap<>();
    private static final Map<Integer, String> BAD_REFS = new HashMap<>();

    static {
        // Частые сущности HTML5 (html.entities.html5 Python — 2231 имя; здесь
        // те, что встречаются в лентах и письмах). Имена Latin-1 и пять
        // базовых HTML разрешают и без «;» — как в стандарте
        String[] legacy = {
                "amp", "&", "lt", "<", "gt", ">", "quot", "\"", "nbsp", " ", "copy", "©", "reg", "®",
                "iexcl", "¡", "cent", "¢", "pound", "£", "curren", "¤", "yen", "¥",
                "brvbar", "¦", "sect", "§", "uml", "¨", "ordf", "ª", "laquo", "«",
                "not", "¬", "shy", "­", "macr", "¯", "deg", "°", "plusmn", "±",
                "sup2", "²", "sup3", "³", "acute", "´", "micro", "µ", "para", "¶",
                "middot", "·", "cedil", "¸", "sup1", "¹", "ordm", "º", "raquo", "»",
                "frac14", "¼", "frac12", "½", "frac34", "¾", "iquest", "¿",
                "Agrave", "À", "Aacute", "Á", "Acirc", "Â", "Atilde", "Ã", "Auml", "Ä",
                "Aring", "Å", "AElig", "Æ", "Ccedil", "Ç", "Egrave", "È", "Eacute", "É",
                "Ecirc", "Ê", "Euml", "Ë", "Igrave", "Ì", "Iacute", "Í", "Icirc", "Î",
                "Iuml", "Ï", "ETH", "Ð", "Ntilde", "Ñ", "Ograve", "Ò", "Oacute", "Ó",
                "Ocirc", "Ô", "Otilde", "Õ", "Ouml", "Ö", "times", "×", "Oslash", "Ø",
                "Ugrave", "Ù", "Uacute", "Ú", "Ucirc", "Û", "Uuml", "Ü", "Yacute", "Ý",
                "THORN", "Þ", "szlig", "ß", "agrave", "à", "aacute", "á", "acirc", "â",
                "atilde", "ã", "auml", "ä", "aring", "å", "aelig", "æ", "ccedil", "ç",
                "egrave", "è", "eacute", "é", "ecirc", "ê", "euml", "ë", "igrave", "ì",
                "iacute", "í", "icirc", "î", "iuml", "ï", "eth", "ð", "ntilde", "ñ",
                "ograve", "ò", "oacute", "ó", "ocirc", "ô", "otilde", "õ", "ouml", "ö",
                "divide", "÷", "oslash", "ø", "ugrave", "ù", "uacute", "ú", "ucirc", "û",
                "uuml", "ü", "yacute", "ý", "thorn", "þ", "yuml", "ÿ",
                "AMP", "&", "LT", "<", "GT", ">", "QUOT", "\"", "COPY", "©", "REG", "®",
        };
        for (int i = 0; i < legacy.length; i += 2) {
            ENTITIES.put(legacy[i], legacy[i + 1]);
            ENTITIES.put(legacy[i] + ";", legacy[i + 1]);
        }
        String[] semi = {
                "apos", "'", "mdash", "—", "ndash", "–", "hellip", "…", "lsquo", "‘",
                "rsquo", "’", "sbquo", "‚", "ldquo", "“", "rdquo", "”", "bdquo", "„",
                "bull", "•", "trade", "™", "euro", "€", "rarr", "→", "larr", "←",
                "uarr", "↑", "darr", "↓", "harr", "↔", "rArr", "⇒", "lArr", "⇐",
                "thinsp", " ", "ensp", " ", "emsp", " ", "zwnj", "‌", "zwj", "‍",
                "lrm", "‎", "rlm", "‏", "dagger", "†", "Dagger", "‡", "permil", "‰",
                "lsaquo", "‹", "rsaquo", "›", "prime", "′", "Prime", "″", "minus", "−",
                "le", "≤", "ge", "≥", "ne", "≠", "asymp", "≈", "infin", "∞",
                "hearts", "♥", "spades", "♠", "clubs", "♣", "diams", "♦", "check", "✓",
                "star", "☆", "starf", "★", "laquo", "«", "numero", "№", "half", "½",
                "nbsp", " ", "OElig", "Œ", "oelig", "œ", "Scaron", "Š", "scaron", "š",
                "Yuml", "Ÿ", "fnof", "ƒ", "circ", "ˆ", "tilde", "˜", "alpha", "α",
                "beta", "β", "gamma", "γ", "delta", "δ", "pi", "π", "sigma", "σ",
                "mu", "μ", "lambda", "λ", "omega", "ω", "Omega", "Ω", "Delta", "Δ",
                "sum", "∑", "prod", "∏", "radic", "√", "part", "∂", "isin", "∈",
                "nbhy", "‑", "hyphen", "‐", "dash", "‐", "colon", ":", "comma", ",",
                "period", ".", "excl", "!", "quest", "?", "num", "#", "dollar", "$", "percnt", "%",
                "lpar", "(", "rpar", ")", "ast", "*", "plus", "+", "sol", "/", "semi", ";", "equals", "=",
                "commat", "@", "lsqb", "[", "rsqb", "]", "lowbar", "_", "lcub", "{", "rcub", "}", "verbar", "|",
                "Tab", "\t", "NewLine", "\n",
        };
        for (int i = 0; i < semi.length; i += 2) ENTITIES.put(semi[i] + ";", semi[i + 1]);
        int[] bad = {0x00, 0xfffd, 0x0d, '\r', 0x80, 0x20ac, 0x81, 0x81, 0x82, 0x201a, 0x83, 0x0192, 0x84, 0x201e,
                0x85, 0x2026, 0x86, 0x2020, 0x87, 0x2021, 0x88, 0x02c6, 0x89, 0x2030, 0x8a, 0x0160, 0x8b, 0x2039,
                0x8c, 0x0152, 0x8d, 0x8d, 0x8e, 0x017d, 0x8f, 0x8f, 0x90, 0x90, 0x91, 0x2018, 0x92, 0x2019,
                0x93, 0x201c, 0x94, 0x201d, 0x95, 0x2022, 0x96, 0x2013, 0x97, 0x2014, 0x98, 0x02dc, 0x99, 0x2122,
                0x9a, 0x0161, 0x9b, 0x203a, 0x9c, 0x0153, 0x9d, 0x9d, 0x9e, 0x017e, 0x9f, 0x0178};
        for (int i = 0; i < bad.length; i += 2) BAD_REFS.put(bad[i], String.valueOf((char) bad[i + 1]));
    }

    private static boolean invalidCodepoint(int n) {
        return (n >= 0x1 && n <= 0x8) || (n >= 0xe && n <= 0x1f) || (n >= 0x7f && n <= 0x9f)
                || (n >= 0xfdd0 && n <= 0xfdef) || n == 0xb || (n & 0xfffe) == 0xfffe;
    }

    /** html.unescape Python. */
    static String unescape(String s) {
        if (s.indexOf('&') < 0) return s;
        Matcher m = CHARREF.matcher(s);
        StringBuffer sb = new StringBuffer();
        while (m.find()) {
            m.appendReplacement(sb, Matcher.quoteReplacement(charref(m.group(1))));
        }
        m.appendTail(sb);
        return sb.toString();
    }

    private static String charref(String s) {
        if (s.charAt(0) == '#') {
            long num;
            try {
                String digits = s.endsWith(";") ? s.substring(0, s.length() - 1) : s;
                if (digits.length() > 1 && (digits.charAt(1) == 'x' || digits.charAt(1) == 'X')) {
                    num = digits.length() > 18 ? Long.MAX_VALUE : Long.parseLong(digits.substring(2), 16);
                } else {
                    num = digits.length() > 19 ? Long.MAX_VALUE : Long.parseLong(digits.substring(1));
                }
            } catch (NumberFormatException e) {
                num = Long.MAX_VALUE;
            }
            if (num <= 0x9f && BAD_REFS.containsKey((int) num)) return BAD_REFS.get((int) num);
            if ((num >= 0xD800 && num <= 0xDFFF) || num > 0x10FFFF) return "�";
            if (invalidCodepoint((int) num)) return "";
            return new String(Character.toChars((int) num));
        }
        String v = ENTITIES.get(s);
        if (v != null) return v;
        for (int x = s.length() - 1; x > 1; x--) {
            v = ENTITIES.get(s.substring(0, x));
            if (v != null) return v + s.substring(x);
        }
        return "&" + s;
    }
}
