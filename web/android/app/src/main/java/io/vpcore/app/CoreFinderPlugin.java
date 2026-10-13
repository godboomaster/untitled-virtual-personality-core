package io.vpcore.app;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.json.JSONObject;

/* «Найти» на экране подключения (BootGate): ядро в той же сети Wi-Fi.
   Берём свой IPv4 на Wi-Fi/Ethernet/точке доступа (мобильная сеть не в
   счёт — там ядра нет) и опрашиваем соседей по /24 на GET /api/health:
   ядро отвечает {"status":"ok"} без токена. Сеть шире /24 сужаем до своей
   /24 — иначе десятки тысяч адресов. Отказ no_wifi — телефон не в локальной
   сети. Из веба такой опрос не сделать: WebView не знает своего адреса. */
@CapacitorPlugin(name = "CoreFinder")
public class CoreFinderPlugin extends Plugin {

    private static final int CONNECT_MS = 800;
    private static final int READ_MS = 2000;
    private static final int THREADS = 64;

    @PluginMethod
    public void find(PluginCall call) {
        int port = call.getInt("port", 8000);
        new Thread(() -> {
            List<int[]> ranges = new ArrayList<>(); // {от, до, свой} — адреса как int
            Set<String> nets = new TreeSet<>(); // Wi-Fi и Ethernet бывают в одной сети
            for (InterfaceAddress ia : localAddresses()) {
                int self = toInt(ia.getAddress());
                int prefix = Math.max(24, Math.min(30, ia.getNetworkPrefixLength()));
                int mask = -1 << (32 - prefix);
                int net = self & mask;
                if (nets.add(toStr(net) + "/" + prefix)) ranges.add(new int[] {net + 1, (net | ~mask) - 1, self});
            }
            if (ranges.isEmpty()) {
                call.reject("phone is not on Wi-Fi", "no_wifi");
                return;
            }
            Set<String> found = Collections.synchronizedSet(new TreeSet<>());
            ExecutorService pool = Executors.newFixedThreadPool(THREADS);
            for (int[] r : ranges) {
                for (int ip = r[0]; ip <= r[1]; ip++) {
                    if (ip == r[2]) continue;
                    String host = toStr(ip) + ":" + port;
                    pool.execute(() -> {
                        if (isCore(host)) found.add(host);
                    });
                }
            }
            pool.shutdown();
            try {
                pool.awaitTermination(30, TimeUnit.SECONDS);
            } catch (InterruptedException ignored) {
                pool.shutdownNow();
            }
            JSArray hosts = new JSArray();
            synchronized (found) {
                for (String h : found) hosts.put(h);
            }
            JSObject res = new JSObject();
            res.put("found", hosts);
            res.put("nets", new JSArray(nets));
            call.resolve(res);
        }, "core-finder").start();
    }

    // Отвечает ли на host ядро: /api/health → {"status":"ok"}
    private static boolean isCore(String host) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL("http://" + host + "/api/health").openConnection();
            c.setConnectTimeout(CONNECT_MS);
            c.setReadTimeout(READ_MS);
            c.setInstanceFollowRedirects(false);
            if (c.getResponseCode() != 200) return false;
            byte[] buf = new byte[512];
            int n = 0;
            try (InputStream in = c.getInputStream()) {
                for (int k; n < buf.length && (k = in.read(buf, n, buf.length - n)) > 0; ) n += k;
            }
            return "ok".equals(new JSONObject(new String(buf, 0, n, StandardCharsets.UTF_8)).optString("status"));
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    // Свои частные IPv4 на Wi-Fi, Ethernet и точке доступа телефона
    private static List<InterfaceAddress> localAddresses() {
        List<InterfaceAddress> out = new ArrayList<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback() || ni.isVirtual()) continue;
                String name = ni.getName().toLowerCase(Locale.ROOT);
                if (!(name.startsWith("wlan") || name.startsWith("eth") || name.startsWith("ap")
                        || name.startsWith("swlan") || name.startsWith("softap"))) continue;
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    InetAddress a = ia.getAddress();
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) out.add(ia);
                }
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    private static int toInt(InetAddress a) {
        byte[] b = a.getAddress();
        return (b[0] & 0xff) << 24 | (b[1] & 0xff) << 16 | (b[2] & 0xff) << 8 | (b[3] & 0xff);
    }

    private static String toStr(int ip) {
        return (ip >>> 24) + "." + ((ip >>> 16) & 0xff) + "." + ((ip >>> 8) & 0xff) + "." + (ip & 0xff);
    }
}
