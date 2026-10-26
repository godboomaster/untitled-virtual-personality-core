package io.vpcore.app;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.util.Log;
import android.util.TypedValue;
import android.view.View;
import android.widget.RemoteViews;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Calendar;
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
 * когда персона пишет, TALK_MS шевелит губами (если есть листы рта) и над
 * головой появляется облачко с репликой.
 *
 * Тап по голове — погладить, как на рабочем столе компьютера: глаза закрыты,
 * голова под руку, над ней сердечко; ядру — сигнал pet (персона узнает, если
 * включены «тычки», не чаще раза в 15 мин). Тап по облачку — чат с персоной;
 * облачко уходит, когда открыли приложение или прошло BUBBLE_MS.
 *
 * Спит — глаза закрыты, голова опущена, «z» у макушки, кадры стоят: когда
 * персона спит в «Комнате» (asleep в /api/portraits), а если ядро этого не
 * знает — ночью по часам телефона. Погладили — просыпается и не спит WAKE_MS
 * после последнего касания, потом засыпает снова. К границе ночи, к концу
 * облачка и пробуждения — будильник RTC без пробуждения телефона
 * (scheduleTick): срабатывает, когда тот и так проснулся, — виджет
 * перерисуется к тому, как на него посмотрят.
 *
 * Живой анимации у виджетов нет: кадры (PortraitWidgetFrames) заранее
 * разложены в ViewFlipper, и лаунчер сам листает их раз в 150 мс — только
 * пока виджет на экране. Порядок кадров — «сценка» на SCENE_FRAMES кадров по
 * кругу, с морганиями и взглядом в сторону в случайных местах; новая — при
 * каждой перерисовке. Спит — свой макет (widget_portrait_sleep): одна
 * картинка, листать нечего. Интервал листания не меняем: ViewFlipper
 * применяет новый только после уже запланированного кадра.
 *
 * Атласы — GET /api/personas/{p}/portrait (те же, что у веба); качаются,
 * только когда портрет правили (updated в лёгком /api/portraits) или
 * сохранённым больше STALE_MS. Лёгкая проверка (спит ли, не правили ли) — при
 * включении экрана и раз в STATE_EVERY_MS, пока он включён (RoomWidget). Адрес
 * и токен — фоновой службы (BackgroundInboxService); без связи виджет
 * показывает сохранённое.
 *
 * Персону выбирают при установке (PersonaWidgetConfigure, общий с
 * «Комнатой»). Сеть и отрисовка — в одном фоновом потоке IO, по очереди.
 */
public class PortraitWidget extends AppWidgetProvider {

    private static final String TAG = "PortraitWidget";

    static final String ACTION_PET = "io.vpcore.app.PORTRAIT_PET";
    static final String ACTION_TICK = "io.vpcore.app.PORTRAIT_TICK";

    private static final String PREFS = "vpc_portrait_widget";
    // Ключи: w.<виджет> → персона; n.<персона> → имя; t.<персона> → когда
    // получен портрет; u.<персона> → его updated_at; e.<персона> → ошибка
    // последнего запроса; s.<персона> → спит ли ("1"/"0", нет — не знаем);
    // b.<персона>, bt.<персона> → облачко: текст и когда пришло;
    // wk.<персона> → до какого времени не спит (погладили);
    // state_at → когда последний раз спрашивали /api/portraits

    private static final long STALE_MS = 24 * 60 * 60_000L;
    private static final long RESUME_STALE_MS = 30 * 60_000L;
    private static final long STATE_EVERY_MS = 15 * 60_000L;
    private static final long TALK_MS = 6_000L;
    private static final long PET_MS = 1_400L;
    private static final long PET_POKE_MS = 60_000L;
    private static final long BUBBLE_MS = 60 * 60_000L;
    // Погладили спящую — не спит столько после последнего касания
    private static final long WAKE_MS = 5 * 60_000L;
    private static final int BUBBLE_MAX_CHARS = 300;
    // Ночь по часам телефона — когда ядро не знает, спит ли персона
    private static final int NIGHT_FROM_H = 0;
    private static final int NIGHT_TO_H = 7;
    // Кадр сценки — android:flipInterval в widget_portrait.xml (150 мс)
    private static final int SCENE_FRAMES = 44;
    // Сердечко: кадров (по 150 мс) и на сколько dp поднимается
    private static final int HEART_FRAMES = 9;
    private static final int HEART_RISE_DP = 44;
    // Пустые кадры после: голова поднимется (PET_MS) раньше, чем сердечко начнётся заново
    private static final int HEART_PAUSE_FRAMES = 4;
    private static final String[] Z_FRAMES = {"z", "z z", ""};
    // Размер кадра: клетка атласа (256 px). Не прошло пакетом Binder — меньше
    private static final int FRAME_PX = 256;
    private static final int FRAME_PX_FALLBACK = 160;

    private static final ScheduledExecutorService IO = Executors.newSingleThreadScheduledExecutor();
    // Только в потоке IO: персона → до какого времени говорит / её гладят;
    // когда ядру последний раз сказали, что гладили
    private static final Map<String, Long> talkingUntil = new HashMap<>();
    private static final Map<String, Long> pettedUntil = new HashMap<>();
    private static final Map<String, Long> petPokedAt = new HashMap<>();
    // Что виджет показывает сейчас: то же самое заново не отправлять лаунчеру
    private static final Map<Integer, String> shownState = new HashMap<>();

    // ── События системы ───────────────────────────────────────────────

    @Override
    public void onUpdate(Context ctx, AppWidgetManager mgr, int[] ids) {
        // Сначала сохранённое (виджет не пустой), потом свежее с ядра
        Context app = ctx.getApplicationContext();
        PendingResult r = goAsync();
        IO.execute(() -> {
            try {
                shownState.clear();
                renderAllNow(app);
                refreshNow(app, STALE_MS);
            } finally {
                r.finish();
            }
        });
    }

    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (ACTION_TICK.equals(intent.getAction())) {
            // Ночь началась или кончилась, облачко устарело
            Context app = ctx.getApplicationContext();
            PendingResult r = goAsync();
            IO.execute(() -> {
                try {
                    renderAllNow(app);
                } finally {
                    r.finish();
                }
            });
            return;
        }
        if (!ACTION_PET.equals(intent.getAction())) {
            super.onReceive(ctx, intent);
            return;
        }
        int id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID);
        String persona = prefs(ctx).getString("w." + id, null);
        if (persona == null) return;
        // Процесс жив, пока голова не поднимется: перерисовка «после» — тоже здесь
        Context app = ctx.getApplicationContext();
        PendingResult r = goAsync();
        IO.execute(() -> pet(app, persona));
        IO.schedule(() -> {
            try {
                Long until = pettedUntil.get(persona);
                if (until != null && until <= System.currentTimeMillis()) {
                    pettedUntil.remove(persona);
                    renderAllNow(app);
                }
            } finally {
                r.finish();
            }
        }, PET_MS + 50, TimeUnit.MILLISECONDS);
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

    /**
     * Включили экран (и раз в STATE_EVERY_MS, пока включён — RoomWidget):
     * спит ли персона и не правили ли портрет — лёгким запросом, не чаще
     * STATE_EVERY_MS; ночь, облачко и прочее по часам — без сети.
     */
    static void checkState(Context ctx) {
        Context app = ctx.getApplicationContext();
        IO.execute(() -> {
            if (configured(app).isEmpty()) return;
            long last = prefs(app).getLong("state_at", 0);
            if (RoomWidget.serviceEnabled(app) && System.currentTimeMillis() - last >= STATE_EVERY_MS) {
                fetchState(app);
            }
            renderAllNow(app);
        });
    }

    /** Персона написала — её портрет TALK_MS говорит, над головой облачко (BackgroundInboxService). */
    static void personaSpoke(Context ctx, String persona, String text) {
        if (persona == null || persona.isEmpty()) return;
        Context app = ctx.getApplicationContext();
        IO.execute(() -> {
            if (!configured(app).containsValue(persona)) return;
            String said = text == null ? "" : text.trim();
            if (!said.isEmpty()) {
                if (said.length() > BUBBLE_MAX_CHARS) said = said.substring(0, BUBBLE_MAX_CHARS);
                prefs(app).edit()
                        .putString("b." + persona, said)
                        .putLong("bt." + persona, System.currentTimeMillis())
                        .apply();
            }
            PortraitWidgetFrames.Frames f = PortraitWidgetFrames.load(app, persona, FRAME_PX);
            if (f != null && f.canTalk()) {
                // Уже говорит (пачка сообщений) — только продлить
                talkingUntil.put(persona, System.currentTimeMillis() + TALK_MS);
                IO.schedule(() -> stopTalking(app, persona), TALK_MS + 50, TimeUnit.MILLISECONDS);
            }
            renderAllNow(app);
        });
    }

    /** Приложение на экране: реплики теперь видно в чате — облачка убрать. */
    static void clearBubbles(Context ctx) {
        Context app = ctx.getApplicationContext();
        IO.execute(() -> {
            SharedPreferences p = prefs(app);
            SharedPreferences.Editor e = p.edit();
            boolean any = false;
            for (String key : p.getAll().keySet()) {
                if (key.startsWith("b.") || key.startsWith("bt.")) {
                    e.remove(key);
                    any = true;
                }
            }
            if (!any) return;
            e.apply();
            renderAllNow(app);
        });
    }

    private static void stopTalking(Context ctx, String persona) {
        Long until = talkingUntil.get(persona);
        if (until == null || until > System.currentTimeMillis()) return;
        talkingUntil.remove(persona);
        renderAllNow(ctx);
    }

    // Только в потоке IO
    private static void pet(Context ctx, String persona) {
        long now = System.currentTimeMillis();
        pettedUntil.put(persona, now + PET_MS);
        // Просыпается (спала — откроет глаза, когда поднимет голову) и не спит
        // WAKE_MS после последнего касания; потом — снова по ночи и «Комнате»
        prefs(ctx).edit().putLong("wk." + persona, now + WAKE_MS).apply();
        IO.schedule(() -> renderAllNow(ctx), WAKE_MS + 1_000, TimeUnit.MILLISECONDS);
        // Повторный тап — сердечко заново
        for (Map.Entry<Integer, String> w : configured(ctx).entrySet()) {
            if (w.getValue().equals(persona)) shownState.remove(w.getKey());
        }
        renderAllNow(ctx);
        Long poked = petPokedAt.get(persona);
        if ((poked != null && now - poked < PET_POKE_MS) || !RoomWidget.serviceEnabled(ctx)) return;
        petPokedAt.put(persona, now);
        try {
            JSONObject body = new JSONObject().put("chat_id", "auto").put("kind", "pet");
            RoomWidget.request(ctx, "POST", "/api/personas/" + RoomWidget.enc(persona) + "/room/poke",
                    body.toString());
        } catch (Exception e) {
            Log.i(TAG, "погладили " + persona + ", ядру не сказали: " + e.getMessage());
        }
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
                .remove("state_at")
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
        boolean enabled = RoomWidget.serviceEnabled(ctx);
        if (enabled) {
            // Спит ли и не правили ли портрет — заодно, запрос лёгкий
            fetchState(ctx);
            Set<String> personas = new LinkedHashSet<>(widgets.values());
            long now = System.currentTimeMillis();
            SharedPreferences p = prefs(ctx);
            for (String persona : personas) {
                if (now - p.getLong("t." + persona, 0) < maxAgeMs && p.getString("e." + persona, null) == null) continue;
                fetch(ctx, persona);
            }
        }
        renderAllNow(ctx);
    }

    /** GET /api/portraits: спит ли каждая персона; портрет правили — скачать заново. */
    private static void fetchState(Context ctx) {
        SharedPreferences p = prefs(ctx);
        p.edit().putLong("state_at", System.currentTimeMillis()).apply();
        JSONObject list;
        try {
            list = new JSONObject(RoomWidget.request(ctx, "GET", "/api/portraits", null));
        } catch (Exception e) {
            // Ядро старее или не отвечает — что спит, не знаем; остаётся ночь по часам
            Log.i(TAG, "состояние портретов: " + e.getMessage());
            return;
        }
        JSONObject asleep = list.optJSONObject("asleep");
        JSONObject updated = list.optJSONObject("updated");
        SharedPreferences.Editor e = p.edit();
        List<String> stale = new ArrayList<>();
        for (String persona : new LinkedHashSet<>(configured(ctx).values())) {
            if (asleep != null && asleep.has(persona) && !asleep.isNull(persona)) {
                e.putString("s." + persona, asleep.optBoolean(persona) ? "1" : "0");
            } else {
                e.remove("s." + persona);
            }
            double u = updated == null ? 0 : updated.optDouble(persona, 0);
            String known = p.getString("u." + persona, "");
            if (u > 0 && !String.valueOf(u).equals(known)) stale.add(persona);
        }
        e.apply();
        for (String persona : stale) fetch(ctx, persona);
    }

    private static void fetch(Context ctx, String persona) {
        SharedPreferences p = prefs(ctx);
        try {
            JSONObject portrait = new JSONObject(RoomWidget.request(ctx, "GET",
                    "/api/personas/" + RoomWidget.enc(persona) + "/portrait", null));
            PortraitWidgetFrames.save(ctx, persona, portrait);
            double u = portrait.optDouble("updated_at", 0);
            SharedPreferences.Editor e = p.edit()
                    .putLong("t." + persona, System.currentTimeMillis())
                    .remove("e." + persona);
            if (u > 0) e.putString("u." + persona, String.valueOf(u));
            else e.remove("u." + persona);
            e.apply();
        } catch (RoomWidget.HttpError e) {
            p.edit().putString("e." + persona, e.code == 404 ? "gone" : "http").apply();
        } catch (Exception e) {
            Log.i(TAG, "портрет " + persona + ": " + e.getMessage());
            p.edit().putString("e." + persona, "offline").apply();
        }
    }

    /** Спит ли персона: так говорит ядро, а если оно не знает — ночь по часам телефона. */
    private static boolean asleep(SharedPreferences p, String persona) {
        String s = p.getString("s." + persona, null);
        if (s != null) return "1".equals(s);
        int h = Calendar.getInstance().get(Calendar.HOUR_OF_DAY);
        return h >= NIGHT_FROM_H && h < NIGHT_TO_H;
    }

    /** Спит сейчас: персона спит (или ночь), а недавно её не гладили. */
    private static boolean sleepsNow(SharedPreferences p, String persona) {
        return asleep(p, persona) && p.getLong("wk." + persona, 0) <= System.currentTimeMillis();
    }

    /** Свежая реплика для облачка (нет или устарела — null). */
    private static String bubble(SharedPreferences p, String persona) {
        String text = p.getString("b." + persona, null);
        if (text == null || System.currentTimeMillis() - p.getLong("bt." + persona, 0) > BUBBLE_MS) return null;
        return text;
    }

    // ── Отрисовка ─────────────────────────────────────────────────────

    private static void renderAllNow(Context ctx) {
        AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
        if (mgr == null) return;
        scheduleTick(ctx);
        for (int id : widgetIds(ctx)) {
            // Показывает то же (сон, облачко, подсказка) — лаунчеру ничего не слать
            String state = stateOf(ctx, id);
            if (state.equals(shownState.get(id))) continue;
            try {
                mgr.updateAppWidget(id, build(ctx, id, FRAME_PX));
                shownState.put(id, state);
            } catch (RuntimeException e) {
                // Кадры не влезли в пакет Binder — те же, но меньше
                Log.w(TAG, "виджет " + id + ": кадры меньше", e);
                try {
                    mgr.updateAppWidget(id, build(ctx, id, FRAME_PX_FALLBACK));
                    shownState.put(id, state);
                } catch (RuntimeException e2) {
                    Log.w(TAG, "виджет " + id + " не обновлён", e2);
                }
            }
        }
    }

    /**
     * Будильник к ближайшей смене по часам: граница ночи, конец облачка или
     * того, что её погладили (засыпает снова).
     * RTC без пробуждения и неточный: спящий телефон не будит, приходит, когда
     * тот и так проснулся. Один на все виджеты, новый заменяет прежний.
     */
    private static void scheduleTick(Context ctx) {
        AlarmManager am = (AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
        if (am == null) return;
        PendingIntent pi = PendingIntent.getBroadcast(ctx, 0x6FFF,
                new Intent(ctx, PortraitWidget.class).setAction(ACTION_TICK),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Map<Integer, String> widgets = configured(ctx);
        if (widgets.isEmpty()) {
            am.cancel(pi);
            return;
        }
        long now = System.currentTimeMillis();
        Calendar edge = Calendar.getInstance();
        int h = edge.get(Calendar.HOUR_OF_DAY);
        edge.set(Calendar.MINUTE, 0);
        edge.set(Calendar.SECOND, 5);
        edge.set(Calendar.MILLISECOND, 0);
        if (h >= NIGHT_FROM_H && h < NIGHT_TO_H) {
            edge.set(Calendar.HOUR_OF_DAY, NIGHT_TO_H);
        } else {
            edge.set(Calendar.HOUR_OF_DAY, NIGHT_FROM_H);
            if (edge.getTimeInMillis() <= now) edge.add(Calendar.DAY_OF_MONTH, 1);
        }
        long next = edge.getTimeInMillis();
        SharedPreferences p = prefs(ctx);
        for (String persona : new LinkedHashSet<>(widgets.values())) {
            long gone = p.getLong("bt." + persona, 0) + BUBBLE_MS + 1_000;
            if (p.contains("b." + persona) && gone > now) next = Math.min(next, gone);
            long woke = p.getLong("wk." + persona, 0) + 1_000;
            if (woke > now) next = Math.min(next, woke);
        }
        am.set(AlarmManager.RTC, next, pi);
    }

    /** Что должен показывать виджет — строкой: сравнить с показанным. */
    private static String stateOf(Context ctx, int widgetId) {
        SharedPreferences p = prefs(ctx);
        String persona = p.getString("w." + widgetId, null);
        if (persona == null) return "pick";
        long now = System.currentTimeMillis();
        Long talk = talkingUntil.get(persona);
        Long pat = pettedUntil.get(persona);
        String b = bubble(p, persona);
        return persona + "|" + p.getLong("t." + persona, 0) + "|" + p.getString("e." + persona, "")
                + "|" + RoomWidget.serviceEnabled(ctx)
                + "|" + (talk != null && talk > now) + "|" + (pat != null && pat > now)
                + "|" + sleepsNow(p, persona) + "|" + (b == null ? "" : b.hashCode() + ":" + p.getLong("bt." + persona, 0));
    }

    private static RemoteViews build(Context ctx, int widgetId, int framePx) {
        RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_portrait);
        SharedPreferences p = prefs(ctx);
        String persona = p.getString("w." + widgetId, null);
        v.setViewVisibility(R.id.portrait_bubble_box, View.GONE);
        if (persona == null) {
            // Не настроен (настройку закрыли) — тап открывает выбор персоны
            hint(ctx, v, ctx.getString(R.string.portrait_w_pick));
            Intent cfg = new Intent(ctx, PersonaWidgetConfigure.class)
                    .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId)
                    .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            v.setOnClickPendingIntent(R.id.portrait_root, PendingIntent.getActivity(ctx, 0x5000 + widgetId, cfg,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
            return v;
        }
        String name = p.getString("n." + persona, persona);

        PortraitWidgetFrames.Frames frames = PortraitWidgetFrames.load(ctx, persona, framePx);
        if (frames != null) {
            long now = System.currentTimeMillis();
            Long until = talkingUntil.get(persona);
            Long pat = pettedUntil.get(persona);
            boolean talking = until != null && until > now && frames.canTalk();
            boolean petted = pat != null && pat > now;
            String said = bubble(p, persona);
            // Пишет или недавно гладили — не спит
            boolean sleeping = !talking && said == null && sleepsNow(p, persona);
            if (sleeping && !petted) return sleepView(ctx, widgetId, name, frames.down);
            List<Bitmap> scene;
            if (petted || sleeping) {
                scene = new ArrayList<>();
                scene.add(frames.down);
            } else {
                scene = talking ? frames.talk(SCENE_FRAMES, new Random()) : frames.idle(SCENE_FRAMES, new Random());
            }
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
            effects(ctx, v, petted, sleeping);
            if (said != null) {
                v.setViewVisibility(R.id.portrait_bubble_box, View.VISIBLE);
                v.setTextViewText(R.id.portrait_bubble, said);
                v.setContentDescription(R.id.portrait_bubble, name + ": " + said);
                // Тап по облачку — чат с персоной
                Intent chat = new Intent(ctx, MainActivity.class)
                        .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                        .putExtra(BackgroundInboxService.EXTRA_PERSONA, persona);
                v.setOnClickPendingIntent(R.id.portrait_bubble_box, PendingIntent.getActivity(ctx, 0x4000 + widgetId,
                        chat, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
            }
            petOnTap(ctx, v, widgetId, name);
            return v;
        }

        String err = p.getString("e." + persona, null);
        boolean fetchedOnce = p.getLong("t." + persona, 0) > 0;
        if ("gone".equals(err)) hint(ctx, v, ctx.getString(R.string.portrait_w_gone));
        else if (!RoomWidget.serviceEnabled(ctx)) hint(ctx, v, ctx.getString(R.string.portrait_w_off_hint));
        else if (!fetchedOnce && err != null) hint(ctx, v, ctx.getString(R.string.portrait_w_offline));
        else if (!fetchedOnce) hint(ctx, v, ctx.getString(R.string.portrait_w_loading));
        else hint(ctx, v, ctx.getString(R.string.portrait_w_none, name));
        // Портрета нет — гладить некого: тап открывает приложение (чат персоны)
        v.setContentDescription(R.id.portrait_root, name);
        Intent open = new Intent(ctx, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra(BackgroundInboxService.EXTRA_PERSONA, persona);
        v.setOnClickPendingIntent(R.id.portrait_root, PendingIntent.getActivity(ctx, 0x4000 + widgetId, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
        return v;
    }

    /** Спит: одна неподвижная картинка и «z»; тап — погладить (сердечко — уже в обычном макете). */
    private static RemoteViews sleepView(Context ctx, int widgetId, String name, Bitmap still) {
        RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_portrait_sleep);
        v.setImageViewBitmap(R.id.portrait_still, still);
        zFrames(ctx, v);
        petOnTap(ctx, v, widgetId, name);
        return v;
    }

    /** Тап по голове — погладить. */
    private static void petOnTap(Context ctx, RemoteViews v, int widgetId, String name) {
        v.setContentDescription(R.id.portrait_root, ctx.getString(R.string.portrait_w_pet, name));
        Intent petIntent = new Intent(ctx, PortraitWidget.class)
                .setAction(ACTION_PET)
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId);
        v.setOnClickPendingIntent(R.id.portrait_root, PendingIntent.getBroadcast(ctx, 0x6000 + widgetId,
                petIntent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
    }

    private static void zFrames(Context ctx, RemoteViews v) {
        v.removeAllViews(R.id.portrait_z);
        for (String text : Z_FRAMES) {
            RemoteViews f = new RemoteViews(ctx.getPackageName(), R.layout.widget_portrait_z);
            f.setTextViewText(R.id.portrait_z_text, text);
            v.addView(R.id.portrait_z, f);
        }
    }

    /** Сердечко над головой (гладят) и «z» у макушки (гладят спящую); нет — пустые и скрыты. */
    private static void effects(Context ctx, RemoteViews v, boolean heart, boolean z) {
        v.removeAllViews(R.id.portrait_fx);
        v.removeAllViews(R.id.portrait_z);
        v.setViewVisibility(R.id.portrait_fx, heart ? View.VISIBLE : View.GONE);
        v.setViewVisibility(R.id.portrait_z, z ? View.VISIBLE : View.GONE);
        if (heart) {
            float dp = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, 1,
                    ctx.getResources().getDisplayMetrics());
            for (int i = 0; i < HEART_FRAMES; i++) {
                float k = (float) i / (HEART_FRAMES - 1);
                RemoteViews f = new RemoteViews(ctx.getPackageName(), R.layout.widget_portrait_heart);
                f.setViewPadding(R.id.portrait_heart_box, 0, 0, 0, Math.round(k * HEART_RISE_DP * dp));
                // Появляется за пару кадров, потом тает
                int alpha = k < 0.2f ? Math.round(255 * (0.4f + 3f * k)) : Math.round(255 * (1f - (k - 0.2f) / 0.8f));
                f.setInt(R.id.portrait_heart, "setImageAlpha", Math.max(0, Math.min(255, alpha)));
                v.addView(R.id.portrait_fx, f);
            }
            for (int i = 0; i < HEART_PAUSE_FRAMES; i++) {
                RemoteViews f = new RemoteViews(ctx.getPackageName(), R.layout.widget_portrait_heart);
                f.setInt(R.id.portrait_heart, "setImageAlpha", 0);
                v.addView(R.id.portrait_fx, f);
            }
        }
        if (z) zFrames(ctx, v);
    }

    private static void hint(Context ctx, RemoteViews v, String text) {
        effects(ctx, v, false, false);
        v.removeAllViews(R.id.portrait_flipper);
        v.setViewVisibility(R.id.portrait_flipper, View.GONE);
        v.setViewVisibility(R.id.portrait_hint, View.VISIBLE);
        v.setTextViewText(R.id.portrait_hint, text);
    }
}
