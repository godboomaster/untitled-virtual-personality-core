package io.vpcore.app;

import android.annotation.SuppressLint;
import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.SystemClock;
import android.view.HapticFeedbackConstants;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewConfiguration;

import androidx.core.content.ContextCompat;
import androidx.core.graphics.PathParser;

import java.util.ArrayList;
import java.util.Random;

/**
 * Голова персоны в окне поверх приложений (AvatarOverlayService). Движок —
 * упрощённый перенос web/src/portrait/motion.ts: взгляд — точка от −1 до 1
 * по каждой оси, сглаженная, округляется до клетки сетки атласа (колонка —
 * влево/вправо, строка — вверх/вниз, как LivePortrait); моргание — лист D
 * той же клетки, с живым разбросом.
 *
 * Куда смотрит:
 * - её гладят (тап по голове) или она спит — голова чуть вниз, глаза закрыты;
 * - вы нажали где-то на экране (lookAtScreen — от службы спец. возможностей
 *   AvatarGazeService) — туда, FOLLOW_MS;
 * - иначе — на вас, изредка коротко в сторону.
 *
 * Погладили — как на рабочем столе компьютера (web/src/portrait/usePat.tsx):
 * голова коротко сплющивается и пружинит обратно (SQUASH_MS, опора — низ),
 * а из места касания всплывают сердечки: вспыхивают с перескоком размера,
 * плывут вверх, покачиваясь, и гаснут (HEART_MS); тапаете часто — их
 * несколько, иногда рядом — второе, поменьше.
 *
 * Разговор (AvatarOverlayService.said — лента реплик ядра и окно
 * «Написать»): услышала реплику — кивает (nod) и думает (think: взгляд вверх
 * и в сторону, то в одну, то в другую); отвечает — рот ходит слогами
 * (speak, листы B и C, как useMouth в вебе), пока облачко (AvatarBubble)
 * «проговаривает» часть.
 *
 * Тап — погладить, и на POPUP_MS в углу появляется карандаш: тап по нему —
 * окно «Написать» (на компьютере поле появляется при наведении, у телефона
 * наведения нет). Долгое нажатие — меню (AvatarMenu).
 *
 * Энергия — главное. Кадр перерисовывается, только когда сменилась клетка
 * или лист; шаги взгляда (STEP_MS) идут, только пока голова поворачивается;
 * дошла — стоит до следующего моргания, взгляда в сторону или нажатия.
 * Экран погас — pause(): ни одного таймера. Клетки режутся в своём
 * фоновом потоке, пока новая не готова — на экране прежняя.
 */
@SuppressLint("ViewConstructor")
final class AvatarHeadView extends View {

    interface Host {
        void dragStart();

        void dragBy(float dx, float dy);

        void dragEnd();

        /** Погладили (ядру — тычок pet, не чаще раза в минуту). */
        void petted();

        /** Долгое нажатие — меню у головы. */
        void openMenu();

        /** Карандаш в углу — окно «Написать». */
        void openInput();

        /** Щипок двумя пальцами: начало, общий масштаб от начала, конец. */
        void resizeStart();

        void resizeBy(float scale);

        void resizeEnd();
    }

    private static final long FOLLOW_MS = 2_500;
    private static final long PET_MS = 1_400;
    // Погладили спящую — не спит столько (как виджет: wk.<персона>)
    private static final long WAKE_MS = 5 * 60_000L;
    private static final long SLEEP_CHECK_MS = 60_000;
    // Шаг взгляда в пути и сглаживание (доля пути за кадр 60 Гц, как в вебе)
    private static final long STEP_MS = 33;
    private static final float SMOOTH = 0.22f;
    private static final float SETTLED = 0.02f;
    // Гладят или спит — голова вниз
    private static final float DOWN_Y = 0.35f;
    // Поглаживание: сплющивание (ключевые кадры — usePat.tsx на ПК) и сердечки
    private static final long SQUASH_MS = 320;
    private static final float[] SQUASH_T = {0f, 0.35f, 0.7f, 1f};
    private static final float[] SQUASH_X = {1f, 1.035f, 0.99f, 1f};
    private static final float[] SQUASH_Y = {1f, 0.94f, 1.015f, 1f};
    private static final long HEART_MS = 1100;
    private static final int HEARTS_MAX = 6;
    private static final long FRAME_MS = 16;
    // Сердечко — тот же контур, что на ПК (viewBox 24×22)
    private static final String HEART_PATH = "M12,21 L2.6,11.6 A5.6,5.6 0,0 1,12 3.9 A5.6,5.6 0,0 1,21.4 11.6 Z";
    private static final int[] HEART_COLORS = {0xFFE5484D, 0xFFF06A7A};
    // Карандаш «Написать» после поглаживания — столько
    private static final long PENCIL_MS = 4_000;
    // Кивок (услышала реплику): голова вниз на столько (как NOD_MS в вебе)
    private static final long NOD_MS = 260;
    private static final float NOD_Y = 0.5f;

    private final Context ctx;
    private final String persona;
    private final Host host;
    private final AvatarCells cells;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final HandlerThread cutThread = new HandlerThread("avatar-cells");
    private final Handler cutter;
    private final Random rnd = new Random();
    private final Paint paint = new Paint(Paint.FILTER_BITMAP_FLAG | Paint.ANTI_ALIAS_FLAG);
    private final Paint zPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF dst = new RectF();
    private final Drawable pencil;
    private final Drawable pencilBg;
    private final RectF pencilRect = new RectF();
    private long pencilUntil;
    private boolean downOnPencil;
    private final Path heartPath = PathParser.createPathFromPathData(HEART_PATH);
    private final Paint heartPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final ArrayList<Heart> hearts = new ArrayList<>();
    private long squashFrom = Long.MIN_VALUE;
    // Где коснулись (координаты окна) — оттуда сердечко; -1 — не знаем
    private float touchX = -1;
    private float touchY = -1;

    /** Сердечко: откуда, какое, когда началось. */
    private static final class Heart {
        float x;
        float y;
        float size;
        float rise;
        float sway;
        float phase;
        int color;
        long at;
    }
    private final int slop;

    // Взгляд: где сейчас, с какого времени шаг; куда нажали и когда
    private float gx;
    private float gy;
    private long prevAt;
    private float lookX;
    private float lookY;
    private long lookAt = Long.MIN_VALUE / 2;
    private float glanceX;
    private float glanceY;
    private long glanceUntil;
    private long nextGlance;
    private boolean stepping;

    // Глаза
    private boolean blink;
    private int blinksLeft;
    private long blinkDur;
    private long pettedUntil;
    private boolean asleep;
    private boolean paused = true;

    // Едет на виджет: замерла в том же кадре, что покажет виджет (прямо или спит)
    private boolean frozen;

    // Разговор: кивает до, думает, говорит (рот: 0 закрыт, 1 приоткрыт, 2 открыт)
    private long nodUntil;
    private boolean thinking;
    private boolean speaking;
    private int mouth;

    // Что на экране и что хотим показать (клетка режется — пока прежняя)
    private Bitmap shown;
    private int wantSheet = -1;
    private int wantR = -1;
    private int wantC = -1;

    // Касание: перетащить, погладить, долгое — чат; двумя пальцами — размер.
    // Второй палец может лечь и мимо головы: окно без FLAG_SPLIT_TOUCH, все
    // пальцы жеста — его
    private float downX;
    private float downY;
    private boolean dragging;
    private boolean longFired;
    private boolean multi;
    // Щипок: расстояние между двумя пальцами в начале; масштаб — к нему.
    // Свой расчёт, а не ScaleGestureDetector: тот не начинает щипок, пока
    // пальцы ближе 27 мм, а голова в 104 dp — около 16 мм
    private boolean scaling;
    private float startSpan;

    private final Runnable step = this::step;
    private final Runnable wake = this::kick;
    private final Runnable blinkStart = this::blinkNow;
    private final Runnable blinkOn = this::blinkOn;
    private final Runnable blinkOff = this::blinkOff;
    private final Runnable sleepCheck = this::checkSleep;
    private final Runnable longPress = this::longPressed;
    private final Runnable syllable = this::syllable;
    private final Runnable mouthClose = this::mouthClose;
    private final Runnable pencilGone = this::invalidate;

    AvatarHeadView(Context ctx, String persona, AvatarCells cells, Host host) {
        super(ctx);
        this.ctx = ctx;
        this.persona = persona;
        this.cells = cells;
        this.host = host;
        cutThread.start();
        cutter = new Handler(cutThread.getLooper());
        heartPaint.setStyle(Paint.Style.FILL);
        pencil = ContextCompat.getDrawable(ctx, R.drawable.widget_write);
        pencilBg = ContextCompat.getDrawable(ctx, R.drawable.widget_write_bg);
        slop = ViewConfiguration.get(ctx).getScaledTouchSlop();
        float dp = ctx.getResources().getDisplayMetrics().density;
        zPaint.setColor(0xFF8A8F98);
        zPaint.setTextSize(15 * dp);
        zPaint.setTypeface(Typeface.DEFAULT_BOLD);
        setContentDescription(persona);
        // Первая клетка — сразу, чтобы окно не появилось пустым
        shown = cells.load(AvatarCells.A, cells.rows / 2, cells.cols / 2);
        wantSheet = AvatarCells.A;
        wantR = cells.rows / 2;
        wantC = cells.cols / 2;
        prefetch(AvatarCells.D, wantR, wantC);
    }

    // ── Жизненный цикл ────────────────────────────────────────────────

    /** Экран включился (или окно только что показали). */
    void resume() {
        if (!paused) return;
        paused = false;
        long now = SystemClock.uptimeMillis();
        nextGlance = now + (long) rand(2500, 5000);
        checkSleep();
        scheduleBlink();
        if (speaking) syllable();
        kick();
    }

    /** Экран погас: ни одного таймера, пока его не включат. */
    void pause() {
        paused = true;
        ui.removeCallbacksAndMessages(null);
        stepping = false;
        blink = false;
        mouth = 0;
    }

    /** Окно сменило размер (щипок, размер из приложения): клетки — под новый. */
    void cellSize(int px) {
        if (!cells.resize(px)) return;
        wantSheet = -1;
        updateFrame();
        invalidate();
    }

    void release() {
        pause();
        cutter.removeCallbacksAndMessages(null);
        cutThread.quitSafely();
        cells.close();
    }

    /**
     * Выходит из виджета: замереть в том кадре, что сейчас на виджете
     * (смотрит прямо; спит — голова опущена, глаза закрыты), без моргания,
     * взглядов, губ и сердечек — пока лаунчер не сменит виджет стеклом,
     * голова на экране одна.
     */
    void freezeForWidget() {
        frozen = true;
        ui.removeCallbacks(blinkStart);
        ui.removeCallbacks(blinkOn);
        ui.removeCallbacks(blinkOff);
        ui.removeCallbacks(syllable);
        ui.removeCallbacks(mouthClose);
        ui.removeCallbacks(step);
        ui.removeCallbacks(wake);
        stepping = false;
        blink = false;
        speaking = false;
        thinking = false;
        mouth = 0;
        pettedUntil = 0;
        nodUntil = 0;
        pencilUntil = 0;
        hearts.clear();
        squashFrom = Long.MIN_VALUE;
        asleep = PortraitWidget.sleepsNow(PortraitWidget.prefs(ctx), persona);
        gx = 0;
        gy = asleep ? DOWN_Y : 0;
        // Кадр — сразу, а не фоновой резкой: окно встаёт поверх виджета уже с ним
        int sheet = asleep && cells.has(AvatarCells.D) ? AvatarCells.D : AvatarCells.A;
        int r = Math.max(0, Math.min(cells.rows - 1, Math.round((gy + 1) / 2 * (cells.rows - 1))));
        int c = cells.cols / 2;
        Bitmap b = cells.load(sheet, r, c);
        wantSheet = sheet;
        wantR = r;
        wantC = c;
        if (b != null) shown = b;
        invalidate();
    }

    /** Вышла из виджета и виджет уже сменился стеклом — ожить. */
    void unfreeze() {
        if (!frozen) return;
        frozen = false;
        nextGlance = SystemClock.uptimeMillis() + (long) rand(2500, 5000);
        scheduleBlink();
        kick();
    }

    // ── Разговор ──────────────────────────────────────────────────────

    /** Услышала реплику: короткий кивок. */
    void nod() {
        nodUntil = SystemClock.uptimeMillis() + NOD_MS;
        kick();
    }

    /** Думает над ответом: взгляд вверх и в сторону. */
    void think(boolean on) {
        if (thinking == on) return;
        thinking = on;
        glanceUntil = 0;
        kick();
    }

    /** Говорит: рот ходит слогами (нет листов рта — только взгляд на вас). */
    void speak(boolean on) {
        if (speaking == on) return;
        speaking = on;
        ui.removeCallbacks(syllable);
        ui.removeCallbacks(mouthClose);
        if (on) {
            thinking = false;
            syllable();
        } else {
            mouth = 0;
            updateFrame();
        }
        kick();
    }

    /** Спала — проснуться (ей пишут): как от поглаживания, но без сердечек. */
    void wake() {
        if (!asleep) return;
        PortraitWidget.prefs(ctx).edit().putLong("wk." + persona, System.currentTimeMillis() + WAKE_MS).apply();
        checkSleep();
    }

    // Слог: открыт → прикрыт, иногда пауза между «словами» (useMouth в вебе)
    private void syllable() {
        if (!speaking || paused || frozen) return;
        if (!cells.has(AvatarCells.B) && !cells.has(AvatarCells.C)) return;
        if (rnd.nextFloat() < 0.12f) {
            mouth = 0;
            updateFrame();
            ui.postDelayed(syllable, (long) rand(150, 300));
            return;
        }
        mouth = rnd.nextFloat() < 0.55f ? 2 : 1;
        updateFrame();
        ui.postDelayed(mouthClose, (long) rand(75, 140));
    }

    private void mouthClose() {
        if (!speaking || paused) return;
        mouth = rnd.nextFloat() < 0.3f ? 1 : 0;
        updateFrame();
        ui.postDelayed(syllable, (long) rand(45, 90));
    }

    // ── Куда смотреть ─────────────────────────────────────────────────

    /** Нажали в точке экрана (координаты экрана, px) — посмотреть туда. */
    void lookAtScreen(int sx, int sy) {
        if (paused || getWidth() == 0) return;
        int[] loc = new int[2];
        getLocationOnScreen(loc);
        float cx = loc[0] + getWidth() / 2f;
        float cy = loc[1] + getHeight() * 0.45f;
        // Полэкрана в сторону — уже взгляд до упора (на рабочем столе — 260 px CSS)
        int screenW = getResources().getDisplayMetrics().widthPixels;
        float range = Math.max(screenW * 0.45f, getWidth() * 1.1f);
        lookX = clamp((sx - cx) / range);
        lookY = clamp((sy - cy) / range);
        lookAt = SystemClock.uptimeMillis();
        kick();
    }

    private void kick() {
        if (paused || frozen) return;
        ui.removeCallbacks(wake);
        if (!stepping) {
            stepping = true;
            prevAt = SystemClock.uptimeMillis();
            ui.post(step);
        }
    }

    private void step() {
        if (paused) {
            stepping = false;
            return;
        }
        long now = SystemClock.uptimeMillis();
        float tx = 0;
        float ty = 0;
        long wakeAt = Long.MAX_VALUE;
        boolean pet = now < pettedUntil;
        if (pet || asleep) {
            ty = DOWN_Y;
            if (pet) wakeAt = pettedUntil;
        } else if (now < nodUntil) {
            // Кивок: вниз, на собеседника
            ty = NOD_Y;
            wakeAt = nodUntil;
        } else if (!speaking && now - lookAt < FOLLOW_MS) {
            tx = lookX;
            ty = lookY;
            wakeAt = lookAt + FOLLOW_MS;
        } else if (thinking) {
            // Думает: вверх и в сторону, чаще — в другую
            if (now > glanceUntil) {
                boolean left = glanceX > 0 ? rnd.nextFloat() < 0.7f : rnd.nextFloat() < 0.3f;
                glanceX = left ? -0.55f : 0.55f;
                glanceY = -0.5f;
                glanceUntil = now + (long) rand(1400, 2800);
            }
            tx = glanceX;
            ty = glanceY;
            wakeAt = glanceUntil;
        } else {
            // На вас; изредка — короткий взгляд в сторону (говорит — чаще и ближе)
            if (now > nextGlance) {
                float k = speaking ? 0.3f : 0.45f;
                glanceX = rand(-k, k);
                glanceY = rand(-k * 0.6f, k * 0.6f);
                glanceUntil = now + (long) rand(450, 1200);
                nextGlance = now + (long) (speaking ? rand(2000, 4500) : rand(4000, 9000));
            }
            if (now < glanceUntil) {
                tx = glanceX;
                ty = glanceY;
                wakeAt = glanceUntil;
            } else {
                wakeAt = nextGlance;
            }
        }
        float dt = Math.min(100, now - prevAt);
        prevAt = now;
        float k = (float) (1 - Math.pow(1 - SMOOTH, dt / 16.7));
        gx += (tx - gx) * k;
        gy += (ty - gy) * k;
        boolean settled = Math.abs(tx - gx) < SETTLED && Math.abs(ty - gy) < SETTLED;
        if (settled) {
            gx = tx;
            gy = ty;
        }
        updateFrame();
        if (!settled) {
            ui.postDelayed(step, STEP_MS);
            return;
        }
        stepping = false;
        if (wakeAt != Long.MAX_VALUE) ui.postDelayed(wake, Math.max(16, wakeAt - now + 1));
        // Дошла — моргание этой клетки заранее
        prefetch(AvatarCells.D, wantR, wantC);
    }

    // ── Глаза ─────────────────────────────────────────────────────────

    private void scheduleBlink() {
        ui.removeCallbacks(blinkStart);
        if (paused || asleep || !cells.has(AvatarCells.D)) return;
        double u = rnd.nextDouble();
        float wait = u < 0.12 ? rand(700, 1500) : u < 0.82 ? rand(1800, 4500) : rand(4500, 9000);
        ui.postDelayed(blinkStart, (long) wait);
    }

    private void blinkNow() {
        if (paused || asleep || frozen) return;
        double roll = rnd.nextDouble();
        blinksLeft = roll < 0.22 ? 2 : 1;
        blinkDur = (long) (roll < 0.22 ? rand(80, 120) : roll < 0.28 ? rand(260, 420) : rand(90, 150));
        blinkOn();
    }

    private void blinkOn() {
        blink = true;
        updateFrame();
        ui.postDelayed(blinkOff, blinkDur);
    }

    private void blinkOff() {
        blink = false;
        updateFrame();
        if (--blinksLeft > 0) {
            blinkDur = (long) rand(70, 110);
            ui.postDelayed(blinkOn, (long) rand(120, 220));
        } else {
            scheduleBlink();
        }
    }

    // Спит ли — как виджет: так говорит ядро, а не знает — ночь по часам телефона
    private void checkSleep() {
        ui.removeCallbacks(sleepCheck);
        boolean was = asleep;
        asleep = PortraitWidget.sleepsNow(PortraitWidget.prefs(ctx), persona);
        if (was != asleep) {
            if (asleep) {
                ui.removeCallbacks(blinkStart);
                blink = false;
            } else {
                scheduleBlink();
            }
            kick();
            invalidate();
        }
        if (!paused) ui.postDelayed(sleepCheck, SLEEP_CHECK_MS);
    }

    private void pet(float x, float y) {
        long now = SystemClock.uptimeMillis();
        pettedUntil = now + PET_MS;
        squashFrom = now;
        pencilUntil = now + PENCIL_MS;
        ui.removeCallbacks(pencilGone);
        ui.postDelayed(pencilGone, PENCIL_MS + 10);
        addHearts(x, y, now);
        if (asleep) {
            // Просыпается и не спит WAKE_MS (та же отметка, что у виджета)
            PortraitWidget.prefs(ctx).edit().putLong("wk." + persona, System.currentTimeMillis() + WAKE_MS).apply();
            checkSleep();
        }
        updateFrame();
        invalidate();
        kick();
        host.petted();
    }

    // Сердечко из места касания (нет его — с макушки), иногда второе, поменьше
    private void addHearts(float x, float y, long now) {
        float dp = getResources().getDisplayMetrics().density;
        float w = getWidth();
        float h = getHeight();
        if (x < 0 || y < 0) {
            x = w / 2;
            y = h * 0.3f;
        }
        int n = rnd.nextFloat() < 0.4f ? 2 : 1;
        for (int i = 0; i < n; i++) {
            Heart t = new Heart();
            boolean small = i > 0;
            t.size = (small ? rand(12, 15) : rand(18, 23)) * dp;
            t.x = Math.max(t.size, Math.min(w - t.size,
                    x + (small ? (rnd.nextBoolean() ? 1 : -1) * rand(14, 22) * dp : rand(-5, 5) * dp)));
            // Чуть выше пальца, чтобы его не закрывал; к верху окна — не выше, чем есть место
            t.y = Math.max(t.size * 1.2f, Math.min(h - t.size * 0.6f, y - t.size * 0.7f));
            t.rise = Math.max(t.size * 0.6f, Math.min((small ? 30 : 42) * dp, t.y - t.size * 0.6f));
            t.sway = rand(3, 6) * dp;
            t.phase = rand(0, (float) (Math.PI * 2));
            t.color = HEART_COLORS[rnd.nextInt(HEART_COLORS.length)];
            t.at = now + (small ? (long) rand(90, 180) : 0);
            hearts.add(t);
        }
        while (hearts.size() > HEARTS_MAX) hearts.remove(0);
    }

    // Сплющивание: ease-out на всё время, между ключевыми кадрами — линейно
    // (как Element.animate(SQUASH, {easing: 'ease-out'}) на ПК)
    private static float squashAt(float[] v, float p) {
        float e = 1 - (1 - p) * (1 - p);
        for (int i = 1; i < SQUASH_T.length; i++) {
            if (e <= SQUASH_T[i]) {
                float k = (e - SQUASH_T[i - 1]) / (SQUASH_T[i] - SQUASH_T[i - 1]);
                return v[i - 1] + (v[i] - v[i - 1]) * k;
            }
        }
        return v[v.length - 1];
    }

    // Сердечки поверх головы; true — ещё есть живые (нужен следующий кадр)
    private boolean drawHearts(Canvas canvas, long now) {
        boolean alive = false;
        for (int i = hearts.size() - 1; i >= 0; i--) {
            Heart t = hearts.get(i);
            float p = (now - t.at) / (float) HEART_MS;
            if (p >= 1) {
                hearts.remove(i);
                continue;
            }
            alive = true;
            if (p < 0) continue;
            // Вспышка с перескоком: 0,4 → 1,15 → 1
            float scale = p < 0.22f ? 0.4f + 0.75f * easeOut(p / 0.22f)
                    : p < 0.38f ? 1.15f - 0.15f * ((p - 0.22f) / 0.16f) : 1f;
            float alpha = p < 0.12f ? p / 0.12f : p < 0.6f ? 1f : 1f - (p - 0.6f) / 0.4f;
            float wobble = (float) Math.sin(p * Math.PI * 2 * 0.9f + t.phase);
            float cx = t.x + wobble * t.sway * (1 - 0.3f * p);
            float cy = t.y - easeOut(p) * t.rise;
            heartPaint.setColor(t.color);
            heartPaint.setAlpha(Math.round(255 * Math.max(0, Math.min(1, alpha))));
            canvas.save();
            canvas.translate(cx, cy);
            canvas.rotate(wobble * 9f);
            float k = t.size / 24f * scale;
            canvas.scale(k, k);
            canvas.translate(-12, -12);
            canvas.drawPath(heartPath, heartPaint);
            canvas.restore();
        }
        return alive;
    }

    private static float easeOut(float p) {
        float q = 1 - Math.max(0, Math.min(1, p));
        return 1 - q * q * q;
    }

    // ── Кадр ──────────────────────────────────────────────────────────

    private void updateFrame() {
        boolean closed = blink || asleep || SystemClock.uptimeMillis() < pettedUntil;
        int sheet = AvatarCells.A;
        if (closed && cells.has(AvatarCells.D)) {
            sheet = AvatarCells.D;
        } else if (mouth == 2) {
            sheet = cells.has(AvatarCells.C) ? AvatarCells.C : cells.has(AvatarCells.B) ? AvatarCells.B : AvatarCells.A;
        } else if (mouth == 1) {
            sheet = cells.has(AvatarCells.B) ? AvatarCells.B : cells.has(AvatarCells.C) ? AvatarCells.C : AvatarCells.A;
        }
        int c = Math.max(0, Math.min(cells.cols - 1, Math.round((gx + 1) / 2 * (cells.cols - 1))));
        int r = Math.max(0, Math.min(cells.rows - 1, Math.round((gy + 1) / 2 * (cells.rows - 1))));
        if (sheet == wantSheet && r == wantR && c == wantC) return;
        wantSheet = sheet;
        wantR = r;
        wantC = c;
        Bitmap b = cells.get(sheet, r, c);
        if (b != null) {
            show(b);
            return;
        }
        final int sh = sheet;
        cutter.post(() -> {
            Bitmap cut = cells.load(sh, r, c);
            if (cut == null) return;
            ui.post(() -> {
                if (sh == wantSheet && r == wantR && c == wantC) show(cut);
            });
        });
    }

    private void show(Bitmap b) {
        if (b == shown) return;
        shown = b;
        invalidate();
    }

    private void prefetch(int sheet, int r, int c) {
        if (!cells.has(sheet) || cells.get(sheet, r, c) != null) return;
        cutter.post(() -> cells.load(sheet, r, c));
    }

    @Override
    protected void onDraw(Canvas canvas) {
        Bitmap b = shown;
        if (b == null || b.isRecycled()) return;
        float w = getWidth();
        float h = getHeight();
        long now = SystemClock.uptimeMillis();
        // Погладили — голова сплющивается и пружинит (опора — низ, как на ПК)
        float sp = (now - squashFrom) / (float) SQUASH_MS;
        boolean squashing = sp >= 0 && sp < 1;
        canvas.save();
        if (squashing) canvas.scale(squashAt(SQUASH_X, sp), squashAt(SQUASH_Y, sp), w / 2, h);
        if (!squashing && Math.abs(b.getWidth() - w) <= 1 && b.getHeight() <= h + 1) {
            // Клетка уже под окно (AvatarCells) — копирование без масштаба;
            // пока окно тянут щипком — прежняя клетка с масштабом
            canvas.drawBitmap(b, (w - b.getWidth()) / 2, (h - b.getHeight()) / 2, null);
        } else {
            // Окно тянут щипком (клетка прежнего размера) или голова сплющивается
            float s = Math.min(w / b.getWidth(), h / b.getHeight());
            float bw = b.getWidth() * s;
            float bh = b.getHeight() * s;
            dst.set((w - bw) / 2, (h - bh) / 2, (w + bw) / 2, (h + bh) / 2);
            canvas.drawBitmap(b, null, dst, paint);
        }
        canvas.restore();
        float dp = getResources().getDisplayMetrics().density;
        // «z» спящей; у виджета (подмена окна виджетом) — своя, вторая не нужна
        if (asleep && !frozen && now >= pettedUntil) canvas.drawText("z", w - 18 * dp, 18 * dp, zPaint);
        if (now < pencilUntil && pencil != null && pencilBg != null) {
            // Карандаш «Написать» в правом нижнем углу — как на виджете
            float ps = Math.max(22 * dp, Math.min(34 * dp, w * 0.24f));
            pencilRect.set(w - ps - 2 * dp, h - ps - 2 * dp, w - 2 * dp, h - 2 * dp);
            pencilBg.setBounds(Math.round(pencilRect.left), Math.round(pencilRect.top),
                    Math.round(pencilRect.right), Math.round(pencilRect.bottom));
            pencilBg.draw(canvas);
            int in = Math.round(ps * 0.22f);
            pencil.setBounds(Math.round(pencilRect.left) + in, Math.round(pencilRect.top) + in,
                    Math.round(pencilRect.right) - in, Math.round(pencilRect.bottom) - in);
            pencil.draw(canvas);
        }
        boolean hearting = drawHearts(canvas, now);
        // Кадры — только пока идёт поглаживание; потом окно снова стоит
        if (squashing || hearting) postInvalidateDelayed(FRAME_MS);
    }

    // ── Касания ───────────────────────────────────────────────────────

    @SuppressLint("ClickableViewAccessibility")
    @Override
    public boolean onTouchEvent(MotionEvent e) {
        switch (e.getActionMasked()) {
            case MotionEvent.ACTION_DOWN:
                downX = e.getRawX();
                downY = e.getRawY();
                touchX = e.getX();
                touchY = e.getY();
                // Карандаш виден и нажат (с запасом под палец) — «Написать», не поглаживание
                float pad = 8 * getResources().getDisplayMetrics().density;
                downOnPencil = SystemClock.uptimeMillis() < pencilUntil
                        && touchX >= pencilRect.left - pad && touchY >= pencilRect.top - pad;
                dragging = false;
                longFired = false;
                multi = false;
                scaling = false;
                startSpan = 0;
                ui.postDelayed(longPress, ViewConfiguration.getLongPressTimeout());
                return true;
            case MotionEvent.ACTION_POINTER_DOWN:
                // Второй палец — щипок: ни поглаживания, ни чата, ни перетаскивания
                multi = true;
                ui.removeCallbacks(longPress);
                if (dragging) {
                    dragging = false;
                    host.dragEnd();
                }
                if (!scaling && e.getPointerCount() >= 2) startSpan = span(e);
                return true;
            case MotionEvent.ACTION_POINTER_UP:
                // Палец щипка поднят — размер готов; остальные пальцы до конца жеста ничего не делают
                if (scaling) {
                    scaling = false;
                    host.resizeEnd();
                }
                startSpan = 0;
                return true;
            case MotionEvent.ACTION_MOVE: {
                if (multi) {
                    if (startSpan > 0 && e.getPointerCount() >= 2) {
                        float sp = span(e);
                        if (!scaling && Math.abs(sp - startSpan) > slop) {
                            // Порог пройден: отсюда и считаем, чтобы голова не прыгнула
                            scaling = true;
                            startSpan = sp;
                            host.resizeStart();
                        }
                        if (scaling) host.resizeBy(sp / startSpan);
                    }
                    return true;
                }
                float dx = e.getRawX() - downX;
                float dy = e.getRawY() - downY;
                if (!dragging && !longFired && Math.hypot(dx, dy) > slop) {
                    dragging = true;
                    ui.removeCallbacks(longPress);
                    host.dragStart();
                }
                if (dragging) host.dragBy(dx, dy);
                return true;
            }
            case MotionEvent.ACTION_UP:
                ui.removeCallbacks(longPress);
                if (scaling) {
                    scaling = false;
                    host.resizeEnd();
                } else if (dragging) {
                    host.dragEnd();
                } else if (!longFired && !multi && downOnPencil) {
                    pencilUntil = 0;
                    invalidate();
                    host.openInput();
                } else if (!longFired && !multi) {
                    performClick();
                }
                return true;
            case MotionEvent.ACTION_CANCEL:
                ui.removeCallbacks(longPress);
                if (scaling) {
                    scaling = false;
                    host.resizeEnd();
                } else if (dragging) {
                    host.dragEnd();
                }
                return true;
            default:
                return true;
        }
    }

    // Расстояние между первыми двумя пальцами (в координатах окна: окно
    // двигается под пальцами, но оба сдвигаются одинаково)
    private static float span(MotionEvent e) {
        return (float) Math.hypot(e.getX(0) - e.getX(1), e.getY(0) - e.getY(1));
    }

    private void longPressed() {
        longFired = true;
        performHapticFeedback(HapticFeedbackConstants.LONG_PRESS);
        host.openMenu();
    }

    @Override
    public boolean performClick() {
        super.performClick();
        // Касание — сердечко оттуда; клик без касания (спец. возможности) — с макушки
        pet(touchX, touchY);
        touchX = -1;
        touchY = -1;
        return true;
    }

    private float rand(float a, float b) {
        return a + rnd.nextFloat() * (b - a);
    }

    private static float clamp(float v) {
        return Math.max(-1f, Math.min(1f, v));
    }
}
