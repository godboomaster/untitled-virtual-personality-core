package io.vpcore.app;

import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.provider.Settings;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * Исполнение команды телефону, разобранной голосом (PhoneVoiceJs): ops —
 * ровно те вызовы плагина, что сделал бы чат ({"op":"torch","on":true},
 * {"op":"setTimer","seconds":300} …), исполняет PhoneActionsCore.
 *
 * Android 10+ не даёт службе открывать экраны (часы, «открой X»), пока
 * приложение не на экране, — разве что с правом «Поверх других приложений».
 * Без него экран не открываем (система молча проглотила бы запуск), а итог —
 * background: персона скажет, что нужно разрешение. Фонарик — не экран,
 * работает и с погасшим экраном.
 */
final class PhoneVoiceOps {

    private static final String TAG = "WakeWord";

    private PhoneVoiceOps() {}

    /** Служба может открыть чужой экран: приложение на экране, право «Поверх
     * других приложений» или Android до 10 (запрета ещё нет). */
    static boolean canStartActivities(Context ctx) {
        if (Build.VERSION.SDK_INT < 29) return true;
        return BackgroundInboxService.appVisible || Settings.canDrawOverlays(ctx);
    }

    /** Исполнить по порядку; null — всё сделано, иначе статус итога для персоны. */
    static String run(Context ctx, JSONArray ops) {
        for (int i = 0; ops != null && i < ops.length(); i++) {
            JSONObject o = ops.optJSONObject(i);
            String op = o == null ? "" : o.optString("op", "");
            try {
                Intent intent = intentFor(ctx, op, o);
                if (intent == null) continue; // фонарик — уже сделано
                PhoneActionsCore.handler(ctx, intent);
                if (!canStartActivities(ctx)) {
                    Log.i(TAG, "команда телефону: " + op + " — экран из фона не открыть (нет права поверх других)");
                    return "background";
                }
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                PhoneActionsCore.start(ctx, intent);
            } catch (PhoneActionsCore.Failure e) {
                Log.i(TAG, "команда телефону: " + op + " — " + e.code + ": " + e.getMessage());
                return failStatus(op, e.code);
            } catch (RuntimeException e) {
                Log.w(TAG, "команда телефону: " + op + " упала", e);
                return "failed";
            }
        }
        return null;
    }

    // Экран, который надо открыть; null — сделано без экрана (фонарик)
    private static Intent intentFor(Context ctx, String op, JSONObject o) throws PhoneActionsCore.Failure {
        switch (op) {
            case "torch":
                PhoneActionsCore.torch(ctx, o.optBoolean("on", true));
                return null;
            case "setAlarm": {
                List<Integer> days = new ArrayList<>();
                JSONArray arr = o.optJSONArray("days");
                for (int i = 0; arr != null && i < arr.length(); i++) days.add(arr.optInt(i, 0));
                return PhoneActionsCore.alarmIntent(optInt(o, "hour"), optInt(o, "minute"), days,
                        o.optString("label", ""));
            }
            case "setTimer":
                return PhoneActionsCore.timerIntent(optInt(o, "seconds"), o.optString("label", ""));
            case "showAlarms":
                return PhoneActionsCore.showAlarmsIntent();
            case "openApp":
                return PhoneActionsCore.openAppIntent(ctx, o.optString("package", ""));
            default:
                throw new PhoneActionsCore.Failure("неизвестная команда: " + op, "bad_args");
        }
    }

    private static Integer optInt(JSONObject o, String key) {
        Object v = o.opt(key);
        return v instanceof Number ? ((Number) v).intValue() : null;
    }

    /** Код отказа → статус итога: как failStatus/openApp в actions.ts. */
    static String failStatus(String op, String code) {
        if ("openApp".equals(op)) return "not_found".equals(code) ? "not_found" : "failed";
        if ("no_handler".equals(code) || "busy".equals(code)) return code;
        return "failed";
    }
}
