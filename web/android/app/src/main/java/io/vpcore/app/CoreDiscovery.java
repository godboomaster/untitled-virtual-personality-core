package io.vpcore.app;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.json.JSONObject;

/* «Кто тут ядро VPC?» — широковещательный запрос в локальной сети (ответчик
   ядра — app/link/discovery.py). Пакет «VPC-DISCOVER/1 <nonce>», дополненный
   пробелами до REQUEST_MIN байт (ядро короче не принимает: ответ не длиннее
   запроса), уходит три раза — UDP теряется; ответы — JSON с тем же nonce:
   постоянный id ядра, имя компьютера, порт API (null — API закрыто для
   сети), нужен ли токен, порт VPC Link. Без android.* — тест гоняет класс обычным javac
   (scripts/test_core_discovery.py). */
final class CoreDiscovery {

    static final int PORT = 8767;
    static final String PROTO = "discover/1";
    private static final String MAGIC = "VPC-DISCOVER/1";
    private static final int REQUEST_MIN = 256;
    private static final int[] SEND_AT_MS = {0, 300, 800};

    private CoreDiscovery() {}

    /** Ответившее ядро. */
    static final class Core {
        final String host; // адрес, с которого пришёл ответ
        final String id; // постоянный id ядра ('' — старое ядро)
        final String name;
        final int api; // 0 — API не открыто в сеть
        final boolean token;
        final int link; // 0 — канала VPC Link нет

        Core(String host, String id, String name, int api, boolean token, int link) {
            this.host = host;
            this.id = id;
            this.name = name;
            this.api = api;
            this.token = token;
            this.link = link;
        }
    }

    /** Спросить targets (широковещательные и обычные адреса) и собрать ответы за waitMs. */
    static List<Core> ask(List<InetAddress> targets, int port, int waitMs) throws Exception {
        String nonce = nonce();
        byte[] req = Arrays.copyOf((MAGIC + " " + nonce).getBytes(StandardCharsets.US_ASCII), REQUEST_MIN);
        for (int i = MAGIC.length() + 1 + nonce.length(); i < req.length; i++) req[i] = ' ';
        Map<String, Core> found = new LinkedHashMap<>();
        try (DatagramSocket sock = new DatagramSocket()) {
            sock.setBroadcast(true);
            long start = System.currentTimeMillis();
            int sent = 0;
            byte[] buf = new byte[2048];
            while (true) {
                long now = System.currentTimeMillis() - start;
                if (now >= waitMs) break;
                while (sent < SEND_AT_MS.length && SEND_AT_MS[sent] <= now) {
                    for (InetAddress t : targets) {
                        try {
                            sock.send(new DatagramPacket(req, req.length, t, port));
                        } catch (Exception ignored) {
                            // сеть без широковещания или адрес недоступен — остальные цели
                        }
                    }
                    sent++;
                }
                long next = sent < SEND_AT_MS.length ? SEND_AT_MS[sent] : waitMs;
                sock.setSoTimeout((int) Math.max(1, Math.min(next, waitMs) - now));
                DatagramPacket p = new DatagramPacket(buf, buf.length);
                try {
                    sock.receive(p);
                } catch (SocketTimeoutException e) {
                    continue;
                }
                Core c = parse(new String(p.getData(), p.getOffset(), p.getLength(), StandardCharsets.UTF_8),
                        nonce, p.getAddress().getHostAddress());
                if (c != null) found.put(c.host + "|" + c.api + "|" + c.link, c);
            }
        }
        return new ArrayList<>(found.values());
    }

    // Ответ ядра на наш запрос → Core; чужое (не тот nonce, не JSON) — null
    static Core parse(String text, String nonce, String host) {
        try {
            JSONObject o = new JSONObject(text);
            if (!PROTO.equals(o.optString("vpc")) || !nonce.equals(o.optString("nonce"))) return null;
            int api = o.isNull("api") ? 0 : o.optInt("api", 0);
            int link = o.isNull("link") ? 0 : o.optInt("link", 0);
            if (api <= 0 && link <= 0) return null;
            String name = o.optString("name", "").trim();
            String id = o.optString("id", "");
            if (!id.matches("[A-Za-z0-9]{0,64}")) id = "";
            return new Core(host, id, name.isEmpty() ? host : name, Math.max(api, 0), o.optBoolean("token", false),
                    Math.max(link, 0));
        } catch (Exception e) {
            return null;
        }
    }

    private static String nonce() {
        byte[] b = new byte[8];
        new SecureRandom().nextBytes(b);
        StringBuilder sb = new StringBuilder();
        for (byte x : b) sb.append(String.format("%02x", x & 0xff));
        return sb.toString();
    }
}
