package io.vpcore.app;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.Closeable;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Сервисы на телефоне: почта по IMAP — порт ядра
 * (app/integrations/sources/imap_mail.py) на своём маленьком клиенте IMAP4rev1
 * (в Android нет imaplib, а JavaMail ради пяти команд — лишняя библиотека).
 *
 * Только чтение: ящик открывается EXAMINE, письма берутся BODY.PEEK — не
 * помечаются прочитанными. Курсор — UID последнего увиденного письма при
 * неизменном UIDVALIDITY; первый опрос только его запоминает.
 *
 * Соединение — Opener: на телефоне TLS (порт 993) с проверкой имени сервера
 * (PhoneServices), в тесте — обычный сокет к поддельному серверу.
 */
final class SvcImap {

    private SvcImap() {
    }

    interface Opener {
        Socket open(String host, int port) throws IOException;
    }

    static final int MAX_NEW_PER_POLL = 30;
    static final int FETCH_BYTES = 65536;
    private static final int MAX_LITERAL = 4 * 1024 * 1024;
    private static final Pattern HOST = Pattern.compile("^[A-Za-z0-9.-]{3,253}$");
    private static final Pattern LITERAL = Pattern.compile("\\{(\\d+)\\}$");
    private static final Pattern UIDVALIDITY = Pattern.compile("\\[UIDVALIDITY (\\d+)\\]", Pattern.CASE_INSENSITIVE);

    // ── Ящик: пресеты и проверка полей (normalize_account ядра) ─────────

    static String[] presetFor(String user) {
        String u = user == null ? "" : user;
        String domain = SvcUtil.lower(u.substring(u.lastIndexOf('@') + 1));
        switch (domain) {
            case "yandex.ru":
            case "ya.ru":
            case "yandex.com":
                return new String[]{"imap.yandex.ru", "993"};
            case "mail.ru":
            case "bk.ru":
            case "inbox.ru":
            case "list.ru":
            case "internet.ru":
                return new String[]{"imap.mail.ru", "993"};
            case "gmail.com":
                return new String[]{"imap.gmail.com", "993"};
            case "outlook.com":
            case "hotmail.com":
            case "live.com":
                return new String[]{"outlook.office365.com", "993"};
            case "icloud.com":
            case "me.com":
                return new String[]{"imap.mail.me.com", "993"};
            case "rambler.ru":
                return new String[]{"imap.rambler.ru", "993"};
            default:
                return null;
        }
    }

    static JSONObject normalizeAccount(JSONObject data) {
        String user = SvcUtil.strip(SvcUtil.str(data, "user"));
        String password = SvcUtil.str(data, "password");
        String host = SvcUtil.lower(SvcUtil.strip(SvcUtil.str(data, "host")));
        int port = (int) SvcUtil.num(data, "port", 0);
        if (host.isEmpty()) {
            String[] p = presetFor(user);
            if (p != null) {
                host = p[0];
                port = Integer.parseInt(p[1]);
            }
        }
        if (user.isEmpty() || password.isEmpty() || !HOST.matcher(host).matches()) {
            throw new IllegalArgumentException("нужны адрес ящика, пароль приложения и сервер IMAP");
        }
        if (port == 0) port = 993;
        if (port < 1 || port > 65535) throw new IllegalArgumentException("неверный порт");
        String folder = SvcUtil.head(SvcUtil.strip(SvcUtil.str(data, "folder")), 100);
        if (folder.isEmpty()) folder = "INBOX";
        JSONObject acc = new JSONObject();
        SvcUtil.put(acc, "user", user);
        SvcUtil.put(acc, "password", password);
        SvcUtil.put(acc, "host", host);
        SvcUtil.put(acc, "port", port);
        SvcUtil.put(acc, "folder", folder);
        return acc;
    }

    // ── Протокол ────────────────────────────────────────────────────────

    /** Ответ сервера без тега: текст (литералы — «{n}») и сами литералы. */
    static final class Untagged {
        final StringBuilder text = new StringBuilder();
        final List<String> chunks = new ArrayList<>(); // текст до каждого литерала
        final List<byte[]> literals = new ArrayList<>();
    }

    static final class Reply {
        String status = "";
        String text = "";
        final List<Untagged> untagged = new ArrayList<>();
    }

    static final class Conn implements Closeable {
        private final Socket socket;
        private final InputStream in;
        private final OutputStream out;
        private int seq = 0;

        Conn(Socket s) throws IOException {
            socket = s;
            socket.setSoTimeout(30_000);
            in = new BufferedInputStream(s.getInputStream());
            out = s.getOutputStream();
        }

        String readLine() throws IOException {
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            int c;
            while ((c = in.read()) >= 0) {
                if (c == '\n') break;
                b.write(c);
                if (b.size() > 1_000_000) throw new IOException("слишком длинная строка ответа");
            }
            if (c < 0 && b.size() == 0) throw new IOException("сервер закрыл соединение");
            byte[] line = b.toByteArray();
            int n = line.length;
            if (n > 0 && line[n - 1] == '\r') n--;
            return new String(line, 0, n, StandardCharsets.UTF_8);
        }

        byte[] readExactly(int n) throws IOException {
            byte[] buf = new byte[n];
            int off = 0;
            while (off < n) {
                int r = in.read(buf, off, n - off);
                if (r < 0) throw new IOException("сервер закрыл соединение");
                off += r;
            }
            return buf;
        }

        void greeting() throws IOException, SvcSources.SourceError {
            String g = readLine();
            if (!g.startsWith("* OK") && !g.startsWith("* PREAUTH")) {
                throw new SvcSources.SourceError("imap: сервер не принял соединение (" + SvcUtil.head(g, 120) + ")");
            }
        }

        /** Команда; аргументы-строки (Arg) уходят в кавычках или литералом. */
        Reply command(String verb, Object... args) throws IOException {
            String tag = "V" + (++seq);
            List<byte[]> pending = new ArrayList<>();
            StringBuilder line = new StringBuilder(tag).append(' ').append(verb);
            for (Object a : args) {
                line.append(' ');
                if (a instanceof Arg) {
                    String s = ((Arg) a).value;
                    if (needsLiteral(s)) {
                        byte[] bytes = s.getBytes(StandardCharsets.UTF_8);
                        line.append('{').append(bytes.length).append('}');
                        pending.add(line.toString().getBytes(StandardCharsets.UTF_8));
                        pending.add(bytes);
                        line = new StringBuilder();
                    } else {
                        line.append('"').append(s.replace("\\", "\\\\").replace("\"", "\\\"")).append('"');
                    }
                } else {
                    line.append(a);
                }
            }
            pending.add(line.toString().getBytes(StandardCharsets.UTF_8));
            // Литерал: строка до него, ждём «+», потом сами байты
            for (int i = 0; i < pending.size(); i++) {
                out.write(pending.get(i));
                if (i % 2 == 0 && i + 1 < pending.size()) {
                    out.write("\r\n".getBytes(StandardCharsets.US_ASCII));
                    out.flush();
                    String cont = readLine();
                    if (!cont.startsWith("+")) {
                        Reply r = new Reply();
                        r.status = "BAD";
                        r.text = cont;
                        return r;
                    }
                }
            }
            out.write("\r\n".getBytes(StandardCharsets.US_ASCII));
            out.flush();
            Reply reply = new Reply();
            while (true) {
                String l = readLine();
                if (l.startsWith(tag + " ")) {
                    String rest = l.substring(tag.length() + 1);
                    int sp = rest.indexOf(' ');
                    reply.status = (sp < 0 ? rest : rest.substring(0, sp)).toUpperCase(Locale.ROOT);
                    reply.text = sp < 0 ? "" : rest.substring(sp + 1);
                    return reply;
                }
                if (!l.startsWith("*")) continue; // «+» и прочее — не наше
                Untagged u = new Untagged();
                while (true) {
                    Matcher m = LITERAL.matcher(l);
                    if (!m.find()) {
                        u.text.append(l);
                        break;
                    }
                    int n = Integer.parseInt(m.group(1));
                    if (n > MAX_LITERAL) throw new IOException("слишком большой ответ");
                    u.text.append(l);
                    u.chunks.add(l);
                    u.literals.add(readExactly(n));
                    l = readLine();
                }
                reply.untagged.add(u);
            }
        }

        @Override
        public void close() {
            try {
                command("LOGOUT");
            } catch (Exception ignored) {
                // выходим всё равно
            }
            try {
                socket.close();
            } catch (IOException ignored) {
                // уже закрыт
            }
        }
    }

    /** Строка-аргумент IMAP (в кавычках или литералом). */
    static final class Arg {
        final String value;

        Arg(String value) {
            this.value = value;
        }
    }

    private static boolean needsLiteral(String s) {
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c > 0x7e || c == '\r' || c == '\n' || c == 0) return true;
        }
        return false;
    }

    static Conn connect(JSONObject acc, Opener opener) throws SvcSources.SourceError {
        String host = SvcUtil.str(acc, "host");
        Socket s;
        Conn conn;
        try {
            s = opener.open(host, (int) SvcUtil.num(acc, "port", 993));
            conn = new Conn(s);
            conn.greeting();
        } catch (IOException e) {
            throw new SvcSources.SourceError("imap: нет соединения с " + host + ": " + e.getMessage());
        }
        Reply r;
        try {
            r = conn.command("LOGIN", new Arg(SvcUtil.str(acc, "user")), new Arg(SvcUtil.str(acc, "password")));
        } catch (IOException e) {
            conn.close();
            throw new SvcSources.SourceError("imap: " + e.getMessage());
        }
        if (!r.status.equals("OK")) {
            conn.close();
            throw new SvcSources.AuthError("imap: вход не принят ([" + r.status + "] " + SvcUtil.head(r.text, 200) + ")");
        }
        return conn;
    }

    private static String folder(JSONObject acc) {
        String f = SvcUtil.str(acc, "folder");
        return f.isEmpty() ? "INBOX" : f;
    }

    /** ImapSource.check ядра. */
    static JSONObject check(JSONObject acc, Opener opener) throws SvcSources.SourceError {
        Conn conn = connect(acc, opener);
        try {
            Reply r = conn.command("EXAMINE", new Arg(folder(acc)));
            if (!r.status.equals("OK")) throw new SvcSources.SourceError("imap: нет папки " + folder(acc));
            JSONObject out = new JSONObject();
            SvcUtil.put(out, "ok", true);
            SvcUtil.put(out, "account", SvcUtil.str(acc, "user"));
            return out;
        } catch (IOException e) {
            throw new SvcSources.SourceError("imap: " + e.getMessage());
        } finally {
            conn.close();
        }
    }

    /** ImapSource.poll ядра. state — {validity, last_uid}. */
    static JSONArray poll(JSONObject acc, JSONObject state, Opener opener) throws SvcSources.SourceError {
        Conn conn = connect(acc, opener);
        try {
            Reply sel = conn.command("EXAMINE", new Arg(folder(acc)));
            if (!sel.status.equals("OK")) throw new SvcSources.SourceError("imap: нет папки " + folder(acc));
            String validity = "";
            for (Untagged u : sel.untagged) {
                Matcher m = UIDVALIDITY.matcher(u.text);
                if (m.find()) validity = m.group(1);
            }
            Reply search = conn.command("UID SEARCH ALL");
            List<Long> uids = new ArrayList<>();
            if (search.status.equals("OK")) {
                for (Untagged u : search.untagged) {
                    String t = u.text.toString();
                    if (!t.toUpperCase(Locale.ROOT).startsWith("* SEARCH")) continue;
                    for (String x : t.substring(8).trim().split("\\s+")) {
                        if (!x.isEmpty()) uids.add(Long.parseLong(x));
                    }
                }
            }
            long last = (long) SvcUtil.num(state, "last_uid", 0);
            long max = 0;
            for (long u : uids) max = Math.max(max, u);
            String known = SvcUtil.str(state, "validity");
            if (known.isEmpty() || !known.equals(validity)) {
                SvcUtil.put(state, "validity", validity);
                SvcUtil.put(state, "last_uid", max);
                return new JSONArray();
            }
            List<Long> fresh = new ArrayList<>();
            for (long u : uids) if (u > last) fresh.add(u);
            if (fresh.size() > MAX_NEW_PER_POLL) fresh = fresh.subList(fresh.size() - MAX_NEW_PER_POLL, fresh.size());
            JSONArray events = new JSONArray();
            for (long uid : fresh) {
                Reply f = conn.command("UID FETCH " + uid + " (FLAGS BODY.PEEK[]<0." + FETCH_BYTES + ">)");
                if (!f.status.equals("OK")) continue;
                byte[] raw = null;
                String flags = "";
                for (Untagged u : f.untagged) {
                    if (u.literals.isEmpty()) continue;
                    String chunk = u.chunks.get(0);
                    boolean mine = Pattern.compile("UID " + uid + "\\b").matcher(u.text).find();
                    if (raw == null || mine) {
                        raw = u.literals.get(0);
                        flags = chunk;
                    }
                }
                if (raw == null || raw.length == 0 || flags.contains("\\Seen")) continue;
                events.put(SvcParse.mailEvent(raw, validity + ":" + uid, flags));
            }
            if (!uids.isEmpty()) SvcUtil.put(state, "last_uid", Math.max(max, last));
            return events;
        } catch (IOException e) {
            throw new SvcSources.SourceError("imap: " + e.getMessage());
        } catch (NumberFormatException e) {
            throw new SvcSources.SourceError("imap: непонятный ответ сервера");
        } finally {
            conn.close();
        }
    }
}
