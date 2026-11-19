package io.vpcore.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.service.notification.StatusBarNotification;
import android.text.TextUtils;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.app.Person;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Все уведомления приложения — одной стопкой в шторке.
 *
 * Общая группа GROUP и одна сводка к ней: сообщения персон, напоминания,
 * «нажмите — откроется», проверка и значки служб (связь с ядром, «слушаю
 * имя», аватар поверх приложений). Сообщения одной персоны — и с ядра, и от
 * сервисов телефона — одна карточка-переписка: новое дописывается в неё, а не
 * ложится отдельной плашкой.
 *
 * Сводку пересчитываем из того, что сейчас в шторке. notify() у Android
 * асинхронный: только что отправленное или снятое в getActiveNotifications()
 * появляется не сразу — его помним сами FRESH_MS.
 *
 * Есть значок службы — сводка тоже «постоянная»: «Очистить всё» снимает
 * сообщения, а стопка со значками остаётся стопкой, а не рассыпается на
 * отдельные плашки. Смахнули уведомление — deleteIntent сюда же
 * (ресивер в манифесте), сводка пересчитывается.
 */
public final class NotifyStack extends BroadcastReceiver {

    static final String GROUP = "io.vpcore.app.all";
    private static final String TAG_SUMMARY = "vpc-stack";
    private static final int SUMMARY_ID = 0;
    // Карточка-переписка персоны: метка + id персоны, id уведомления 0
    private static final String TAG_PERSONA = "vpc-msg:";
    // До общей стопки: сводка на каждую персону — снимаем, если остались в шторке
    private static final String TAG_OLD_SUMMARY = "vpc-sum:";
    // Экстра уведомления: чьё оно — тап по сводке откроет эту персону, если она одна
    private static final String EXTRA_PERSONA = "io.vpcore.app.notify.PERSONA";
    private static final String ACTION_DISMISSED = "io.vpcore.app.NOTIFY_DISMISSED";
    private static final String EXTRA_TAG = "tag";
    private static final String EXTRA_ID = "id";
    // Сколько реплик держит карточка персоны (старые уходят)
    private static final int MAX_LINES = 8;
    private static final int MAX_SUMMARY_LINES = 6;
    // Столько считаем отправленное/снятое «свежим»: шторка его могла ещё не учесть
    private static final long FRESH_MS = 3_000;

    /** Реплика для карточки персоны. */
    static final class Line {
        final String text;
        final long when;

        Line(String text, long when) {
            this.text = text == null ? "" : text;
            this.when = when;
        }
    }

    private static final class Card {
        final List<Line> lines = new ArrayList<>();
        long postedAt;
    }

    /** Только что отправлено (n != null) или снято (n == null). */
    private static final class Event {
        final String tag;
        final int id;
        final Notification n;
        final long at;
        final long wall;

        Event(String tag, int id, Notification n) {
            this.tag = tag;
            this.id = id;
            this.n = n;
            this.at = SystemClock.elapsedRealtime();
            this.wall = System.currentTimeMillis();
        }
    }

    private static final class Child {
        final Notification n;
        final long postedWall;

        Child(Notification n, long postedWall) {
            this.n = n;
            this.postedWall = postedWall;
        }
    }

    private static final Object LOCK = new Object();
    // Переписки персон, пока карточка в шторке (по id персоны)
    private static final Map<String, Card> cards = new HashMap<>();
    private static final Map<String, Event> recent = new HashMap<>();
    // Что сейчас в сводке и когда она отправлена — одно и то же не шлём:
    // у Android предел обновлений уведомлений в секунду, лишние он отбрасывает
    private static String shownSummary = null;
    private static long summaryAt = 0;
    // Смахивания, ждущие общего пересчёта (onReceive)
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static final long DISMISS_COALESCE_MS = 300;
    private static final List<PendingResult> dismissWaiting = new ArrayList<>();
    private static boolean dismissQueued = false;

    // ── Отправка ─────────────────────────────────────────────────────

    /** Реплики персоны — в её карточку-переписку. Прежнюю смахнули или
     * открыли — начинается новая. */
    static void personaMessages(Context ctx, String persona, String name, List<Line> lines) {
        if (persona == null || persona.isEmpty() || lines == null || lines.isEmpty()) return;
        if (name == null || name.isEmpty()) name = persona;
        BackgroundInboxService.createChannels(ctx);
        String tag = TAG_PERSONA + persona;
        synchronized (LOCK) {
            long now = SystemClock.elapsedRealtime();
            StatusBarNotification shown = findActive(ctx, tag, 0);
            Card card = cards.get(persona);
            // Карточки нет в шторке и отправлена она не только что — её смахнули
            // или открыли: переписка начинается заново
            if (shown == null && card != null && now - card.postedAt > FRESH_MS) card = null;
            if (card == null) {
                card = new Card();
                // Процесс перезапускался, а карточка висит — продолжаем её
                if (shown != null) card.lines.addAll(linesOf(shown.getNotification()));
                cards.put(persona, card);
            }
            card.lines.addAll(lines);
            while (card.lines.size() > MAX_LINES) card.lines.remove(0);

            Person me = new Person.Builder().setName(ctx.getString(R.string.notify_you)).build();
            Person who = new Person.Builder().setName(name).setKey(persona).build();
            NotificationCompat.MessagingStyle style = new NotificationCompat.MessagingStyle(me);
            for (Line l : card.lines) style.addMessage(l.text, l.when, who);
            Line last = card.lines.get(card.lines.size() - 1);

            // Тап — чат персоны (requestCode по персоне: у каждой свой PendingIntent)
            Intent open = new Intent(ctx, MainActivity.class)
                    .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                    .putExtra(BackgroundInboxService.EXTRA_PERSONA, persona);
            PendingIntent pi = PendingIntent.getActivity(ctx, persona.hashCode(), open,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            NotificationCompat.Builder b = new NotificationCompat.Builder(ctx, BackgroundInboxService.CHANNEL_MESSAGES)
                    .setSmallIcon(R.drawable.ic_stat_vpc)
                    .setContentTitle(name)
                    .setContentText(last.text)
                    .setStyle(style)
                    .setWhen(last.when)
                    .setShowWhen(true)
                    .setContentIntent(pi)
                    .setAutoCancel(true)
                    .setPriority(NotificationCompat.PRIORITY_HIGH)
                    .setCategory(NotificationCompat.CATEGORY_MESSAGE);
            if (postLocked(ctx, tag, 0, b, persona)) card.postedAt = now;
        }
    }

    /** Уведомление в общую стопку. persona — чьё (для тапа по сводке),
     * null — ничьё. → false — уведомления приложению запрещены. */
    static boolean post(Context ctx, String tag, int id, NotificationCompat.Builder b, String persona) {
        synchronized (LOCK) {
            return postLocked(ctx, tag, id, b, persona);
        }
    }

    private static boolean postLocked(Context ctx, String tag, int id, NotificationCompat.Builder b, String persona) {
        b.setGroup(GROUP).setDeleteIntent(dismissIntent(ctx, tag, id));
        if (persona != null && !persona.isEmpty()) {
            Bundle x = new Bundle();
            x.putString(EXTRA_PERSONA, persona);
            b.addExtras(x);
        }
        Notification n = b.build();
        try {
            NotificationManagerCompat.from(ctx).notify(tag, id, n);
        } catch (SecurityException e) {
            return false;
        }
        recent.put(key(tag, id), new Event(tag, id, n));
        refreshLocked(ctx);
        return true;
    }

    /** Значок службы показан или обновлён (startForeground / notify с тем же
     * id): его уведомление собрано с setGroup(GROUP). */
    static void shown(Context ctx, int id, Notification n) {
        synchronized (LOCK) {
            recent.put(key(null, id), new Event(null, id, n));
            refreshLocked(ctx);
        }
    }

    /** Уведомление снято приложением (значок службы — по id, tag = null). */
    static void gone(Context ctx, String tag, int id) {
        synchronized (LOCK) {
            recent.put(key(tag, id), new Event(tag, id, null));
            refreshLocked(ctx);
        }
    }

    /** Приложение на экране: сообщения персон теперь видно в чате — их
     * карточки снимаем (и сводки прежних версий), значки служб остаются. */
    static void clearMessages(Context ctx) {
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        synchronized (LOCK) {
            cards.clear();
            Set<String> done = new HashSet<>();
            try {
                for (StatusBarNotification sbn : nm.getActiveNotifications()) {
                    String tag = sbn.getTag();
                    if (tag != null && (tag.startsWith(TAG_PERSONA) || tag.startsWith(TAG_OLD_SUMMARY))) {
                        nm.cancel(tag, sbn.getId());
                        done.add(key(tag, sbn.getId()));
                    }
                }
            } catch (RuntimeException e) {
                // не критично
            }
            // Отправленные только что шторка могла ещё не показать — снять и их
            for (Event e : new ArrayList<>(recent.values())) {
                if (e.n == null || e.tag == null || !e.tag.startsWith(TAG_PERSONA)) continue;
                if (!done.contains(key(e.tag, e.id))) nm.cancel(e.tag, e.id);
                done.add(key(e.tag, e.id));
            }
            for (String k : done) {
                int hash = k.lastIndexOf('#');
                recent.put(k, new Event(k.substring(0, hash), Integer.parseInt(k.substring(hash + 1)), null));
            }
            refreshLocked(ctx);
        }
    }

    /** Смахнули (по одному или «Очистить всё») — пересчитать сводку.
     * «Очистить всё» присылает пачку таких разом — пересчёт один, чуть позже. */
    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (intent == null || !ACTION_DISMISSED.equals(intent.getAction())) return;
        String tag = intent.getStringExtra(EXTRA_TAG);
        int id = intent.getIntExtra(EXTRA_ID, 0);
        final Context app = ctx.getApplicationContext();
        PendingResult pr = goAsync();
        synchronized (LOCK) {
            if (tag != null && tag.startsWith(TAG_PERSONA)) cards.remove(tag.substring(TAG_PERSONA.length()));
            recent.put(key(tag, id), new Event(tag, id, null));
            dismissWaiting.add(pr);
            if (dismissQueued) return;
            dismissQueued = true;
        }
        MAIN.postDelayed(() -> {
            List<PendingResult> done;
            synchronized (LOCK) {
                dismissQueued = false;
                refreshLocked(app);
                done = new ArrayList<>(dismissWaiting);
                dismissWaiting.clear();
            }
            for (PendingResult r : done) r.finish();
        }, DISMISS_COALESCE_MS);
    }

    // ── Сводка ───────────────────────────────────────────────────────

    private static void refreshLocked(Context ctx) {
        NotificationManager sys = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (sys == null) return;
        long now = SystemClock.elapsedRealtime();
        long wall = System.currentTimeMillis();
        Map<String, Child> kids = new LinkedHashMap<>();
        boolean summaryVisible = false;
        try {
            for (StatusBarNotification sbn : sys.getActiveNotifications()) {
                Notification n = sbn.getNotification();
                if (!GROUP.equals(n.getGroup())) continue;
                if ((n.flags & Notification.FLAG_GROUP_SUMMARY) != 0) {
                    if (TAG_SUMMARY.equals(sbn.getTag())) summaryVisible = true;
                    continue;
                }
                kids.put(key(sbn.getTag(), sbn.getId()), new Child(n, sbn.getPostTime()));
            }
        } catch (RuntimeException e) {
            // шторка недоступна — считаем по тому, что отправили сами
        }
        Iterator<Event> it = recent.values().iterator();
        while (it.hasNext()) {
            Event e = it.next();
            if (now - e.at > FRESH_MS) {
                it.remove();
                continue;
            }
            String k = key(e.tag, e.id);
            if (e.n == null) kids.remove(k);
            else kids.put(k, new Child(e.n, e.wall));
        }

        NotificationManagerCompat nm = NotificationManagerCompat.from(ctx);
        if (kids.isEmpty()) {
            // Сводка без уведомлений Android показывает отдельной плашкой — снять
            if (summaryVisible || shownSummary != null) nm.cancel(TAG_SUMMARY, SUMMARY_ID);
            shownSummary = null;
            return;
        }

        // Сообщения — сверху, свежие первыми; значки служб — отдельно
        List<Child> alert = new ArrayList<>();
        List<Child> quiet = new ArrayList<>();
        for (Child c : kids.values()) (isOngoing(c.n) ? quiet : alert).add(c);
        Collections.sort(alert, (a, b) -> Long.compare(b.n.when, a.n.when));

        String channel;
        String title;
        String text;
        List<String> lines = new ArrayList<>();
        String persona = null;
        boolean onePersona = true;
        long timeout = 0;
        if (!alert.isEmpty()) {
            channel = BackgroundInboxService.CHANNEL_MESSAGES;
            Child top = alert.get(0);
            title = alert.size() == 1 ? titleOf(top.n) : ctx.getString(R.string.app_name);
            text = alert.size() == 1 ? textOf(top.n) : lineOf(top.n);
            for (Child c : alert) {
                if (lines.size() < MAX_SUMMARY_LINES) lines.add(lineOf(c.n));
                String p = c.n.extras == null ? null : c.n.extras.getString(EXTRA_PERSONA);
                if (p == null || (persona != null && !persona.equals(p))) onePersona = false;
                else persona = p;
            }
            // Только временные («нажмите — откроется») и нет значков служб —
            // сводка уходит вместе с последним из них
            if (quiet.isEmpty()) {
                long deadline = 0;
                for (Child c : alert) {
                    long t = NotificationCompat.getTimeoutAfter(c.n);
                    if (t <= 0) {
                        deadline = 0;
                        break;
                    }
                    deadline = Math.max(deadline, c.postedWall + t);
                }
                if (deadline > 0) timeout = Math.max(1_000, deadline - wall);
            }
        } else {
            channel = loudestChannel(sys, quiet);
            title = ctx.getString(R.string.app_name);
            List<String> names = new ArrayList<>();
            for (Child c : quiet) names.add(titleOf(c.n));
            text = TextUtils.join(" · ", names);
            for (String s : names) if (lines.size() < MAX_SUMMARY_LINES) lines.add(s);
        }
        if (!onePersona) persona = null;
        boolean ongoing = !quiet.isEmpty();
        long when = alert.isEmpty() ? 0 : alert.get(0).n.when;

        String summaryKey = channel + "|" + ongoing + "|" + title + "|" + text + "|" + lines
                + "|" + persona + "|" + (timeout > 0 ? (wall + timeout) / 10_000 : 0) + "|" + when;
        if (summaryKey.equals(shownSummary) && (summaryVisible || now - summaryAt < FRESH_MS)) return;

        Intent open = new Intent(ctx, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        if (persona != null) open.putExtra(BackgroundInboxService.EXTRA_PERSONA, persona);
        PendingIntent pi = PendingIntent.getActivity(ctx, 0x57AC, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        NotificationCompat.InboxStyle inbox = new NotificationCompat.InboxStyle();
        for (String s : lines) inbox.addLine(s);
        NotificationCompat.Builder b = new NotificationCompat.Builder(ctx, channel)
                .setSmallIcon(R.drawable.ic_stat_vpc)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(inbox)
                .setContentIntent(pi)
                .setGroup(GROUP)
                .setGroupSummary(true)
                // Звенят сами уведомления, сводка — никогда
                .setGroupAlertBehavior(NotificationCompat.GROUP_ALERT_CHILDREN)
                .setOnlyAlertOnce(true)
                .setOngoing(ongoing)
                .setAutoCancel(!ongoing)
                .setPriority(alert.isEmpty() ? NotificationCompat.PRIORITY_LOW : NotificationCompat.PRIORITY_HIGH)
                .setCategory(alert.isEmpty() ? NotificationCompat.CATEGORY_SERVICE : NotificationCompat.CATEGORY_MESSAGE);
        if (when > 0) b.setWhen(when).setShowWhen(true);
        else b.setShowWhen(false);
        if (timeout > 0) b.setTimeoutAfter(timeout);
        try {
            nm.notify(TAG_SUMMARY, SUMMARY_ID, b.build());
            shownSummary = summaryKey;
            summaryAt = now;
        } catch (SecurityException e) {
            // уведомления запрещены — сводки нет, как и остального
        }
    }

    // ── Мелочи ───────────────────────────────────────────────────────

    private static String key(String tag, int id) {
        return (tag == null ? "" : tag) + "#" + id;
    }

    private static boolean isOngoing(Notification n) {
        return (n.flags & (Notification.FLAG_ONGOING_EVENT | Notification.FLAG_FOREGROUND_SERVICE)) != 0;
    }

    private static String titleOf(Notification n) {
        CharSequence t = n.extras == null ? null : n.extras.getCharSequence(Notification.EXTRA_TITLE);
        return t == null ? "" : t.toString();
    }

    private static String textOf(Notification n) {
        CharSequence t = n.extras == null ? null : n.extras.getCharSequence(Notification.EXTRA_TEXT);
        return t == null ? "" : t.toString();
    }

    private static String lineOf(Notification n) {
        String title = titleOf(n);
        String text = textOf(n);
        if (title.isEmpty()) return text;
        if (text.isEmpty()) return title;
        return title + ": " + text;
    }

    /** Канал сводки, когда в стопке только значки служб: самый «громкий» из
     * их каналов (у аватара канал минимальный — им одним стопку не прятать). */
    private static String loudestChannel(NotificationManager sys, List<Child> quiet) {
        String best = null;
        int bestImportance = Integer.MIN_VALUE;
        for (Child c : quiet) {
            String id = NotificationCompat.getChannelId(c.n);
            if (id == null) continue;
            int importance = 0;
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationChannel ch = sys.getNotificationChannel(id);
                if (ch == null) continue;
                importance = ch.getImportance();
            }
            if (importance > bestImportance) {
                bestImportance = importance;
                best = id;
            }
        }
        return best != null ? best : BackgroundInboxService.CHANNEL_LINK;
    }

    private static StatusBarNotification findActive(Context ctx, String tag, int id) {
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return null;
        try {
            for (StatusBarNotification sbn : nm.getActiveNotifications()) {
                if (sbn.getId() == id && tag.equals(sbn.getTag())) return sbn;
            }
        } catch (RuntimeException e) {
            // не критично
        }
        return null;
    }

    /** Реплики из висящей карточки (после перезапуска процесса). */
    private static List<Line> linesOf(Notification n) {
        List<Line> out = new ArrayList<>();
        NotificationCompat.MessagingStyle style =
                NotificationCompat.MessagingStyle.extractMessagingStyleFromNotification(n);
        if (style == null) return out;
        for (NotificationCompat.MessagingStyle.Message m : style.getMessages()) {
            out.add(new Line(m.getText() == null ? "" : m.getText().toString(), m.getTimestamp()));
        }
        return out;
    }

    private static PendingIntent dismissIntent(Context ctx, String tag, int id) {
        // data — по уведомлению: у каждого свой PendingIntent, экстры не перетираются
        Intent i = new Intent(ctx, NotifyStack.class)
                .setAction(ACTION_DISMISSED)
                .setData(Uri.fromParts("vpc-notify", key(tag, id), null))
                .putExtra(EXTRA_TAG, tag)
                .putExtra(EXTRA_ID, id);
        return PendingIntent.getBroadcast(ctx, 0, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }
}
