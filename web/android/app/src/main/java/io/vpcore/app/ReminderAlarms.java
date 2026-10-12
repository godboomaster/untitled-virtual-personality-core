package io.vpcore.app;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.util.Log;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.HashSet;
import java.util.Set;

/**
 * Напоминания будильником телефона — чтобы они срабатывали и без ноутбука.
 *
 * Список приходит из приложения (OfflineLlmPlugin.syncReminders): ближайшие
 * срабатывания напоминаний ядра (метка «id@время» — та же, что ядро пришлёт
 * в inbox) и напоминания, поставленные на телефоне без ноутбука («local:…»).
 *
 * Срабатывание (ReminderReceiver): ноутбук в сети (проба TCP по адресам
 * подключения) — ждём DEFER_MS: ядро само пришлёт напоминание текстом
 * персоны, фоновая служба отметит его доставленным, и телефон промолчит. Не
 * пришло или ноутбука нет — уведомление от телефона. Показанное телефоном
 * ядро потом пришлёт ещё раз (догонит, вернувшись) — служба по метке его не
 * покажет (wasFired), а в чате оно останется.
 */
final class ReminderAlarms {

    private static final String TAG = "ReminderAlarms";
    private static final String PREFS = "vpc_reminders";
    private static final String KEY_ITEMS = "items";
    private static final String KEY_PROBES = "probes";
    private static final String KEY_FIRED = "fired";
    private static final String KEY_DELIVERED = "delivered";
    private static final String KEY_FIRED_LOG = "firedLog";

    static final String ACTION_FIRE = "io.vpcore.app.REMINDER_FIRE";
    static final String EXTRA_REF = "ref";
    static final String EXTRA_DEFERRED = "deferred";

    // Ноутбук в сети — столько ждём, что напоминание придёт от него самого
    private static final long DEFER_MS = 75_000L;
    // После перезагрузки телефона пропущенное напоминание старше — не показываем
    private static final long MISSED_MAX_MS = 12 * 3600_000L;
    private static final int KEEP_REFS = 400;
    private static final int KEEP_LOG = 50;
    private static final int PROBE_TIMEOUT_MS = 1500;
    private static final int MAX_PROBES = 3;
    private static final String TAG_NOTIFY = "vpc-rem:";

    private static final Object LOCK = new Object();

    private ReminderAlarms() {
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static JSONArray readArray(SharedPreferences p, String key) {
        try {
            return new JSONArray(p.getString(key, "[]"));
        } catch (JSONException e) {
            return new JSONArray();
        }
    }

    private static Set<String> readSet(SharedPreferences p, String key) {
        JSONArray a = readArray(p, key);
        Set<String> out = new HashSet<>();
        for (int i = 0; i < a.length(); i++) out.add(a.optString(i));
        return out;
    }

    /** Добавить метку в ограниченный список (старые вытесняются). */
    private static void appendRef(SharedPreferences p, String key, String ref) {
        JSONArray a = readArray(p, key);
        for (int i = 0; i < a.length(); i++) if (ref.equals(a.optString(i))) return;
        a.put(ref);
        JSONArray trimmed = new JSONArray();
        for (int i = Math.max(0, a.length() - KEEP_REFS); i < a.length(); i++) trimmed.put(a.optString(i));
        p.edit().putString(key, trimmed.toString()).apply();
    }

    static boolean canExact(Context ctx) {
        AlarmManager am = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        if (am == null) return false;
        return Build.VERSION.SDK_INT < 31 || am.canScheduleExactAlarms();
    }

    private static PendingIntent pending(Context ctx, String ref, boolean deferred, int extraFlags) {
        Intent i = new Intent(ctx, ReminderReceiver.class)
                .setAction(ACTION_FIRE)
                // data делает интент каждого напоминания отдельным для PendingIntent
                .setData(Uri.parse("vpc-reminder://" + Uri.encode(ref)))
                .putExtra(EXTRA_REF, ref)
                .putExtra(EXTRA_DEFERRED, deferred);
        return PendingIntent.getBroadcast(ctx, ref.hashCode(), i,
                extraFlags | PendingIntent.FLAG_IMMUTABLE);
    }

    private static void arm(Context ctx, String ref, long whenMs, boolean deferred) {
        AlarmManager am = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        if (am == null) return;
        PendingIntent pi = pending(ctx, ref, deferred, PendingIntent.FLAG_UPDATE_CURRENT);
        try {
            if (canExact(ctx)) {
                am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, whenMs, pi);
            } else {
                // Без точных будильников система может сдвинуть срабатывание
                am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, whenMs, pi);
            }
        } catch (SecurityException e) {
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, whenMs, pi);
        }
    }

    private static void disarm(Context ctx, String ref) {
        AlarmManager am = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        PendingIntent pi = pending(ctx, ref, false, PendingIntent.FLAG_NO_CREATE);
        if (am != null && pi != null) {
            am.cancel(pi);
            pi.cancel();
        }
    }

    /**
     * Новый список напоминаний: снятые — отменить, остальные — поставить.
     * items: [{ref, persona, name, text, at (unix-секунды)}]; probes —
     * адреса ноутбука «host:port». → сколько будильников стоит.
     */
    static int sync(Context ctx, JSONArray items, JSONArray probes) {
        synchronized (LOCK) {
            SharedPreferences p = prefs(ctx);
            JSONArray old = readArray(p, KEY_ITEMS);
            Set<String> keep = new HashSet<>();
            for (int i = 0; i < items.length(); i++) {
                JSONObject e = items.optJSONObject(i);
                if (e != null) keep.add(e.optString("ref"));
            }
            for (int i = 0; i < old.length(); i++) {
                JSONObject e = old.optJSONObject(i);
                if (e != null && !keep.contains(e.optString("ref"))) disarm(ctx, e.optString("ref"));
            }
            p.edit().putString(KEY_ITEMS, items.toString())
                    .putString(KEY_PROBES, probes == null ? "[]" : probes.toString()).apply();
            return armAll(ctx, false);
        }
    }

    /** Поставить будильники всем будущим. missed — после перезагрузки:
     * пропущенные за последние MISSED_MAX_MS показать сейчас. */
    static int armAll(Context ctx, boolean missed) {
        synchronized (LOCK) {
            SharedPreferences p = prefs(ctx);
            JSONArray items = readArray(p, KEY_ITEMS);
            Set<String> done = readSet(p, KEY_FIRED);
            done.addAll(readSet(p, KEY_DELIVERED));
            long now = System.currentTimeMillis();
            int armed = 0;
            for (int i = 0; i < items.length(); i++) {
                JSONObject e = items.optJSONObject(i);
                if (e == null) continue;
                String ref = e.optString("ref");
                if (ref.isEmpty() || done.contains(ref)) continue;
                long at = (long) (e.optDouble("at", 0) * 1000);
                if (at > now) {
                    arm(ctx, ref, at, false);
                    armed++;
                } else if (missed && now - at < MISSED_MAX_MS) {
                    arm(ctx, ref, now + 5_000, false);
                    armed++;
                }
            }
            return armed;
        }
    }

    static int pendingCount(Context ctx) {
        synchronized (LOCK) {
            SharedPreferences p = prefs(ctx);
            JSONArray items = readArray(p, KEY_ITEMS);
            Set<String> done = readSet(p, KEY_FIRED);
            done.addAll(readSet(p, KEY_DELIVERED));
            long now = System.currentTimeMillis();
            int n = 0;
            for (int i = 0; i < items.length(); i++) {
                JSONObject e = items.optJSONObject(i);
                if (e != null && !done.contains(e.optString("ref"))
                        && e.optDouble("at", 0) * 1000 > now) n++;
            }
            return n;
        }
    }

    /** Ядро прислало это напоминание само — телефону показывать не нужно. */
    static void markDelivered(Context ctx, String ref) {
        if (ref == null || ref.isEmpty()) return;
        synchronized (LOCK) {
            appendRef(prefs(ctx), KEY_DELIVERED, ref);
        }
        disarm(ctx, ref);
    }

    /** Телефон уже показал это напоминание сам. */
    static boolean wasFired(Context ctx, String ref) {
        if (ref == null || ref.isEmpty()) return false;
        synchronized (LOCK) {
            return readSet(prefs(ctx), KEY_FIRED).contains(ref);
        }
    }

    /** Показанные телефоном напоминания (последние) — для ленты чата без ноутбука. */
    static JSONArray firedLog(Context ctx) {
        synchronized (LOCK) {
            return readArray(prefs(ctx), KEY_FIRED_LOG);
        }
    }

    /** Будильник сработал (в фоне, из ReminderReceiver.goAsync). */
    static void onFire(Context ctx, String ref, boolean deferred) {
        JSONObject entry = null;
        JSONArray probes;
        synchronized (LOCK) {
            SharedPreferences p = prefs(ctx);
            if (readSet(p, KEY_FIRED).contains(ref) || readSet(p, KEY_DELIVERED).contains(ref)) return;
            JSONArray items = readArray(p, KEY_ITEMS);
            for (int i = 0; i < items.length(); i++) {
                JSONObject e = items.optJSONObject(i);
                if (e != null && ref.equals(e.optString("ref"))) entry = e;
            }
            probes = readArray(p, KEY_PROBES);
        }
        if (entry == null) return; // напоминание сняли
        // Напоминание ядра, ноутбук рядом — пусть пришлёт его сам, текстом персоны
        if (!deferred && !ref.startsWith("local:") && laptopReachable(probes)) {
            arm(ctx, ref, System.currentTimeMillis() + DEFER_MS, true);
            return;
        }
        synchronized (LOCK) {
            SharedPreferences p = prefs(ctx);
            if (readSet(p, KEY_DELIVERED).contains(ref)) return; // пришло, пока ждали
            appendRef(p, KEY_FIRED, ref);
            JSONArray log = readArray(p, KEY_FIRED_LOG);
            try {
                JSONObject rec = new JSONObject(entry.toString());
                rec.put("firedAt", System.currentTimeMillis() / 1000.0);
                log.put(rec);
            } catch (JSONException ignored) {
                // запись журнала не обязательна
            }
            JSONArray trimmed = new JSONArray();
            for (int i = Math.max(0, log.length() - KEEP_LOG); i < log.length(); i++) trimmed.put(log.opt(i));
            p.edit().putString(KEY_FIRED_LOG, trimmed.toString()).apply();
        }
        show(ctx, entry);
    }

    /** Ноутбук в той же сети: TCP-соединение с одним из адресов подключения. */
    private static boolean laptopReachable(JSONArray probes) {
        for (int i = 0; probes != null && i < Math.min(probes.length(), MAX_PROBES); i++) {
            String hp = probes.optString(i);
            int colon = hp.lastIndexOf(':');
            if (colon <= 0) continue;
            try (Socket s = new Socket()) {
                s.connect(new InetSocketAddress(hp.substring(0, colon),
                        Integer.parseInt(hp.substring(colon + 1))), PROBE_TIMEOUT_MS);
                return true;
            } catch (Exception e) {
                // нет — следующий адрес
            }
        }
        return false;
    }

    private static void show(Context ctx, JSONObject e) {
        BackgroundInboxService.createChannels(ctx);
        String persona = e.optString("persona");
        String name = e.optString("name", persona);
        String text = e.optString("text");
        Intent open = new Intent(ctx, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra(BackgroundInboxService.EXTRA_PERSONA, persona);
        PendingIntent pi = PendingIntent.getActivity(ctx, persona.hashCode(), open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification n = new NotificationCompat.Builder(ctx, BackgroundInboxService.CHANNEL_MESSAGES)
                .setSmallIcon(R.drawable.ic_stat_vpc)
                .setContentTitle(name)
                .setContentText(text)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(text))
                .setWhen((long) (e.optDouble("at", System.currentTimeMillis() / 1000.0) * 1000))
                .setShowWhen(true)
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_REMINDER)
                .build();
        try {
            NotificationManagerCompat.from(ctx).notify(TAG_NOTIFY + e.optString("ref"), 0, n);
        } catch (SecurityException ex) {
            Log.w(TAG, "уведомления запрещены — напоминание не показано");
        }
    }
}
