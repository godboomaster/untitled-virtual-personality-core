package io.vpcore.app;

import java.net.InetAddress;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONObject;

/* Обвязка теста scripts/test_core_discovery.py: CoreDiscovery.ask на заданных
   адресах, ответы — по строке JSON. Аргументы: порт, ожидание (мс), адреса. */
public class DiscoveryHarness {
    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(args[0]);
        int waitMs = Integer.parseInt(args[1]);
        List<InetAddress> targets = new ArrayList<>();
        for (int i = 2; i < args.length; i++) targets.add(InetAddress.getByName(args[i]));
        for (CoreDiscovery.Core c : CoreDiscovery.ask(targets, port, waitMs)) {
            JSONObject o = new JSONObject();
            o.put("host", c.host);
            o.put("id", c.id);
            o.put("name", c.name);
            o.put("api", c.api);
            o.put("token", c.token);
            o.put("link", c.link);
            System.out.println(o);
        }
    }
}
