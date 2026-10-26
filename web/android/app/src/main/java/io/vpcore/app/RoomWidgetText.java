package io.vpcore.app;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Iterator;

/**
 * Что показать в виджете комнаты по снимку GET /room — по тем же правилам,
 * что у раздела «Комната» (web/src/room/useRoomScene.ts): подпись — занятие
 * (pastime или подпись места) и место (нет в комнате — локация), во время
 * «поработать вместе» — «работаем вместе» за столом; для картинки — место и
 * поза персоны. Состояния нет — персона у стола, как в вебе.
 */
final class RoomWidgetText {

    String activity = "";
    String spot = "desk";
    String pose = "stand";
    String itemZone = null; // зона предмета — у мест item:…

    static RoomWidgetText of(Context ctx, JSONObject snap) {
        RoomWidgetText t = new RoomWidgetText();
        if (snap == null) return t;
        JSONObject config = snap.optJSONObject("config");
        JSONArray spots = config == null ? null : config.optJSONArray("spots");
        JSONObject living = snap.optJSONObject("living");
        boolean lifeOn = living != null && living.optBoolean("enabled", false) && living.optBoolean("ui_sync", true);
        JSONObject state = lifeOn ? living.optJSONObject("state") : null;
        JSONObject focus = snap.optJSONObject("focus");
        t.spot = restSpot(spots);

        if (state != null) {
            String spot = state.optString("spot", "");
            if (!"away".equals(spot) && findSpot(spots, spot) == null) spot = restSpot(spots);
            JSONObject spotCfg = findSpot(spots, spot);
            t.spot = spot;
            t.pose = "away".equals(spot) ? "away"
                    : nonEmpty(str(state, "pose"), spotCfg == null ? "stand" : str(spotCfg, "pose"));
            String label = nonEmpty(str(state, "pastime"), spotCfg == null ? "" : str(spotCfg, "label"));
            String place = "away".equals(spot)
                    ? str(state, "location")
                    : nonEmpty(spotCfg == null ? "" : str(spotCfg, "place"), str(state, "location"));
            t.activity = "away".equals(spot)
                    ? join(ctx.getString(R.string.room_w_away), place)
                    : join(label.isEmpty() ? "—" : label, place);
            if (spot.startsWith("item:")) t.itemZone = itemZone(snap, spot.substring(5));
        } else {
            // Как у веба: жизнь выключена или комната не следует за ней (ui_sync)
            t.activity = ctx.getString(lifeOn ? R.string.room_w_no_state : R.string.room_w_life_off);
        }

        // Сессия «поработать вместе»: персона за столом
        if (focus != null && focus.optBoolean("active", false)) {
            JSONObject desk = findSpot(spots, "desk");
            t.activity = join(ctx.getString(R.string.room_w_focus), desk != null ? str(desk, "place") : "");
            t.spot = desk != null ? "desk" : restSpot(spots);
            t.pose = desk != null ? "write" : "read";
        }
        return t;
    }

    private static JSONObject findSpot(JSONArray spots, String key) {
        if (spots == null || key == null || key.isEmpty()) return null;
        for (int i = 0; i < spots.length(); i++) {
            JSONObject s = spots.optJSONObject(i);
            if (s != null && key.equals(s.optString("key"))) return s;
        }
        return null;
    }

    // Место «по умолчанию»: стол, иначе первое место в комнате
    private static String restSpot(JSONArray spots) {
        if (findSpot(spots, "desk") != null) return "desk";
        if (spots != null) {
            for (int i = 0; i < spots.length(); i++) {
                JSONObject s = spots.optJSONObject(i);
                String k = s == null ? "" : s.optString("key");
                if (!k.isEmpty() && !"away".equals(k) && !"bed".equals(k)) return k;
            }
        }
        return "desk";
    }

    // Зона предмета (placements снимка): на картинке персона у него
    private static String itemZone(JSONObject snap, String item) {
        JSONObject placements = snap.optJSONObject("placements");
        if (placements == null) return null;
        Iterator<String> it = placements.keys();
        while (it.hasNext()) {
            String name = it.next();
            if (!name.equalsIgnoreCase(item)) continue;
            String zone = str(placements.optJSONObject(name), "zone");
            return zone.isEmpty() ? null : zone;
        }
        return null;
    }

    // optString отдаёт "null" для JSON null — здесь это пусто
    private static String str(JSONObject o, String key) {
        if (o == null || o.isNull(key)) return "";
        return o.optString(key, "").trim();
    }

    private static String nonEmpty(String a, String b) {
        return a != null && !a.isEmpty() ? a : (b == null ? "" : b);
    }

    private static String join(String a, String b) {
        if (b == null || b.isEmpty() || b.equalsIgnoreCase(a)) return a;
        return a + " · " + b;
    }
}
