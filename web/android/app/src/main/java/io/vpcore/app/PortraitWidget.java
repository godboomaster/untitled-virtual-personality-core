package io.vpcore.app;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.util.Log;
import android.view.View;
import android.widget.RemoteViews;

import org.json.JSONObject;

import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * Виджет «Живой портрет» на рабочем столе: голова персоны из её живого
 * портрета (арт-мастерская → «Живой портрет», app/api/portrait_api.py) прямо
 * на обоях, без рамки, — смотрит на вас, моргает, изредка отводит взгляд, а
 * когда персона пишет, TALK_MS шевелит губами (если есть листы рта).
 *
 * Живой анимации у виджетов нет: кадры (PortraitWidgetFrames) заранее
 * разложены в ViewFlipper, и лаунчер сам листает их раз в FRAME_MS — только
 * пока виджет на экране. Порядок кадров — «сценка» на SCENE_FRAMES кадров по
 * кругу, с морганиями и взглядом в сторону в случайных местах; новая — при
 * каждой перерисовке.
 *
 * Атласы — GET /api/personas/{p}/portrait (те же, что у веба); портрет меняют
 * редко, поэтому качаются, когда сохранённым больше STALE_MS (при установке,
 * включении службы, перерисовке системой) или RESUME_STALE_MS (при открытии
 * приложения — там его и меняют). Адрес и токен — фоновой службы
 * (BackgroundInboxService); без связи виджет показывает сохранённое.
 *
 * Тап — голосовой режим чата с персоной. Персону выбирают при установке
 * (PersonaWidgetConfigure, общий с «Комнатой»). Сеть и отрисовка — в одном
 * фоновом потоке IO, по очереди.
 */
public class PortraitWidget extends AppWidgetProvider {

    private static final String TAG = "PortraitWidget";

    static final String SECTION_VOICE = "voice";

    private static final String PREFS = "vpc_portrait_widget";
    // Ключи: w.<виджет> → персона; n.<персона> → имя; t.<персона> → когда
    // получен портрет; e.<персона> → ошибка последнего запроса

    private static final long STALE_MS = 6 * 60 * 60_000L;
    private static final long RESUME_STALE_MS = 30 * 60_000L;
    private static final long TALK_MS = 6_000L;
    // Кадр сценки — android:flipInterval в widget_portrait.xml (150 мс)
    private static final int SCENE_FRAMES = 44;
    // Размер кадра: клетка атласа (256 px). Не прошло пакетом Binder — меньше
    private static final int FRAME_PX = 256;
    private static final int FRAME_PX_FALLBACK = 160;

    private static final ScheduledExecutorService IO = Executors.newSingleThreadScheduledExecutor();
    // Персона → до какого времени говорит (только в потоке IO)
    private static final Map<String, Long> talkingUntil = new HashMap<>();

    // ── События системы ───────────────────────────────────────────────

    @Override
    public void onUpdate(Context ctx, AppWidgetManager mgr, int[] ids) {
        // Сначала сохранённое (виджет не пустой), потом свежее с ядра
        Context app = ctx.getApplicationContext();
        PendingResult r = goAsync();
        IO.execute(() -> {
            try {
                renderAllNow(app);
                refreshNow(app, STALE_MS);
            } finally {
                r.finish();
            }
        });
    }

    @Override
    public void onDeleted(Context ctx, int[] ids) {
        SharedPreferences.Editor e = prefs(ctx).edit();
        for (int id : ids) e.remove("w." + id);
        e.apply();
    }

    // ── Когда обновлять ───────────────────────────────────────────────

    /** Открыли приложение: портрет могли поменять — освежить, если сохранённый не свежий. */
    static void refreshIfStale(Context ctx) {
        Context app = ctx.getApplicationContext();
        IO.execute(() -> refreshNow(app, RESUME_STALE_MS));
    }

    /** Освежить все сейчас (включили службу, выбрали персону). */
    static void refreshAll(Context ctx) {
        Context app = ctx.getApplicationContext();
        IO.execute(() -> refreshNow(app, 0));
    }

    /** Перерисовать из сохранённого (без сети). */
    static void renderAll(Context ctx) {
        Context app = ctx.getApplicationContext();
        IO.execute(() -> renderAllNow(app));
    }

    /** Персона написала — её портрет TALK_MS говорит (BackgroundInboxService). */
    static void personaSpoke(Context ctx, String persona) {
        if (persona == null || persona.isEmpty()) return;
        Context app = ctx.getApplicationContext();
        IO.execute(() -> {
            if (!configured(app).containsValue(persona)) return;
            PortraitWidgetFrames.Frames f = PortraitWidgetFrames.load(app, persona, FRAME_PX);
            if (f == null || !f.canTalk()) return;
            // Уже говорит (пачка сообщений) — только продлить
            boolean already = talkingUntil.containsKey(persona);
            talkingUntil.put(persona, System.currentTimeMillis() + TALK_MS);
            if (!already) renderAllNow(app);
            IO.schedule(() -> stopTalking(app, persona), TALK_MS + 50, TimeUnit.MILLISECONDS);
        });
    }

    private static void stopTalking(Context ctx, String persona) {
        Long until = talkingUntil.get(persona);
        if (until == null || until > System.currentTimeMillis()) return;
        talkingUntil.remove(persona);
        renderAllNow(ctx);
    }

    // ── Данные ────────────────────────────────────────────────────────

    static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** Виджет выбрал персону (PersonaWidgetConfigure). */
    static void configure(Context ctx, int widgetId, String persona, String name) {
        prefs(ctx).edit()
                .putString("w." + widgetId, persona)
                .putString("n." + persona, name)
                .remove("t." + persona)
                .apply();
        refreshAll(ctx);
    }

    private static int[] widgetIds(Context ctx) {
        AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
        if (mgr == null) return new int[0];
        int[] ids = mgr.getAppWidgetIds(new ComponentName(ctx, PortraitWidget.class));
        return ids == null ? new int[0] : ids;
    }

    /** Виджеты и их персоны (без ещё не настроенных). */
    private static Map<Integer, String> configured(Context ctx) {
        Map<Integer, String> out = new LinkedHashMap<>();
        SharedPreferences p = prefs(ctx);
        for (int id : widgetIds(ctx)) {
            String persona = p.getString("w." + id, null);
            if (persona != null) out.put(id, persona);
        }
        return out;
    }

    private static void refreshNow(Context ctx, long maxAgeMs) {
        Map<Integer, String> widgets = configured(ctx);
        if (widgets.isEmpty()) return;
        boolean fetched = false;
        boolean enabled = RoomWidget.serviceEnabled(ctx);
        if (enabled) {
            Set<String> personas = new LinkedHashSet<>(widgets.values());
            long now = System.currentTimeMillis();
            SharedPreferences p = prefs(ctx);
            for (String persona : personas) {
                if (now - p.getLong("t." + persona, 0) < maxAgeMs && p.getString("e." + persona, null) == null) continue;
                fetch(ctx, persona);
                fetched = true;
            }
        }
        // Служба выключена — запросов нет, но подсказка должна смениться
        if (fetched || !enabled) renderAllNow(ctx);
    }

    private static void fetch(Context ctx, String persona) {
        SharedPreferences p = prefs(ctx);
        try {
            JSONObject portrait = new JSONObject(RoomWidget.request(ctx, "GET",
                    "/api/personas/" + RoomWidget.enc(persona) + "/portrait", null));
            PortraitWidgetFrames.save(ctx, persona, portrait);
            p.edit().putLong("t." + persona, System.currentTimeMillis()).remove("e." + persona).apply();
        } catch (RoomWidget.HttpError e) {
            p.edit().putString("e." + persona, e.code == 404 ? "gone" : "http").apply();
        } catch (Exception e) {
            Log.i(TAG, "портрет " + persona + ": " + e.getMessage());
            p.edit().putString("e." + persona, "offline").apply();
        }
    }

    // ── Отрисовка ─────────────────────────────────────────────────────

    private static void renderAllNow(Context ctx) {
        AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
        if (mgr == null) return;
        for (int id : widgetIds(ctx)) {
            try {
                mgr.updateAppWidget(id, build(ctx, id, FRAME_PX));
            } catch (RuntimeException e) {
                // Кадры не влезли в пакет Binder — те же, но меньше
                Log.w(TAG, "виджет " + id + ": кадры меньше", e);
                try {
                    mgr.updateAppWidget(id, build(ctx, id, FRAME_PX_FALLBACK));
                } catch (RuntimeException e2) {
                    Log.w(TAG, "виджет " + id + " не обновлён", e2);
                }
            }
        }
    }

    private static RemoteViews build(Context ctx, int widgetId, int framePx) {
        RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_portrait);
        SharedPreferences p = prefs(ctx);
        String persona = p.getString("w." + widgetId, null);
        if (persona == null) {
            // Не настроен (настройку закрыли) — тап открывает выбор персоны
            hint(v, ctx.getString(R.string.portrait_w_pick));
            Intent cfg = new Intent(ctx, PersonaWidgetConfigure.class)
                    .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId)
                    .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            v.setOnClickPendingIntent(R.id.portrait_root, PendingIntent.getActivity(ctx, 0x5000 + widgetId, cfg,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
            return v;
        }
        String name = p.getString("n." + persona, persona);
        v.setContentDescription(R.id.portrait_root, name);

        PortraitWidgetFrames.Frames frames = PortraitWidgetFrames.load(ctx, persona, framePx);
        if (frames != null) {
            Long until = talkingUntil.get(persona);
            boolean talking = until != null && until > System.currentTimeMillis() && frames.canTalk();
            List<Bitmap> scene = talking ? frames.talk(SCENE_FRAMES, new Random())
                    : frames.idle(SCENE_FRAMES, new Random());
            v.setViewVisibility(R.id.portrait_flipper, View.VISIBLE);
            v.setViewVisibility(R.id.portrait_hint, View.GONE);
            // Лаунчер может наложить обновление на уже показанный виджет
            // (тот же макет) — старые кадры убрать, а не дописать к ним
            v.removeAllViews(R.id.portrait_flipper);
            for (Bitmap b : scene) {
                RemoteViews frame = new RemoteViews(ctx.getPackageName(), R.layout.widget_portrait_frame);
                frame.setImageViewBitmap(R.id.portrait_frame, b);
                v.addView(R.id.portrait_flipper, frame);
            }
        } else {
            String err = p.getString("e." + persona, null);
            boolean fetchedOnce = p.getLong("t." + persona, 0) > 0;
            if ("gone".equals(err)) hint(v, ctx.getString(R.string.portrait_w_gone));
            else if (!RoomWidget.serviceEnabled(ctx)) hint(v, ctx.getString(R.string.portrait_w_off_hint));
            else if (!fetchedOnce && err != null) hint(v, ctx.getString(R.string.portrait_w_offline));
            else if (!fetchedOnce) hint(v, ctx.getString(R.string.portrait_w_loading));
            else hint(v, ctx.getString(R.string.portrait_w_none, name));
        }

        Intent open = new Intent(ctx, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra(BackgroundInboxService.EXTRA_PERSONA, persona)
                .putExtra(BackgroundInboxService.EXTRA_SECTION, SECTION_VOICE);
        v.setOnClickPendingIntent(R.id.portrait_root, PendingIntent.getActivity(ctx, 0x4000 + widgetId, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
        return v;
    }

    private static void hint(RemoteViews v, String text) {
        v.removeAllViews(R.id.portrait_flipper);
        v.setViewVisibility(R.id.portrait_flipper, View.GONE);
        v.setViewVisibility(R.id.portrait_hint, View.VISIBLE);
        v.setTextViewText(R.id.portrait_hint, text);
    }
}
