package io.vpcore.app;

import android.app.Notification;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.provider.Settings;
import android.util.Log;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Исполнение команды телефону, разобранной голосом (PhoneVoiceJs): ops —
 * ровно те вызовы плагина, что сделал бы чат ({"op":"torch","on":true},
 * {"op":"setTimer","seconds":300} …), исполняет PhoneActionsCore.
 *
 * Android 10+ не даёт службе открывать экраны (часы, «открой X»), пока
 * приложение не на экране, — разве что с правом «Поверх других приложений».
 * Без него экран не открываем (система молча проглотила бы запуск): итог —
 * background, а в шторке — уведомление, нажатие на которое откроет экран
 * (запуск по нажатию Android разрешает). Фонарик — не экран, работает и с
 * погасшим экраном.
 *
 * say — что сказать по итогу самой службе, когда ответить некому (ядро
 * недоступно, а приложение без него не ответило).
 */
final class PhoneVoiceOps {

    private static final String TAG = "WakeWord";
    private static final String TAP_TAG = "vpc-voice-tap";
    // Нажать «открыть» имеет смысл, пока просьба свежая
    private static final long TAP_TTL_MS = 5 * 60_000L;

    private PhoneVoiceOps() {}

    /** Служба может открыть чужой экран: приложение на экране, право «Поверх
     * других приложений» или Android до 10 (запрета ещё нет). */
    static boolean canStartActivities(Context ctx) {
        if (Build.VERSION.SDK_INT < 29) return true;
        return BackgroundInboxService.appVisible || Settings.canDrawOverlays(ctx);
    }

    /** Исполнить по порядку; null — всё сделано, иначе статус итога для
     * персоны. report — итог разбора (для подписи уведомления «нажмите»). */
    static String run(Context ctx, JSONArray ops, JSONObject report) {
        for (int i = 0; ops != null && i < ops.length(); i++) {
            JSONObject o = ops.optJSONObject(i);
            String op = o == null ? "" : o.optString("op", "");
            try {
                Intent intent = intentFor(ctx, op, o);
                if (intent == null) continue; // фонарик — уже сделано
                PhoneActionsCore.handler(ctx, intent);
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                if (!canStartActivities(ctx)) {
                    Log.i(TAG, "команда телефону: " + op + " — экран из фона не открыть (нет права поверх других)");
                    offerTap(ctx, intent, report);
                    return "background";
                }
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

    /** Уведомление «нажмите — откроется»: из фона экран не открыть, а по
     * нажатию на уведомление — можно. */
    private static void offerTap(Context ctx, Intent intent, JSONObject report) {
        BackgroundInboxService.createChannels(ctx);
        PendingIntent pi = PendingIntent.getActivity(ctx, 0x7a9, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification n = new NotificationCompat.Builder(ctx, BackgroundInboxService.CHANNEL_MESSAGES)
                .setSmallIcon(R.drawable.ic_stat_vpc)
                .setContentTitle(tapTitle(ctx, report))
                .setContentText(ctx.getString(R.string.wake_tap_text))
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setTimeoutAfter(TAP_TTL_MS)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_RECOMMENDATION)
                .build();
        try {
            NotificationManagerCompat.from(ctx).notify(TAP_TAG, 0, n);
        } catch (SecurityException e) {
            // уведомления запрещены — остаётся открыть приложение
        }
    }

    private static String tapTitle(Context ctx, JSONObject r) {
        String kind = r == null ? "" : r.optString("kind", "");
        switch (kind) {
            case "open_app":
                return ctx.getString(R.string.wake_tap_open, r.optString("app", ""));
            case "alarm":
                return ctx.getString(R.string.wake_tap_alarm, hm(r));
            case "timer":
                return ctx.getString(R.string.wake_tap_timer);
            default:
                return ctx.getString(R.string.wake_tap_alarms);
        }
    }

    /** Что сказать по итогу команды без персоны (ядро недоступно, ответить
     * без него некому). */
    static String say(Context ctx, JSONObject r) {
        String kind = r.optString("kind", "");
        String status = r.optString("status", "");
        switch (status) {
            case "ok":
                switch (kind) {
                    case "open_app":
                        return ctx.getString(R.string.wake_act_open_ok, r.optString("app", ""));
                    case "alarm":
                        return ctx.getString(R.string.wake_act_alarm_ok, hm(r));
                    case "timer":
                        return ctx.getString(R.string.wake_act_timer_ok);
                    case "torch":
                        return ctx.getString(r.optBoolean("on") ? R.string.wake_act_torch_on : R.string.wake_act_torch_off);
                    default:
                        return ctx.getString(R.string.wake_act_alarms_ok);
                }
            case "ask":
                return ctx.getString("timer".equals(kind) ? R.string.wake_act_ask_timer : R.string.wake_act_ask_alarm);
            case "background":
                return ctx.getString(R.string.wake_act_background);
            case "not_found":
                return ctx.getString(R.string.wake_act_not_found);
            case "ambiguous": {
                JSONArray c = r.optJSONArray("candidates");
                List<String> names = new ArrayList<>();
                for (int i = 0; c != null && i < c.length(); i++) names.add(c.optString(i));
                return ctx.getString(R.string.wake_act_ambiguous, String.join(", ", names));
            }
            case "no_handler":
                return ctx.getString(R.string.wake_act_no_handler);
            case "unsupported_date":
                return ctx.getString(R.string.wake_act_unsupported_date);
            case "busy":
                return ctx.getString(R.string.wake_act_busy);
            default:
                return ctx.getString(R.string.wake_act_failed);
        }
    }

    private static String hm(JSONObject r) {
        return String.format(Locale.ROOT, "%d:%02d", r.optInt("hour"), r.optInt("minute"));
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
