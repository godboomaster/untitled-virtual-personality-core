package io.vpcore.app;

import android.app.AlarmManager;
import android.app.KeyguardManager;
import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Rect;
import android.widget.Toast;
import android.graphics.Bitmap;
import android.graphics.Paint;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.text.StaticLayout;
import android.text.TextPaint;
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
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.Predicate;
import java.util.regex.Pattern;

/**
 * Виджет «Живой портрет» на рабочем столе: голова персоны из её живого
 * портрета (арт-мастерская → «Живой портрет», app/api/portrait_api.py) прямо
 * на обоях, без рамки, — смотрит на вас, моргает, изредка отводит взгляд, а
 * когда персона пишет, шевелит губами (если есть листы рта) и «говорит»
 * реплику в облачке над головой: слово за словом, длинную — частями под
 * облачко (как портрет на рабочем столе компьютера). Облачко лежит поверх
 * сцены, голова под ним не сжимается; в высоком виджете голова — внизу, место
 * над ней — облачку. Никто не смотрит (экран погас или заблокирован) —
 * реплика ждёт и начинается, когда телефон разблокируют.
 *
 * Пауза до ответа (лента реплик ядра — PortraitSaidFeed): пользователь
 * написал персоне — она коротко кивает («слышу»), его сообщение встаёт
 * облачком слева вверху, и персона поворачивается к нему и читает (по длине
 * текста, READ_*), потом задумывается: взгляд вверх и в сторону, пока не
 * придёт ответ (тогда облачко пользователя уходит и персона говорит, как
 * выше) или ход не кончится без него, но не дольше THINK_MAX_MS. Сообщение
 * без текста (картинка) — только кивок и «думает». Спала — просыпается, как
 * от поглаживания (WAKE_MS).
 *
 * Тап по голове — погладить, как на рабочем столе компьютера: глаза закрыты,
 * голова под руку, над ней сердечко; ядру — сигнал pet (персона узнает, если
 * включены «тычки», не чаще раза в 15 мин). Тап по облачку — чат с персоной;
 * облачко уходит, когда открыли приложение или прошло BUBBLE_MS. Карандаш в
 * углу — окно «Написать» (PortraitInputActivity): поле над клавиатурой,
 * ответ — здесь же облачком.
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
 * показывает сохранённое. Своего портрета у персоны нет — голова её фигуры
 * из «Комнаты»: листы печатает веб (PortraitWidgetFrames).
 *
 * Кнопка в левом нижнем углу — «Поверх приложений»: персона выходит из
 * виджета на экран (AvatarOverlayService) — голова появляется там, где была
 * на виджете, а виджет, пока она там, прозрачный — на месте головы только
 * стеклянная карточка «Вернуть» (awayView); тап по ней (или «На виджет» в
 * меню головы) — голова едет обратно на виджет. Где голова на экране — по
 * месту нажатой кнопки или карточки (sourceBounds) и размеру виджета.
 *
 * Персону выбирают при установке (PersonaWidgetConfigure, общий с
 * «Комнатой»). Сеть и отрисовка — в одном фоновом потоке IO, по очереди.
 */
public class PortraitWidget extends AppWidgetProvider {

    private static final String TAG = "PortraitWidget";

    static final String ACTION_PET = "io.vpcore.app.PORTRAIT_PET";
    static final String ACTION_TICK = "io.vpcore.app.PORTRAIT_TICK";
    // Выйти на экран поверх приложений и вернуться обратно
    static final String ACTION_OUT = "io.vpcore.app.PORTRAIT_OUT";
    static final String ACTION_BACK = "io.vpcore.app.PORTRAIT_BACK";

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
    private static final int BUBBLE_MAX_CHARS = 1200;
    // Реплика по словам: слово — по его длине, часть висит, пока её можно
    // прочесть (как PART_MS портрета на компьютере); губы — всё это время.
    // Это скорость по умолчанию (TEXT_WPS слов/с, TEXT_PART_SEC с на часть);
    // настройка «Общие» в вебе (app/core/avatar_text) ускоряет или
    // замедляет в тех же пропорциях: tw.<>/tp — в prefs, приходит с лентой
    // реплик и /api/portraits
    private static final long WORD_BASE_MS = 70;
    private static final long WORD_PER_CHAR_MS = 22;
    private static final long WORD_MAX_MS = 400;
    private static final long PART_PER_CHAR_MS = 65;
    private static final long PART_MIN_MS = 2_500;
    private static final long PART_MAX_MS = 12_000;
    private static final float TEXT_WPS = 5f;
    private static final float TEXT_PART_SEC = 6.5f;
    private static final long TALK_MIN_MS = 1_500;
    private static final long TALK_MAX_MS = 45_000;
    private static final int MAX_PARTS = 40;
    // Включили или разблокировали экран, а он ещё «заблокирован» — проверить снова через
    private static final long[] RESUME_RETRY_MS = {300, 500, 700, 1_000, 1_500};
    // Облачко: шрифт — как у portrait_bubble; поля текста — рамка облачка
    // (portrait_bubble_box) и отступы текста; размер виджета неизвестен — столько
    private static final float BUBBLE_TEXT_SP = 12f;
    private static final int BUBBLE_TEXT_PAD_DP = 2 + 2 + 8 + 8;
    private static final int DEFAULT_SIDE_DP = 160;
    // Конец предложения: знак и пробел после него (или перевод строки)
    private static final Pattern SENTENCE_END = Pattern.compile("(?<=[.!?…])\\s+|\\n+");
    private static final String ELLIPSIS = "…";
    // Ночь по часам телефона — когда ядро не знает, спит ли персона
    private static final int NIGHT_FROM_H = 0;
    private static final int NIGHT_TO_H = 7;
    // Кадр сценки — android:flipInterval в widget_portrait.xml
    private static final long FRAME_MS = 150;
    private static final int SCENE_FRAMES = 44;
    // Сердечко: кадров (по 150 мс) и на сколько dp поднимается
    private static final int HEART_FRAMES = 9;
    private static final int HEART_RISE_DP = 44;
    // Пустые кадры после: голова поднимется (PET_MS) раньше, чем сердечко начнётся заново
    private static final int HEART_PAUSE_FRAMES = 4;
    // Пауза до ответа: кивок — три кадра сценки (вниз, вниз, прямо) с запасом,
    // потом — сценка «думает» без кивка; ответа нет столько — перестать думать
    private static final long NOD_MS = 500;
    private static final long THINK_MAX_MS = 3 * 60_000L;
    private static final int THINK_FRAMES = 48;
    // Читает сообщение пользователя столько, сколько его читали бы глазами
    private static final long READ_PER_CHAR_MS = 60;
    private static final long READ_MIN_MS = 1_200;
    private static final long READ_MAX_MS = 6_000;
    // Облачко пользователя: доля ширины виджета (правее — голова на виду)
    private static final float USER_BUBBLE_W = 0.62f;
    // Одна реплика двумя путями (лента ответов, входящие, ответ окну
    // «Написать») — второй раз не говорит
    private static final long SAID_DEDUP_MS = 2 * 60_000L;
    // Своя отправка из окна «Написать» и heard ядра на неё — один кивок (и
    // одно чтение: то же сообщение, пока персона думает над ним, — не заново)
    private static final long NOD_GAP_MS = 3_000L;
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
    // Пауза до ответа: до какого времени думает (предел), кивает и читает;
    // сообщение пользователя для облачка (только на время хода, на диск не пишется)
    private static final Map<String, Long> thinkingUntil = new HashMap<>();
    private static final Map<String, Long> noddingUntil = new HashMap<>();
    private static final Map<String, Long> readingUntil = new HashMap<>();
    private static final Map<String, String> userSaid = new HashMap<>();
    // Недавние реплики персоны (текст → когда): та же второй раз не нужна
    private static final Map<String, Map<String, Long>> recentSaid = new HashMap<>();
    // Когда последний раз кивнула (услышала реплику)
    private static final Map<String, Long> noddedAt = new HashMap<>();
    // Что виджет показывает сейчас: то же самое заново не отправлять лаунчеру
    private static final Map<Integer, String> shownState = new HashMap<>();
    // Реплика, которую портрет сейчас «говорит» по словам (или сказал до конца)
    private static final Map<String, Speech> speech = new HashMap<>();
    // Скорость текста (слов/с, 0 — сразу; секунд на часть); null — ещё не читали prefs
    private static volatile float[] textSpeed;
    // Размер виджета уже записан в журнал (виджет → «ш×в»)
    private static final Map<Integer, String> loggedSize = new HashMap<>();

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
                PortraitSaidFeed.update(app);
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
        if (ACTION_OUT.equals(intent.getAction()) || ACTION_BACK.equals(intent.getAction())) {
            stepOutOrBack(ctx, intent);
            return;
        }
        if (!ACTION_PET.equals(intent.getAction())) {
            super.onReceive(ctx, intent);
            return;
        }
        int id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID);
        String persona = prefs(ctx).getString("w." + id, null);
        if (persona == null) return;
        // Гладят — нажат виджет целиком: его настоящее место и размер на экране
        // (размер из параметров виджета лаунчер Honor даёт с полями) — для выхода поверх
        Rect whole = intent.getSourceBounds();
        if (whole != null && !whole.isEmpty()) prefs(ctx).edit().putString("wb." + id, whole.flattenToString()).apply();
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

    // Кнопка «Поверх приложений» (OUT) или тап по виджету, пока персона там (BACK)
    private static void stepOutOrBack(Context ctx, Intent intent) {
        int id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID);
        String persona = prefs(ctx).getString("w." + id, null);
        if (persona == null) return;
        Rect src = intent.getSourceBounds();
        if (ACTION_BACK.equals(intent.getAction())) {
            // «Вернуть»: персона, что сейчас поверх, — сразу на этот виджет
            AvatarOverlayService.returnToWidget(ctx, id);
            return;
        }
        // Кнопка выхода — в левом нижнем углу (отступ 2 dp): от неё — весь
        // виджет, от него — где в нём голова
        Rect head = null;
        if (src != null) {
            float dp = ctx.getResources().getDisplayMetrics().density;
            int left = src.left - Math.round(2 * dp);
            int bottom = src.bottom + Math.round(2 * dp);
            // Виджет целиком уже видели (гладили) и он там же — его размер точный;
            // нет — из параметров виджета (у некоторых лаунчеров — с полями)
            Rect whole = Rect.unflattenFromString(prefs(ctx).getString("wb." + id, ""));
            int slack = Math.round(6 * dp);
            if (whole == null || Math.abs(whole.left - left) > slack || Math.abs(whole.bottom - bottom) > slack) {
                int[] wh = sizeDp(ctx, id);
                whole = new Rect(left, bottom - Math.round(wh[1] * dp), left + Math.round(wh[0] * dp), bottom);
            }
            head = headOnScreen(ctx, id, whole);
        }
        if (!AvatarOverlayService.canDraw(ctx)) {
            Toast.makeText(ctx, R.string.portrait_w_no_overlay, Toast.LENGTH_LONG).show();
            return;
        }
        AvatarOverlayService.startFromWidget(ctx, id, persona, prefs(ctx).getString("n." + persona, persona), head);
    }

    /** Этот виджет ещё на рабочем столе. */
    static boolean hasWidget(Context ctx, int widgetId) {
        return configured(ctx).containsKey(widgetId);
    }

    /** Есть ли на рабочем столе портрет этой персоны (меню головы: «На виджет» или «Убрать»). */
    static boolean hasWidgetFor(Context ctx, String persona) {
        return configured(ctx).containsValue(persona);
    }

    // Размер виджета, dp (книжная ориентация, как geometry): {ширина, высота}
    private static int[] sizeDp(Context ctx, int widgetId) {
        int w = 0;
        int h = 0;
        AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
        if (mgr != null) {
            Bundle o = mgr.getAppWidgetOptions(widgetId);
            if (o != null) {
                w = o.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH, 0);
                h = o.getInt(AppWidgetManager.OPTION_APPWIDGET_MAX_HEIGHT, 0);
            }
        }
        if (w <= 0) w = DEFAULT_SIDE_DP;
        if (h <= 0) h = w;
        return new int[]{w, h};
    }

    // Голова на экране: виджет без отступа над головой, в нём кадр по центру (fitCenter, квадрат)
    private static Rect headOnScreen(Context ctx, int widgetId, Rect widget) {
        int pad = geometry(ctx, widgetId).padTopPx;
        Rect area = new Rect(widget.left, widget.top + pad, widget.right, widget.bottom);
        int side = Math.min(area.width(), area.height());
        return new Rect(area.centerX() - side / 2, area.centerY() - side / 2,
                area.centerX() - side / 2 + side, area.centerY() - side / 2 + side);
    }

    @Override
    public void onAppWidgetOptionsChanged(Context ctx, AppWidgetManager mgr, int id, Bundle options) {
        // Размер поменяли — отступ над головой и строки облачка другие
        Context app = ctx.getApplicationContext();
        PendingResult r = goAsync();
        IO.execute(() -> {
            try {
                shownState.remove(id);
                renderAllNow(app);
            } finally {
                r.finish();
            }
        });
    }

    @Override
    public void onDeleted(Context ctx, int[] ids) {
        SharedPreferences.Editor e = prefs(ctx).edit();
        for (int id : ids) e.remove("w." + id).remove("wb." + id);
        e.apply();
        // Последний портрет убрали — ленте реплик больше некого показывать
        PortraitSaidFeed.update(ctx);
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

    /** Персона написала — её портрет говорит, над головой облачко (BackgroundInboxService). */
    static void personaSpoke(Context ctx, String persona, String text) {
        if (persona == null || persona.isEmpty()) return;
        AvatarOverlayService.said(ctx, persona, "spoke", text);
        if (AvatarOverlayService.silences(ctx, persona)) return;
        Context app = ctx.getApplicationContext();
        IO.execute(() -> {
            if (!configured(app).containsValue(persona)) return;
            spoke(app, persona, text);
        });
    }

    /**
     * Лента реплик ядра (PortraitSaidFeed): heard — пользователь написал
     * персоне, text — его сообщение (кивнуть, прочесть его в облачке,
     * задуматься), settled — ход кончился, reply — ответ в чате (говорит,
     * облачко — если приложения не видно). Окно «Написать» зовёт сюда же —
     * само, не дожидаясь ленты: её может не быть (служба выключена).
     */
    static void said(Context ctx, String persona, String kind, String text) {
        if (persona == null || persona.isEmpty()) return;
        // И аватару поверх приложений (если он на экране и это его персона);
        // персона там — виджет пуст, говорить ему нечем
        AvatarOverlayService.said(ctx, persona, kind, text);
        if (AvatarOverlayService.silences(ctx, persona)) return;
        Context app = ctx.getApplicationContext();
        IO.execute(() -> {
            if (!configured(app).containsValue(persona)) return;
            long now = System.currentTimeMillis();
            String t = text == null ? "" : text.trim();
            switch (kind) {
                case "heard": {
                    String mine = t.length() > BUBBLE_MAX_CHARS ? t.substring(0, BUBBLE_MAX_CHARS) : t;
                    // Окно «Написать» уже показало это сообщение и персона над ним
                    // думает — heard ядра на ту же реплику не начинает всё заново
                    if (!mine.isEmpty() && thinking(persona, now) && mine.equals(userSaid.get(persona))) return;
                    // Новая реплика пользователя: прежний ответ в облачке уже не к месту
                    talkingUntil.remove(persona);
                    // Новый ход: тот же ответ, что на прошлую реплику («Да.»), — уже не повтор
                    recentSaid.remove(persona);
                    cancelSpeech(persona);
                    startThinking(app, persona, now);
                    // Окно «Написать» уже кивнуло — heard ядра на ту же реплику второго не нужно
                    long readFrom = now;
                    Long nodded = noddedAt.get(persona);
                    if (nodded == null || now - nodded >= NOD_GAP_MS) {
                        noddedAt.put(persona, now);
                        noddingUntil.put(persona, now + NOD_MS);
                        IO.schedule(() -> renderAllNow(app), NOD_MS + 50, TimeUnit.MILLISECONDS);
                        readFrom = now + NOD_MS;
                    }
                    // Сообщение — облачком сбоку: кивнула, поворачивается к нему и
                    // читает, потом думает; без текста (картинка) — сразу думает
                    if (mine.isEmpty()) {
                        userSaid.remove(persona);
                        readingUntil.remove(persona);
                    } else {
                        long read = Math.max(READ_MIN_MS, Math.min(READ_MAX_MS, READ_PER_CHAR_MS * t.length()));
                        userSaid.put(persona, mine);
                        readingUntil.put(persona, readFrom + read);
                        IO.schedule(() -> renderAllNow(app), readFrom + read - now + 50, TimeUnit.MILLISECONDS);
                    }
                    SharedPreferences p = prefs(app);
                    SharedPreferences.Editor e = p.edit().remove("b." + persona).remove("bt." + persona);
                    // Спала — проснулась (как от поглаживания): ответ без неё не напишется
                    if (asleep(p, persona)) {
                        e.putLong("wk." + persona, now + WAKE_MS);
                        IO.schedule(() -> renderAllNow(app), WAKE_MS + 1_000, TimeUnit.MILLISECONDS);
                    }
                    e.apply();
                    break;
                }
                case "settled":
                    stopThinkingNow(persona);
                    break;
                case "reply":
                    stopThinkingNow(persona);
                    spoke(app, persona, BackgroundInboxService.appVisible ? null : t);
                    return; // spoke перерисовал
                default:
                    return;
            }
            renderAllNow(app);
        });
    }

    /** Лента реплик остановлена (экран погас, виджет убрали): конца хода не узнать — не думать. */
    static void saidStopped(Context ctx) {
        AvatarOverlayService.saidStopped();
        Context app = ctx.getApplicationContext();
        IO.execute(() -> {
            if (thinkingUntil.isEmpty() && userSaid.isEmpty()) return;
            thinkingUntil.clear();
            noddingUntil.clear();
            readingUntil.clear();
            userSaid.clear();
            renderAllNow(app);
        });
    }

    /** Есть ли на рабочем столе портрет с выбранной персоной (PortraitSaidFeed). */
    static boolean hasWidgets(Context ctx) {
        return !configured(ctx).isEmpty();
    }

    // Только в потоке IO
    private static void startThinking(Context ctx, String persona, long now) {
        thinkingUntil.put(persona, now + THINK_MAX_MS);
        IO.schedule(() -> {
            Long until = thinkingUntil.get(persona);
            if (until == null || until > System.currentTimeMillis()) return;
            // Ни ответа, ни конца хода — больше не ждём
            stopThinkingNow(persona);
            renderAllNow(ctx);
        }, THINK_MAX_MS + 50, TimeUnit.MILLISECONDS);
    }

    private static void stopThinkingNow(String persona) {
        thinkingUntil.remove(persona);
        noddingUntil.remove(persona);
        readingUntil.remove(persona);
        userSaid.remove(persona);
    }

    private static boolean thinking(String persona, long now) {
        Long until = thinkingUntil.get(persona);
        return until != null && until > now;
    }

    // Только в потоке IO: персона сказала text (null — без облачка)
    private static void spoke(Context app, String persona, String text) {
        long now = System.currentTimeMillis();
        String said = text == null ? "" : text.trim();
        if (!said.isEmpty()) {
            // Тот же ответ вторым путём (лента ответов, входящие, окно
            // «Написать») — уже сказан; ответ бывает из нескольких сообщений,
            // и пути приносят их вперемешку — помним все недавние
            Map<String, Long> recent = recentSaid.computeIfAbsent(persona, k -> new LinkedHashMap<>());
            recent.values().removeIf(at -> now - at >= SAID_DEDUP_MS);
            if (recent.containsKey(said)) return;
            recent.put(said, now);
            if (said.length() > BUBBLE_MAX_CHARS) said = said.substring(0, BUBBLE_MAX_CHARS - 1) + ELLIPSIS;
            prefs(app).edit()
                    .putString("b." + persona, said)
                    .putLong("bt." + persona, now)
                    .apply();
            say(app, persona, said);
            renderAllNow(app);
            return;
        }
        // Без облачка (приложение на экране) — только губы
        PortraitWidgetFrames.Frames f = PortraitWidgetFrames.load(app, persona, FRAME_PX);
        if (f != null && f.canTalk()) {
            // Уже говорит (пачка сообщений) — только продлить
            talkingUntil.put(persona, now + TALK_MS);
            IO.schedule(() -> stopTalking(app, persona), TALK_MS + 50, TimeUnit.MILLISECONDS);
        }
        renderAllNow(app);
    }

    /** Приложение на экране: реплики теперь видно в чате — облачка убрать. */
    static void clearBubbles(Context ctx) {
        Context app = ctx.getApplicationContext();
        IO.execute(() -> {
            SharedPreferences p = prefs(app);
            SharedPreferences.Editor e = p.edit();
            boolean any = false;
            for (String persona : new ArrayList<>(speech.keySet())) cancelSpeech(persona);
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

    // ── Реплика по словам ─────────────────────────────────────────────

    /**
     * Реплика, которую портрет «говорит»: части под облачко (splitParts),
     * в части — слово за словом. Только в потоке IO.
     */
    private static final class Speech {
        // Реплика целиком, как в облачке (b.<персона>): облачко показывает
        // уже другое — эта больше не говорится
        String key;
        final List<String> parts;
        int part = 0;
        // Слов текущей части на виду; 0 — ещё не начинала
        int words = 0;
        boolean running = false;
        boolean done = false;
        ScheduledFuture<?> next;

        Speech(String key, List<String> parts) {
            this.key = key;
            this.parts = parts;
        }

        String[] partWords(int i) {
            return parts.get(i).split(" ");
        }

        String shown() {
            String[] w = partWords(part);
            StringBuilder out = new StringBuilder();
            for (int i = 0; i < Math.min(words, w.length); i++) {
                if (i > 0) out.append(' ');
                out.append(w[i]);
            }
            return out.toString();
        }

        /** Слов части на виду в её начале: «сразу» — все. */
        int firstWords() {
            return speed()[0] > 0 ? 1 : partWords(part).length;
        }

        /** Сколько ждать до следующего слова или части; -1 — сказано всё. */
        long delayAfter() {
            String[] w = partWords(part);
            if (words < w.length) return wordMs(w[words]);
            if (part + 1 < parts.size()) return partMs(parts.get(part));
            return -1;
        }

        /** Сколько ещё говорить: оставшиеся слова и чтение частей. */
        long remainingMs() {
            long ms = 0;
            for (int i = part; i < parts.size(); i++) {
                String[] w = partWords(i);
                for (int j = i == part ? words : 0; j < w.length; j++) ms += wordMs(w[j]);
                if (i + 1 < parts.size()) ms += partMs(parts.get(i));
            }
            return ms;
        }
    }

    private static long wordMs(String word) {
        float wps = speed()[0];
        if (wps <= 0) return 0;
        long ms = Math.min(WORD_MAX_MS, WORD_BASE_MS + WORD_PER_CHAR_MS * word.length());
        return Math.round(ms * TEXT_WPS / wps);
    }

    private static long partMs(String part) {
        long ms = Math.max(PART_MIN_MS, Math.min(PART_MAX_MS, PART_PER_CHAR_MS * part.length()));
        return Math.round(ms * speed()[1] / TEXT_PART_SEC);
    }

    /** Слово реплики — сколько (скорость текста из «Общих»); 0 — все сразу. И аватару поверх приложений. */
    static long wordMsFor(Context ctx, String word) {
        loadSpeed(ctx);
        return wordMs(word);
    }

    /** Часть реплики висит столько, чтобы её прочесть (скорость текста из «Общих»). */
    static long partMsFor(Context ctx, String part) {
        loadSpeed(ctx);
        return partMs(part);
    }

    /** Скорость текста: {слов/с (0 — сразу), секунд на часть}. */
    private static float[] speed() {
        float[] sp = textSpeed;
        return sp != null ? sp : new float[]{TEXT_WPS, TEXT_PART_SEC};
    }

    private static void loadSpeed(Context ctx) {
        if (textSpeed != null) return;
        SharedPreferences p = prefs(ctx);
        textSpeed = new float[]{p.getFloat("tw", TEXT_WPS), p.getFloat("tp", TEXT_PART_SEC)};
    }

    /**
     * Скорость текста с ядра ({words_per_sec, part_sec} — лента реплик,
     * /api/portraits): запомнить; со следующей реплики — по ней. Значение
     * вне пределов (как в app/core/avatar_text) — не трогать.
     */
    static void setTextSpeed(Context ctx, JSONObject text) {
        if (text == null) return;
        double w = text.optDouble("words_per_sec", Double.NaN);
        double t = text.optDouble("part_sec", Double.NaN);
        boolean okW = w == 0 || (w >= 1 && w <= 20);
        boolean okT = t >= 1.5 && t <= 30;
        if (!okW && !okT) return;
        float[] cur = speed();
        float[] next = {okW ? (float) w : cur[0], okT ? (float) t : cur[1]};
        if (next[0] == cur[0] && next[1] == cur[1] && textSpeed != null) return;
        textSpeed = next;
        prefs(ctx).edit().putFloat("tw", next[0]).putFloat("tp", next[1]).apply();
    }

    /**
     * Сказать key: экран смотрят — сразу по словам, нет — когда разблокируют
     * (resumeSpeech). Прошлое сообщение того же ответа ещё говорится — это
     * следом, теми же частями.
     */
    private static void say(Context ctx, String persona, String key) {
        loadSpeed(ctx);
        List<String> parts = partsFor(ctx, persona, key);
        if (parts.isEmpty()) return;
        Speech s = speech.get(persona);
        if (s != null && !s.done) {
            for (String part : parts) if (s.parts.size() < MAX_PARTS) s.parts.add(part);
            s.key = key;
            if (s.running) talk(ctx, persona, s);
            return;
        }
        cancelSpeech(persona);
        s = new Speech(key, parts);
        speech.put(persona, s);
        if (looking(ctx)) run(ctx, persona, s);
    }

    private static void run(Context ctx, String persona, Speech s) {
        s.running = true;
        if (s.words == 0) s.words = s.firstWords();
        talk(ctx, persona, s);
        renderAllNow(ctx);
        // Перерисовка могла не понадобиться (то же состояние) — слова отдельно
        push(ctx, persona, s.shown());
        next(ctx, persona, s);
    }

    private static void next(Context ctx, String persona, Speech s) {
        long delay = s.delayAfter();
        if (delay < 0) {
            s.running = false;
            s.done = true;
            s.next = null;
            return;
        }
        s.next = IO.schedule(() -> tick(ctx, persona, s), delay, TimeUnit.MILLISECONDS);
    }

    private static void tick(Context ctx, String persona, Speech s) {
        if (speech.get(persona) != s || !s.running) return;
        if (!current(ctx, persona, s)) {
            cancelSpeech(persona);
            return;
        }
        // Экран погас или заблокирован — договорит, когда посмотрят
        if (!looking(ctx)) {
            s.running = false;
            s.next = null;
            return;
        }
        if (s.words < s.partWords(s.part).length) {
            s.words++;
        } else {
            s.part++;
            s.words = s.firstWords();
        }
        push(ctx, persona, s.shown());
        next(ctx, persona, s);
    }

    /** Губы — пока говорит. */
    private static void talk(Context ctx, String persona, Speech s) {
        PortraitWidgetFrames.Frames f = PortraitWidgetFrames.load(ctx, persona, FRAME_PX);
        if (f == null || !f.canTalk()) return;
        long ms = Math.max(TALK_MIN_MS, Math.min(TALK_MAX_MS, s.remainingMs()));
        talkingUntil.put(persona, System.currentTimeMillis() + ms);
        IO.schedule(() -> stopTalking(ctx, persona), ms + 50, TimeUnit.MILLISECONDS);
    }

    private static void cancelSpeech(String persona) {
        Speech s = speech.remove(persona);
        if (s != null && s.next != null) s.next.cancel(false);
    }

    /** Облачко показывает ещё эту реплику (не сменилась, не устарела). */
    private static boolean current(Context ctx, String persona, Speech s) {
        return s.key.equals(bubble(prefs(ctx), persona));
    }

    /** Что в облачке сейчас: сказанное по словам, а не начинала — вся реплика (said). */
    private static String shownNow(String persona, String said) {
        Speech s = speech.get(persona);
        if (s == null || !s.key.equals(said) || s.words == 0) return said;
        return s.shown();
    }

    /**
     * Экран включили или разблокировали: реплики, которые ждали взгляда, — по
     * словам. Сигнал приходит, пока экран ещё считается заблокированным, —
     * проверить ещё несколько раз (RESUME_RETRY_MS); на экране блокировки
     * так и не дождётся — дождётся разблокировки.
     */
    static void resumeSpeech(Context ctx) {
        Context app = ctx.getApplicationContext();
        IO.execute(() -> resumeTry(app, 0));
    }

    private static void resumeTry(Context app, int attempt) {
        boolean waiting = false;
        for (Speech s : speech.values()) waiting |= !s.running && !s.done;
        if (!waiting) return;
        if (!looking(app)) {
            if (attempt < RESUME_RETRY_MS.length) {
                IO.schedule(() -> resumeTry(app, attempt + 1), RESUME_RETRY_MS[attempt], TimeUnit.MILLISECONDS);
            }
            return;
        }
        for (Map.Entry<String, Speech> e : new ArrayList<>(speech.entrySet())) {
            Speech s = e.getValue();
            if (s.running || s.done) continue;
            if (current(app, e.getKey(), s)) run(app, e.getKey(), s);
            else cancelSpeech(e.getKey());
        }
    }

    /** Слова в облачке — без перерисовки кадров (частичное обновление виджетов персоны). */
    private static void push(Context ctx, String persona, String text) {
        AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
        if (mgr == null || PortraitWidgetFrames.load(ctx, persona, FRAME_PX) == null) return;
        for (Map.Entry<Integer, String> w : configured(ctx).entrySet()) {
            if (!w.getValue().equals(persona)) continue;
            RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_portrait);
            v.setViewVisibility(R.id.portrait_bubble_box, View.VISIBLE);
            v.setTextViewText(R.id.portrait_bubble, text);
            try {
                mgr.partiallyUpdateAppWidget(w.getKey(), v);
            } catch (RuntimeException e) {
                Log.w(TAG, "виджет " + w.getKey() + ": слова не обновлены", e);
            }
        }
    }

    /** На виджет смотрят: экран включён и не заблокирован. */
    private static boolean looking(Context ctx) {
        PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
        if (pm != null && !pm.isInteractive()) return false;
        KeyguardManager km = (KeyguardManager) ctx.getSystemService(Context.KEYGUARD_SERVICE);
        return km == null || !km.isKeyguardLocked();
    }

    /** Части реплики под облачко самого узкого виджета этой персоны. */
    private static List<String> partsFor(Context ctx, String persona, String text) {
        int width = Integer.MAX_VALUE;
        int lines = Integer.MAX_VALUE;
        for (Map.Entry<Integer, String> w : configured(ctx).entrySet()) {
            if (!w.getValue().equals(persona)) continue;
            Geometry g = geometry(ctx, w.getKey());
            width = Math.min(width, g.textWidthPx);
            lines = Math.min(lines, g.lines);
        }
        if (width == Integer.MAX_VALUE) {
            Geometry g = geometry(ctx, AppWidgetManager.INVALID_APPWIDGET_ID);
            width = g.textWidthPx;
            lines = g.lines;
        }
        TextPaint paint = new TextPaint(Paint.ANTI_ALIAS_FLAG);
        paint.setTextSize(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, BUBBLE_TEXT_SP,
                ctx.getResources().getDisplayMetrics()));
        // С запасом: лаунчер мог отдать виджету чуть меньше, чем говорит
        int w = Math.round(width * 0.92f);
        int max = lines;
        List<String> parts = splitParts(text, s -> StaticLayout.Builder.obtain(s, 0, s.length(), paint, w)
                .build().getLineCount() <= max);
        return parts.size() > MAX_PARTS ? new ArrayList<>(parts.subList(0, MAX_PARTS)) : parts;
    }

    /**
     * Длинная реплика — по частям, чтобы каждая влезала в облачко целиком: по
     * предложениям, а предложение длиннее облачка — по словам, с многоточием
     * на месте разреза (как bubbleParts.ts портрета на компьютере).
     */
    static List<String> splitParts(String text, Predicate<String> fits) {
        List<String> units = new ArrayList<>();
        for (String u : SENTENCE_END.split(text)) {
            String t = u.replaceAll("\\s+", " ").trim();
            if (!t.isEmpty()) units.add(t);
        }
        List<String> parts = new ArrayList<>();
        String cur = "";
        for (String unit : units) {
            String joined = cur.isEmpty() ? unit : cur + " " + unit;
            if (fits.test(joined)) {
                cur = joined;
                continue;
            }
            if (!cur.isEmpty()) parts.add(cur);
            cur = "";
            if (fits.test(unit)) {
                cur = unit;
                continue;
            }
            // Предложение само не влезает — по словам; слово длиннее облачка
            // облачко перенесёт по буквам, но часть из него одна
            for (String word : unit.split(" ")) {
                String next = cur.isEmpty() ? word : cur + " " + word;
                if (cur.isEmpty() || fits.test(next + ELLIPSIS)) {
                    cur = next;
                    continue;
                }
                parts.add(cur + ELLIPSIS);
                cur = word;
            }
        }
        if (!cur.isEmpty()) parts.add(cur);
        return parts;
    }

    /**
     * Размер виджета: отступ сцены сверху, строк в облачке, ширина текста
     * облачка; наибольшая ширина облачка пользователя.
     */
    private static final class Geometry {
        final int padTopPx;
        final int lines;
        final int textWidthPx;
        final int userWidthPx;

        Geometry(int padTopPx, int lines, int textWidthPx, int userWidthPx) {
            this.padTopPx = padTopPx;
            this.lines = lines;
            this.textWidthPx = textWidthPx;
            this.userWidthPx = userWidthPx;
        }
    }

    /**
     * Голова — квадрат по ширине: в высоком виджете она прижата к низу, а всё
     * место над ней — облачку (облачко лежит поверх и в квадратном виджете
     * заходит на макушку). Места хватает — три строки в облачке, нет — две.
     * Размер — из параметров виджета (книжная ориентация); нет их — DEFAULT_SIDE_DP.
     */
    private static Geometry geometry(Context ctx, int widgetId) {
        int w = 0;
        int h = 0;
        AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
        if (mgr != null && widgetId != AppWidgetManager.INVALID_APPWIDGET_ID) {
            Bundle o = mgr.getAppWidgetOptions(widgetId);
            if (o != null) {
                w = o.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH, 0);
                h = o.getInt(AppWidgetManager.OPTION_APPWIDGET_MAX_HEIGHT, 0);
            }
        }
        if (w <= 0) w = DEFAULT_SIDE_DP;
        if (h <= 0) h = w;
        int pad = Math.max(0, Math.min(h - w, Math.round(h * 0.4f)));
        int lines = pad >= 20 ? 3 : 2;
        int text = Math.max(60, w - BUBBLE_TEXT_PAD_DP);
        int user = Math.max(60, Math.round(w * USER_BUBBLE_W));
        String size = w + "×" + h;
        if (widgetId != AppWidgetManager.INVALID_APPWIDGET_ID && !size.equals(loggedSize.get(widgetId))) {
            loggedSize.put(widgetId, size);
            Log.i(TAG, "виджет " + widgetId + ": " + size + " dp, над головой " + pad + " dp, строк " + lines);
        }
        float dp = ctx.getResources().getDisplayMetrics().density;
        return new Geometry(Math.round(pad * dp), lines, Math.round(text * dp), Math.round(user * dp));
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
        PortraitSaidFeed.update(ctx);
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
        setTextSpeed(ctx, list.optJSONObject("text"));
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

    /**
     * В чате сменился режим персоны (лента реплик, kind mode): у режима
     * бывает свой аватар (app/api/avatar_modes.py) — скачать живой и
     * перерисовать, если персона на рабочем столе или поверх приложений.
     */
    static void modeChanged(Context ctx, String persona) {
        if (persona == null || persona.isEmpty()) return;
        Context app = ctx.getApplicationContext();
        IO.execute(() -> {
            if (!configured(app).containsValue(persona) && !AvatarOverlayService.shows(persona)) return;
            fetch(app, persona);
            renderAllNow(app);
        });
    }

    // live=1 — листы режима персоны, что включён в чате (старое ядро
    // параметр не знает и отдаёт обычные)
    private static void fetch(Context ctx, String persona) {
        SharedPreferences p = prefs(ctx);
        try {
            JSONObject portrait = new JSONObject(RoomWidget.request(ctx, "GET",
                    "/api/personas/" + RoomWidget.enc(persona) + "/portrait?live=1", null));
            PortraitWidgetFrames.save(ctx, persona, portrait);
            // Голова поверх приложений режет те же листы — пусть возьмёт новые
            AvatarOverlayService.portraitChanged(persona);
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
    static boolean sleepsNow(SharedPreferences p, String persona) {
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
        Long nod = noddingUntil.get(persona);
        boolean think = thinking(persona, now);
        Long read = readingUntil.get(persona);
        String user = think ? userSaid.get(persona) : null;
        String b = bubble(p, persona);
        Geometry g = geometry(ctx, widgetId);
        return persona + "|" + p.getLong("t." + persona, 0) + "|" + p.getString("e." + persona, "")
                + "|" + g.padTopPx + ":" + g.lines
                + "|" + RoomWidget.serviceEnabled(ctx)
                + "|" + (talk != null && talk > now) + "|" + (pat != null && pat > now)
                + "|" + sleepsNow(p, persona) + "|" + (b == null ? "" : b.hashCode() + ":" + p.getLong("bt." + persona, 0))
                + "|" + think + "|" + (think && nod != null && nod > now) + "|" + (think && read != null && read > now)
                + "|" + (user == null ? "" : user.hashCode())
                + "|" + AvatarOverlayService.awayFor(ctx, widgetId, persona);
    }

    private static RemoteViews build(Context ctx, int widgetId, int framePx) {
        RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_portrait);
        SharedPreferences p = prefs(ctx);
        String persona = p.getString("w." + widgetId, null);
        v.setViewVisibility(R.id.portrait_bubble_box, View.GONE);
        v.setViewVisibility(R.id.portrait_user_box, View.GONE);
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
        // Персона вышла на экран поверх приложений — её здесь нет: плашка «Вернуть»
        if (AvatarOverlayService.awayFor(ctx, widgetId, persona)) return awayView(ctx, widgetId, persona, name, framePx);

        PortraitWidgetFrames.Frames frames = PortraitWidgetFrames.load(ctx, persona, framePx);
        if (frames != null) {
            long now = System.currentTimeMillis();
            Long until = talkingUntil.get(persona);
            Long pat = pettedUntil.get(persona);
            boolean talking = until != null && until > now && frames.canTalk();
            boolean petted = pat != null && pat > now;
            // Думает над ответом (заговорила — уже нет); только услышала — сначала кивок
            boolean thinking = !talking && thinking(persona, now);
            Long nod = noddingUntil.get(persona);
            boolean nodding = thinking && nod != null && nod > now;
            // Сначала читает сообщение пользователя (облачко сбоку), потом думает
            Long read = readingUntil.get(persona);
            boolean reading = thinking && read != null && read > now;
            String user = thinking ? userSaid.get(persona) : null;
            String said = bubble(p, persona);
            // Пишет, думает или недавно гладили — не спит
            boolean sleeping = !talking && !thinking && said == null && sleepsNow(p, persona);
            if (sleeping && !petted) return sleepView(ctx, widgetId, persona, name, frames.down);
            List<Bitmap> scene;
            if (petted || sleeping) {
                scene = new ArrayList<>();
                scene.add(frames.down);
            } else if (talking) {
                scene = frames.talk(SCENE_FRAMES, new Random());
            } else if (reading) {
                // Кадров — на остаток чтения после кивка: дочитала — перерисовка
                // (said) сменит сценку на «думает»
                long from = nodding ? nod : now;
                scene = frames.read((int) Math.max(4, (read - from) / FRAME_MS), new Random(), nodding);
            } else if (thinking) {
                scene = frames.think(THINK_FRAMES, new Random(), nodding);
            } else {
                scene = frames.idle(SCENE_FRAMES, new Random());
            }
            v.setViewVisibility(R.id.portrait_flipper, View.VISIBLE);
            v.setViewVisibility(R.id.portrait_hint, View.GONE);
            Geometry g = geometry(ctx, widgetId);
            v.setViewPadding(R.id.portrait_stage, 0, g.padTopPx, 0, 0);
            v.setInt(R.id.portrait_bubble, "setMaxLines", g.lines);
            // Лаунчер может наложить обновление на уже показанный виджет
            // (тот же макет) — старые кадры убрать, а не дописать к ним
            v.removeAllViews(R.id.portrait_flipper);
            for (Bitmap b : scene) {
                RemoteViews frame = new RemoteViews(ctx.getPackageName(), R.layout.widget_portrait_frame);
                frame.setImageViewBitmap(R.id.portrait_frame, b);
                v.addView(R.id.portrait_flipper, frame);
            }
            effects(ctx, v, petted, sleeping);
            // Посреди хода персона написала сама (инициатива) — её облачко
            // главнее: оба сверху, одно на другом не читаются
            if (user != null && said == null) {
                v.setViewVisibility(R.id.portrait_user_box, View.VISIBLE);
                v.setTextViewText(R.id.portrait_user, user);
                v.setInt(R.id.portrait_user, "setMaxWidth", g.userWidthPx);
                v.setContentDescription(R.id.portrait_user, ctx.getString(R.string.portrait_w_user, user));
                // Тап — чат с персоной, как по её облачку
                v.setOnClickPendingIntent(R.id.portrait_user_box, chatIntent(ctx, widgetId, persona));
            }
            if (said != null) {
                v.setViewVisibility(R.id.portrait_bubble_box, View.VISIBLE);
                // Сказано пока не всё — только сказанное; не начинала (никто
                // не смотрел) — вся реплика, сколько влезет
                v.setTextViewText(R.id.portrait_bubble, shownNow(persona, said));
                v.setContentDescription(R.id.portrait_bubble, name + ": " + said);
                // Тап по облачку — чат с персоной
                v.setOnClickPendingIntent(R.id.portrait_bubble_box, chatIntent(ctx, widgetId, persona));
            }
            petOnTap(ctx, v, widgetId, name);
            writeOnTap(ctx, v, widgetId, persona, name);
            outOnTap(ctx, v, widgetId, name);
            // Сообщение пользователя слева вверху — кнопка выбора персоны под ним не нужна
            switchOnTap(ctx, v, widgetId, user == null || said != null);
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

    /** Чат с персоной в приложении (тап по облачку). */
    private static PendingIntent chatIntent(Context ctx, int widgetId, String persona) {
        Intent chat = new Intent(ctx, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra(BackgroundInboxService.EXTRA_PERSONA, persona);
        return PendingIntent.getActivity(ctx, 0x4000 + widgetId, chat,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    /** Спит: одна неподвижная картинка и «z»; тап — погладить (сердечко — уже в обычном макете). */
    private static RemoteViews sleepView(Context ctx, int widgetId, String persona, String name, Bitmap still) {
        RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_portrait_sleep);
        v.setImageViewBitmap(R.id.portrait_still, still);
        v.setViewPadding(R.id.portrait_root, 0, geometry(ctx, widgetId).padTopPx, 0, 0);
        zFrames(ctx, v);
        petOnTap(ctx, v, widgetId, name);
        writeOnTap(ctx, v, widgetId, persona, name);
        outOnTap(ctx, v, widgetId, name);
        switchOnTap(ctx, v, widgetId, true);
        return v;
    }

    /** Карандаш в углу — окно «Написать» (поле над клавиатурой). Портрета нет — карандаша тоже: тап и так открывает чат. */
    private static void writeOnTap(Context ctx, RemoteViews v, int widgetId, String persona, String name) {
        v.setViewVisibility(R.id.portrait_write, View.VISIBLE);
        v.setContentDescription(R.id.portrait_write, ctx.getString(R.string.portrait_w_write, name));
        Intent write = new Intent(ctx, PortraitInputActivity.class)
                .putExtra(PortraitInputActivity.EXTRA_PERSONA, persona)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        v.setOnClickPendingIntent(R.id.portrait_write, PendingIntent.getActivity(ctx, 0x7000 + widgetId, write,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
    }

    /** Тап по голове — погладить. */
    /**
     * Персона поверх приложений: виджет прозрачный, на месте головы —
     * стеклянная карточка «Вернуть» (тап — голова едет обратно).
     */
    private static RemoteViews awayView(Context ctx, int widgetId, String persona, String name, int framePx) {
        RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_portrait_away);
        // Карточка — по центру места головы (под отступом над головой)
        v.setViewPadding(R.id.portrait_root, 0, geometry(ctx, widgetId).padTopPx, 0, 0);
        v.setContentDescription(R.id.portrait_away, ctx.getString(R.string.portrait_w_away, name));
        Intent back = new Intent(ctx, PortraitWidget.class)
                .setAction(ACTION_BACK)
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId);
        v.setOnClickPendingIntent(R.id.portrait_away, PendingIntent.getBroadcast(ctx, 0x8800 + widgetId,
                back, PendingIntent.FLAG_UPDATE_CURRENT | boundsFlag()));
        return v;
    }

    // Кнопка «Сменить персону» (левый верхний угол): выбор персоны, как при установке виджета
    private static void switchOnTap(Context ctx, RemoteViews v, int widgetId, boolean visible) {
        v.setViewVisibility(R.id.portrait_switch, visible ? View.VISIBLE : View.GONE);
        if (!visible) return;
        v.setContentDescription(R.id.portrait_switch, ctx.getString(R.string.portrait_w_switch));
        Intent pick = new Intent(ctx, PersonaWidgetConfigure.class)
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        v.setOnClickPendingIntent(R.id.portrait_switch, PendingIntent.getActivity(ctx, 0x9000 + widgetId, pick,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
    }

    // Кнопка «Поверх приложений» (левый нижний угол)
    private static void outOnTap(Context ctx, RemoteViews v, int widgetId, String name) {
        v.setViewVisibility(R.id.portrait_out, View.VISIBLE);
        v.setContentDescription(R.id.portrait_out, ctx.getString(R.string.portrait_w_out, name));
        Intent out = new Intent(ctx, PortraitWidget.class)
                .setAction(ACTION_OUT)
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId);
        v.setOnClickPendingIntent(R.id.portrait_out, PendingIntent.getBroadcast(ctx, 0x8000 + widgetId,
                out, PendingIntent.FLAG_UPDATE_CURRENT | boundsFlag()));
    }

    /**
     * «Где нажали» (sourceBounds) лаунчер дописывает в интент нажатия, а в
     * неизменяемый (FLAG_IMMUTABLE) Android ничего не дописывает — у кнопок
     * выхода и возврата интент изменяемый: явный, только этому виджету.
     */
    private static int boundsFlag() {
        return Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0;
    }

    private static void petOnTap(Context ctx, RemoteViews v, int widgetId, String name) {
        v.setContentDescription(R.id.portrait_root, ctx.getString(R.string.portrait_w_pet, name));
        Intent petIntent = new Intent(ctx, PortraitWidget.class)
                .setAction(ACTION_PET)
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId);
        // Изменяемый: лаунчер допишет, где виджет на экране (выход поверх — с его места)
        v.setOnClickPendingIntent(R.id.portrait_root, PendingIntent.getBroadcast(ctx, 0x6000 + widgetId,
                petIntent, PendingIntent.FLAG_UPDATE_CURRENT | boundsFlag()));
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
