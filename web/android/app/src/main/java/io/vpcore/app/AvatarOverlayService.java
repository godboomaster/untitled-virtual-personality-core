package io.vpcore.app;

import android.animation.Animator;
import android.animation.AnimatorListenerAdapter;
import android.animation.ValueAnimator;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.KeyguardManager;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.ContextWrapper;
import android.content.IntentFilter;
import android.content.pm.ApplicationInfo;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.content.res.Configuration;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.hardware.input.InputManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.provider.Settings;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Gravity;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.view.animation.DecelerateInterpolator;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;
import androidx.core.content.ContextCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Аватар поверх приложений: голова выбранной персоны (AvatarHeadView) в
 * маленьком окне TYPE_APPLICATION_OVERLAY — над любым приложением, как
 * портрет на рабочем столе компьютера. Право «Поверх других приложений»
 * человек даёт сам в настройках Android.
 *
 * Что умеет: моргает, изредка отводит взгляд, смотрит туда, где вы нажали
 * (если включена служба спец. возможностей AvatarGazeService — без неё
 * Android не говорит приложению, куда нажимали в других приложениях), тап —
 * погладить (ядру — тычок pet, как у виджета), перетащить — переставить,
 * долгое нажатие — чат с персоной. Спит — как виджет: ядро говорит, что
 * спит в «Комнате», или ночь по часам телефона.
 *
 * Энергия: окно маленькое, кадр меняется только на моргании, взгляде и
 * касании (см. AvatarHeadView); экран погас — ни одного таймера. Служба
 * переднего плана — только чтобы Android не убил окно в фоне; уведомление
 * тихое, в нём «Убрать».
 *
 * Размер — щипком двумя пальцами по голове (или ползунком в приложении).
 * «Сквозь касания» (prefs pass; в приложении и кнопкой в уведомлении) —
 * окно FLAG_NOT_TOUCHABLE: нажатия уходят приложению под головой. Пока
 * открыта клавиатура, так же — само (кнопка «Отправить» над клавиатурой
 * часто как раз под головой). Android 12+ пропускает касания сквозь чужое
 * окно, только если оно не плотнее getMaximumObscuringOpacityForTouch
 * (0,8), поэтому голова в этом режиме чуть прозрачная — заодно и видно, что
 * трогать её сейчас бесполезно. Клавиатуру видно по вставкам окна, а со
 * службой «куда вы нажимаете» — и по списку окон (AvatarGazeService).
 *
 * Разговор — как у виджета и портрета на компьютере: лента реплик ядра
 * (PortraitSaidFeed идёт и ради аватара, не только виджета) и окно
 * «Написать» зовут said (через PortraitWidget.said): услышала реплику —
 * кивает и думает; ответ — облачком (AvatarBubble) с губами, а если
 * приложение открыто (ответ и так виден в чате) — только губами. Фоновые
 * сообщения (инициатива, напоминание) — тоже облачком.
 *
 * Долгое нажатие — меню (AvatarMenu): «Написать», «Сменить персону»,
 * «Открыть приложение», «Сквозь касания», «Убрать». Тап — погладить, и на
 * пару секунд в углу карандаш «Написать». Имена персон для меню присылает
 * приложение (setPersonas); нет их — имена виджета или id.
 *
 * С виджета «Живой портрет»: кнопка «Поверх приложений» — голова
 * появляется на месте головы в виджете и плавно принимает свой размер
 * (startFromWidget); виджет этой персоны, пока она здесь, — стекло
 * «Вернуть» (owns). «На виджет» в меню или «Вернуть» на виджете — окно
 * исчезает сразу, виджет снова её показывает (toWidget). Где голова в
 * виджете — prefs wr.<персона> (для выхода).
 *
 * Включено ли и для кого — prefs vpc_avatar_overlay (on, persona, name,
 * size — сторона окна в dp, x/y — где поставили, pass — сквозь касания). Включает и выключает
 * плагин AvatarOverlay из «Аватар» → «Поверх приложений»; после перезагрузки
 * и обновления приложения возвращается сам (ReminderBootReceiver → restore).
 */
public class AvatarOverlayService extends Service {

    private static final String TAG = "AvatarOverlay";

    static final String ACTION_STOP = "io.vpcore.app.AVATAR_OVERLAY_STOP";
    static final String ACTION_PASS = "io.vpcore.app.AVATAR_OVERLAY_PASS";
    static final String PREFS = "vpc_avatar_overlay";
    private static final String CHANNEL = "vpc_avatar";
    private static final int NOTE_ID = 0x5A7A;
    static final int SIZE_DP_DEFAULT = 104;
    static final int SIZE_DP_MIN = 64;
    static final int SIZE_DP_MAX = 220;
    // Прозрачность «сквозь касания»: ниже порога Android (0,8) с запасом
    private static final float PASS_ALPHA = 0.75f;
    private static final long PET_POKE_MS = 60_000L;
    // Думает над ответом не дольше; тот же ответ дважды (окно «Написать» и лента) — не говорить
    private static final long THINK_MAX_MS = 120_000L;
    private static final long RECENT_MS = 120_000L;
    // Выход из виджета и возврат на него
    private static final long MOVE_MS = 320;
    // Вышла из виджета: окно стоит поверх его головы в том же кадре, пока
    // лаунчер не сменит виджет стеклом
    private static final long HANDOFF_MS = 350;

    private static volatile AvatarOverlayService instance;
    // Почему окна нет (для карточки в приложении): no_permission, no_sheets, start
    static volatile String lastError = "";

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService net = Executors.newSingleThreadExecutor();
    private WindowManager wm;
    private AvatarHeadView head;
    private WindowManager.LayoutParams lp;
    private String persona = "";
    private int startX;
    private int startY;
    private int startSize;
    private int startCx;
    private int startCy;
    private long petPokedAt;
    // Клавиатура на экране: по вставкам своего окна и по списку окон службы
    private boolean imeByInsets;
    private boolean imeByA11y;
    // Облачко реплики и меню (окна рядом с головой)
    private AvatarBubble bubble;
    private AvatarMenu menu;
    // Разговор: думает ли, над чем, что недавно сказала
    private boolean thinking;
    private String lastHeard = "";
    private long heardAt;
    private final Map<String, Long> recentSaid = new HashMap<>();
    // Голова у поля «Написать»: где стояла до того (вернуться)
    private boolean docked;
    private int dockFromX;
    private int dockFromY;
    // Плавный переезд окна (из виджета и обратно)
    private ValueAnimator moving;
    private final Runnable stopThink = () -> setThinking(false);
    private final Runnable quietHead = () -> {
        if (head != null) head.speak(false);
    };

    private final BroadcastReceiver screen = new BroadcastReceiver() {
        @Override
        public void onReceive(Context c, Intent i) {
            if (head == null) return;
            String a = i.getAction();
            if (Intent.ACTION_SCREEN_OFF.equals(a)) {
                head.pause();
                if (bubble != null) bubble.pause();
                if (menu != null) menu.dismiss();
            } else if (Intent.ACTION_USER_PRESENT.equals(a) || unlocked(c)) {
                // Поверх экрана блокировки окно не видно — оживает после разблокировки
                head.resume();
                if (bubble != null) bubble.resume();
            }
        }
    };

    static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** Персона сейчас поверх приложений: включено и это она. */
    static boolean owns(Context ctx, String persona) {
        SharedPreferences p = prefs(ctx);
        return p.getBoolean("on", false) && persona != null && persona.equals(p.getString("persona", ""));
    }

    /**
     * Виджет пуст (стекло «Вернуть»): с него и вышла голова — пока она
     * поверх, это её место, какую персону ни выбрали в меню (fromWidget); или
     * на нём та же персона, что поверх (одна персона — не в двух местах).
     */
    static boolean awayFor(Context ctx, int widgetId, String widgetPersona) {
        SharedPreferences p = prefs(ctx);
        if (!p.getBoolean("on", false)) return false;
        return p.getInt("fromWidget", -1) == widgetId || owns(ctx, widgetPersona);
    }

    /** Виджетам этой персоны молчать: она поверх или вышла с виджета, где была. */
    static boolean silences(Context ctx, String persona) {
        if (persona == null) return false;
        if (owns(ctx, persona)) return true;
        SharedPreferences p = prefs(ctx);
        int from = p.getInt("fromWidget", -1);
        return p.getBoolean("on", false) && from != -1
                && persona.equals(PortraitWidget.prefs(ctx).getString("w." + from, null));
    }

    /** Включить из приложения («Аватар» → «Поверх»): не с виджета — его место ни за кем. */
    static void startFromApp(Context ctx, String persona, String name, int sizeDp) {
        if (!prefs(ctx).getBoolean("on", false)) prefs(ctx).edit().remove("fromWidget").apply();
        start(ctx, persona, name, sizeDp);
        PortraitWidget.renderAll(ctx);
    }

    /**
     * Кнопка на виджете: персона выходит на экран. head — где её голова в
     * виджете (px экрана; null — не знаем: просто на своё место).
     */
    static void startFromWidget(Context ctx, int widgetId, String persona, String name, Rect head) {
        SharedPreferences p = prefs(ctx);
        SharedPreferences.Editor e = p.edit().putInt("fromWidget", widgetId);
        if (head != null && !head.isEmpty()) {
            e.putString("wr." + persona, head.flattenToString()).putString("from", head.flattenToString());
        }
        e.apply();
        start(ctx, persona, name, p.getInt("size", SIZE_DP_DEFAULT));
        PortraitWidget.renderAll(ctx);
    }

    /**
     * «Вернуть» на пустом виджете: персона, что сейчас поверх (её могли
     * сменить в меню), возвращается на этот виджет.
     */
    static void returnToWidget(Context ctx, int widgetId) {
        SharedPreferences p = prefs(ctx);
        // Вернуть — на тот виджет, где нажали
        if (p.getBoolean("on", false)) p.edit().putInt("fromWidget", widgetId).apply();
        AvatarOverlayService s = instance;
        if (s != null && s.head != null) {
            s.main.post(s::toWidget);
            return;
        }
        // Окна нет (убито) — персона из настроек снова на виджете
        settleWidget(ctx, p.getString("persona", ""), p.getString("name", ""));
        p.edit().putBoolean("on", false).apply();
        if (s != null) s.main.post(s::stopSelf);
        PortraitWidget.renderAll(ctx);
    }

    /**
     * Голова ушла на виджет, с которого вышла: в меню сменили персону — на
     * виджете теперь она. Место виджета больше ни за кем.
     */
    private static void settleWidget(Context ctx, String persona, String name) {
        SharedPreferences p = prefs(ctx);
        int from = p.getInt("fromWidget", -1);
        p.edit().remove("fromWidget").apply();
        if (from == -1 || persona == null || persona.isEmpty()) return;
        if (!PortraitWidget.hasWidget(ctx, from)) return;
        String was = PortraitWidget.prefs(ctx).getString("w." + from, null);
        if (!persona.equals(was)) PortraitWidget.configure(ctx, from, persona, name == null || name.isEmpty() ? persona : name);
    }

    /** Окно на экране (служба работает и голова показана). */
    static boolean running() {
        AvatarOverlayService s = instance;
        return s != null && s.head != null;
    }

    /** Включить для персоны (name — для уведомления), sizeDp — сторона окна. */
    static void start(Context ctx, String persona, String name, int sizeDp) {
        prefs(ctx).edit()
                .putBoolean("on", true)
                .putString("persona", persona)
                .putString("name", name)
                .putInt("size", Math.max(SIZE_DP_MIN, Math.min(SIZE_DP_MAX, sizeDp)))
                .apply();
        lastError = "";
        ContextCompat.startForegroundService(ctx, new Intent(ctx, AvatarOverlayService.class));
    }

    static void stop(Context ctx) {
        prefs(ctx).edit().putBoolean("on", false).apply();
        AvatarOverlayService s = instance;
        if (s != null) s.main.post(s::stopSelf);
        AvatarGazeService.sync();
    }

    /** «Сквозь касания»: голова не ловит нажатия, они уходят приложению под ней. */
    static void setPassThrough(Context ctx, boolean on) {
        prefs(ctx).edit().putBoolean("pass", on).apply();
        AvatarOverlayService s = instance;
        if (s != null) s.main.post(s::applyTouch);
    }

    /** Клавиатура на экране (голова сейчас сквозная сама). */
    static boolean imeVisible() {
        AvatarOverlayService s = instance;
        return s != null && (s.imeByInsets || s.imeByA11y);
    }

    /** Служба «куда вы нажимаете» увидела, что клавиатура открылась или закрылась. */
    static void imeFromA11y(boolean visible) {
        AvatarOverlayService s = instance;
        if (s == null) return;
        s.main.post(() -> {
            if (s.imeByA11y == visible) return;
            s.imeByA11y = visible;
            s.applyTouch();
        });
    }

    /**
     * Реплика из ленты ядра или окна «Написать» (PortraitWidget.said):
     * heard — пользователь написал, settled — ход кончился, reply — ответ,
     * spoke — фоновое сообщение персоны. Чужая персона — мимо.
     */
    static void said(Context ctx, String persona, String kind, String text) {
        AvatarOverlayService s = instance;
        if (s == null || persona == null) return;
        s.main.post(() -> s.onSaid(persona, kind, text));
    }

    /** На экране голова этой персоны. */
    static boolean shows(String persona) {
        AvatarOverlayService s = instance;
        return s != null && persona != null && persona.equals(s.persona) && s.head != null;
    }

    /**
     * Листы персоны скачаны заново (PortraitWidget.fetch: правили портрет,
     * сменился режим и с ним аватар) — голова на экране этой персоны режет
     * их заново, на том же месте и того же размера.
     */
    static void portraitChanged(String persona) {
        AvatarOverlayService s = instance;
        if (s == null || persona == null) return;
        s.main.post(() -> {
            if (!persona.equals(s.persona) || s.head == null || s.lp == null) return;
            s.show(persona, s.lp.width);
        });
    }

    /** Лента остановилась: конца хода не узнать — не думать. */
    static void saidStopped() {
        AvatarOverlayService s = instance;
        if (s != null) s.main.post(() -> s.setThinking(false));
    }

    /**
     * Окно «Написать» этой персоны открыто, верх его панели — top (px
     * экрана): голова садится к полю справа, вплотную над панелью, как поле
     * у головы на компьютере (и не закрывает клавиатуру).
     */
    static void inputPanel(String persona, int top) {
        AvatarOverlayService s = instance;
        if (s == null || persona == null) return;
        s.main.post(() -> s.dock(persona, top));
    }

    /** Окно «Написать» закрыто — голова обратно, где стояла. */
    static void inputClosed() {
        AvatarOverlayService s = instance;
        if (s != null) s.main.post(s::undock);
    }

    /** Персоны для меню «Сменить персону» (из приложения): JSON [{id, name}]. */
    static void setPersonas(Context ctx, String json) {
        prefs(ctx).edit().putString("personas", json).apply();
    }

    /**
     * Было включено, а окна нет — показать снова: после перезагрузки и
     * обновления (ReminderBootReceiver; MagicOS иногда не доставляет
     * MY_PACKAGE_REPLACED — тогда AvatarGazeService, которую система
     * поднимает сама, и открытие приложения, MainActivity.onResume).
     */
    static void restore(Context ctx) {
        SharedPreferences p = prefs(ctx);
        if (running() || !p.getBoolean("on", false) || !canDraw(ctx)) return;
        try {
            ContextCompat.startForegroundService(ctx, new Intent(ctx, AvatarOverlayService.class));
        } catch (RuntimeException e) {
            Log.w(TAG, "не поднялся после перезагрузки: " + e.getMessage());
        }
    }

    static boolean canDraw(Context ctx) {
        return Build.VERSION.SDK_INT < 23 || Settings.canDrawOverlays(ctx);
    }

    /** Нажали в точке экрана (координаты экрана, px) — от AvatarGazeService. */
    static void lookAt(int x, int y) {
        AvatarOverlayService s = instance;
        if (s == null) return;
        s.main.post(() -> {
            if (s.head != null) s.head.lookAtScreen(x, y);
        });
    }

    @Override
    public void onCreate() {
        super.onCreate();
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        SharedPreferences p = prefs(this);
        boolean stopAsked = intent != null && ACTION_STOP.equals(intent.getAction());
        // startForeground — сразу и всегда: после startForegroundService
        // остановка без него роняет приложение
        final Notification note;
        try {
            note = notification();
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(NOTE_ID, note, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
            } else {
                startForeground(NOTE_ID, note);
            }
        } catch (RuntimeException e) {
            Log.w(TAG, "startForeground не удался", e);
            if (!stopAsked) lastError = "start";
            stopSelf();
            return START_NOT_STICKY;
        }
        if (intent != null && ACTION_PASS.equals(intent.getAction()) && head != null) {
            // Кнопка в уведомлении: переключить «сквозь касания»
            p.edit().putBoolean("pass", !p.getBoolean("pass", false)).apply();
            applyTouch();
            return START_STICKY;
        }
        if (stopAsked) p.edit().putBoolean("on", false).apply();
        if (stopAsked || !p.getBoolean("on", false)) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (!canDraw(this)) {
            lastError = "no_permission";
            p.edit().putBoolean("on", false).apply();
            stopSelf();
            return START_NOT_STICKY;
        }
        instance = this;
        String want = p.getString("persona", "");
        int sizeDp = p.getInt("size", SIZE_DP_DEFAULT);
        int sizePx = Math.round(sizeDp * getResources().getDisplayMetrics().density);
        if (head != null && want.equals(persona) && lp != null && lp.width != sizePx) {
            // Тот же аватар, другой размер (ползунок в приложении) — вокруг центра
            resizeAround(sizePx, lp.x + lp.width / 2, lp.y + lp.height / 2);
            saveSize();
        } else if (head == null || !want.equals(persona) || lp == null) {
            if (!show(want, sizePx)) {
                lastError = "no_sheets";
                p.edit().putBoolean("on", false).apply();
                stopSelf();
                return START_NOT_STICKY;
            }
        }
        lastError = "";
        NotifyStack.shown(this, NOTE_ID, note); // значок — в общую стопку уведомлений
        AvatarGazeService.sync();
        return START_STICKY;
    }

    // Показать голову персоны (заменить прежнюю); листов нет — false
    private boolean show(String who, int sizePx) {
        hide();
        // Клетки атласа режутся сразу под размер окна
        AvatarCells cells = AvatarCells.open(this, who, sizePx);
        if (cells == null) return false;
        persona = who;
        head = new AvatarHeadView(softwareContext(), who, cells, new AvatarHeadView.Host() {
            @Override
            public void dragStart() {
                startX = lp.x;
                startY = lp.y;
                if (menu != null) menu.dismiss();
            }

            @Override
            public void dragBy(float dx, float dy) {
                place(startX + Math.round(dx), startY + Math.round(dy));
            }

            @Override
            public void dragEnd() {
                // Переставили, пока сидела у поля, — новое место и есть её
                docked = false;
                prefs(AvatarOverlayService.this).edit().putInt("x", lp.x).putInt("y", lp.y).apply();
            }

            @Override
            public void petted() {
                pokeCore();
            }

            @Override
            public void resizeStart() {
                startSize = lp.width;
                startCx = lp.x + lp.width / 2;
                startCy = lp.y + lp.height / 2;
            }

            @Override
            public void resizeBy(float scale) {
                resizeAround(Math.round(startSize * scale), startCx, startCy);
            }

            @Override
            public void resizeEnd() {
                saveSize();
            }

            @Override
            public void openMenu() {
                if (menu != null) menu.show();
            }

            @Override
            public void openInput() {
                AvatarOverlayService.this.openInput();
            }
        });
        int type = Build.VERSION.SDK_INT >= 26
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_PHONE;
        lp = new WindowManager.LayoutParams(sizePx, sizePx, type,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
        lp.gravity = Gravity.TOP | Gravity.START;
        lp.setTitle("VPC avatar");
        Rect screenRect = screen();
        SharedPreferences p = prefs(this);
        float dp = getResources().getDisplayMetrics().density;
        // Где стояла; впервые — справа внизу, над панелью навигации
        lp.x = p.getInt("x", screenRect.width() - sizePx - Math.round(12 * dp));
        lp.y = p.getInt("y", screenRect.height() - sizePx - Math.round(160 * dp));
        // Вышла из виджета — сначала там, где была её голова, и того же размера
        Rect from = Rect.unflattenFromString(p.getString("from", ""));
        p.edit().remove("from").apply();
        if (from != null && !from.isEmpty()) {
            lp.width = from.width();
            lp.height = from.width();
            lp.x = from.left;
            lp.y = from.top;
        }
        clampInto(screenRect);
        boolean pass = p.getBoolean("pass", false);
        if (pass) {
            lp.flags |= WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE;
            lp.alpha = passAlpha();
        }
        // Выходит из виджета: встаёт поверх его головы в том же кадре, пока
        // лаунчер не сменит виджет стеклом (иначе на миг две головы)
        if (from != null && !from.isEmpty()) head.freezeForWidget();
        if (Build.VERSION.SDK_INT >= 30) {
            // Клавиатура: её вставки приходят и окну поверх (проверено на Honor)
            head.setOnApplyWindowInsetsListener((v, ins) -> {
                boolean ime = ins.isVisible(WindowInsets.Type.ime());
                if (ime != imeByInsets) {
                    imeByInsets = ime;
                    Log.i(TAG, "клавиатура (вставки окна): " + ime);
                    main.post(this::applyTouch);
                }
                return ins;
            });
        }
        try {
            wm.addView(head, lp);
        } catch (RuntimeException e) {
            Log.w(TAG, "окно не добавилось", e);
            head.release();
            head = null;
            return false;
        }
        Context sw = softwareContext();
        bubble = new AvatarBubble(sw, wm, new AvatarBubble.Host() {
            @Override
            public Rect headRect() {
                return AvatarOverlayService.this.headRect();
            }

            @Override
            public void talking(boolean on) {
                main.removeCallbacks(quietHead);
                if (head != null) head.speak(on);
            }

            @Override
            public void tapped() {
                openApp(persona);
            }
        });
        if (pass) bubble.passThrough(true, passAlpha());
        menu = new AvatarMenu(sw, wm, new AvatarMenu.Host() {
            @Override
            public Rect headRect() {
                return AvatarOverlayService.this.headRect();
            }

            @Override
            public String personaName() {
                return prefs(AvatarOverlayService.this).getString("name", persona);
            }

            @Override
            public List<String[]> personas() {
                return personaList();
            }

            @Override
            public String currentPersona() {
                return persona;
            }

            @Override
            public boolean passThrough() {
                return prefs(AvatarOverlayService.this).getBoolean("pass", false);
            }

            @Override
            public void write() {
                openInput();
            }

            @Override
            public void switchTo(String id, String name) {
                int dpNow = Math.round(lp.width / getResources().getDisplayMetrics().density);
                start(AvatarOverlayService.this, id, name, dpNow);
            }

            @Override
            public void openApp() {
                AvatarOverlayService.this.openApp(null);
            }

            @Override
            public void togglePass() {
                SharedPreferences pp = prefs(AvatarOverlayService.this);
                pp.edit().putBoolean("pass", !pp.getBoolean("pass", false)).apply();
                applyTouch();
            }

            @Override
            public void hideAvatar() {
                stop(AvatarOverlayService.this);
            }

            @Override
            public boolean hasWidget() {
                return hasWidgetToReturn();
            }

            @Override
            public void toWidget() {
                AvatarOverlayService.this.toWidget();
            }
        });
        // Лента реплик ядра — теперь и ради аватара
        PortraitSaidFeed.update(this);
        // Виджет этой персоны — пуст, прежней (если сменили) — снова с ней
        PortraitWidget.renderAll(this);
        if (from != null && !from.isEmpty()) {
            // Виджет сменился стеклом — ожить и плавно к своему размеру, середина на месте
            main.postDelayed(() -> {
                if (head == null) return;
                head.unfreeze();
                if (lp.width == sizePx) return;
                Rect to = new Rect(0, 0, sizePx, sizePx);
                to.offsetTo(lp.x + lp.width / 2 - sizePx / 2, lp.y + lp.height / 2 - sizePx / 2);
                animateTo(to, () -> {
                    // Целиком на экране, размер и место — запомнить
                    place(lp.x, lp.y);
                    saveSize();
                });
            }, HANDOFF_MS);
        }
        IntentFilter f = new IntentFilter();
        f.addAction(Intent.ACTION_SCREEN_OFF);
        f.addAction(Intent.ACTION_SCREEN_ON);
        f.addAction(Intent.ACTION_USER_PRESENT);
        ContextCompat.registerReceiver(this, screen, f, ContextCompat.RECEIVER_NOT_EXPORTED);
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        if (pm == null || pm.isInteractive()) head.resume();
        return true;
    }

    /**
     * Контекст окна без аппаратного ускорения. Окно без родителя
     * (WindowManagerGlobal.addView) включает его по флагу приложения, а с ним
     * процесс поднимает контекст GPU и кэши отрисовки — замер на Honor:
     * +26 МБ PSS ради картинки, которая меняется раз в несколько секунд.
     * Одна клетка 338×338 процессором — доли миллисекунды.
     */
    private Context softwareContext() {
        return new ContextWrapper(this) {
            @Override
            public ApplicationInfo getApplicationInfo() {
                ApplicationInfo ai = new ApplicationInfo(super.getApplicationInfo());
                ai.flags &= ~ApplicationInfo.FLAG_HARDWARE_ACCELERATED;
                return ai;
            }
        };
    }

    // Ловит ли голова касания: «сквозь касания» включено или открыта клавиатура — нет
    private void applyTouch() {
        if (head == null || lp == null) return;
        boolean pass = prefs(this).getBoolean("pass", false) || imeByInsets || imeByA11y;
        int flags = pass
                ? lp.flags | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                : lp.flags & ~WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE;
        float alpha = pass ? passAlpha() : 1f;
        if (bubble != null) bubble.passThrough(pass, passAlpha());
        if (flags != lp.flags || alpha != lp.alpha) {
            lp.flags = flags;
            lp.alpha = alpha;
            try {
                wm.updateViewLayout(head, lp);
            } catch (RuntimeException ignored) {
                // окно уже убрали
            }
        }
        // Подпись кнопки в уведомлении — по ручному режиму
        try {
            Notification n = notification();
            NotificationManagerCompat.from(this).notify(NOTE_ID, n);
            NotifyStack.shown(this, NOTE_ID, n);
        } catch (SecurityException ignored) {
            // нет разрешения на уведомления — кнопка только в приложении
        }
    }

    private float passAlpha() {
        float max = 0.8f;
        if (Build.VERSION.SDK_INT >= 31) {
            InputManager im = getSystemService(InputManager.class);
            if (im != null) max = im.getMaximumObscuringOpacityForTouch();
        }
        return Math.max(0.3f, Math.min(PASS_ALPHA, max - 0.05f));
    }

    // Сторона окна — px, центр остаётся на месте; в пределах SIZE_DP_* и экрана
    private void resizeAround(int px, int cx, int cy) {
        if (head == null) return;
        float dp = getResources().getDisplayMetrics().density;
        Rect sc = screen();
        int min = Math.round(SIZE_DP_MIN * dp);
        int max = Math.min(Math.round(SIZE_DP_MAX * dp), Math.min(sc.width(), sc.height()));
        px = Math.max(min, Math.min(max, px));
        lp.width = px;
        lp.height = px;
        lp.x = cx - px / 2;
        lp.y = cy - px / 2;
        clampInto(sc);
        try {
            wm.updateViewLayout(head, lp);
        } catch (RuntimeException ignored) {
            // окно уже убрали
        }
        moved();
    }

    // Размер и место запомнить, клетки — под новый размер
    private void saveSize() {
        if (head == null) return;
        float dp = getResources().getDisplayMetrics().density;
        prefs(this).edit()
                .putInt("size", Math.round(lp.width / dp))
                .putInt("x", lp.x)
                .putInt("y", lp.y)
                .apply();
        head.cellSize(lp.width);
    }

    private void hide() {
        if (moving != null) {
            moving.cancel();
            moving = null;
        }
        if (head == null) return;
        if (bubble != null) bubble.hide();
        bubble = null;
        if (menu != null) menu.dismiss();
        menu = null;
        main.removeCallbacks(stopThink);
        main.removeCallbacks(quietHead);
        thinking = false;
        docked = false;
        recentSaid.clear();
        try {
            unregisterReceiver(screen);
        } catch (RuntimeException ignored) {
            // не регистрировали
        }
        try {
            wm.removeView(head);
        } catch (RuntimeException ignored) {
            // окна уже нет
        }
        head.release();
        head = null;
        imeByInsets = false;
    }

    private void place(int x, int y) {
        if (head == null) return;
        lp.x = x;
        lp.y = y;
        clampInto(screen());
        try {
            wm.updateViewLayout(head, lp);
        } catch (RuntimeException ignored) {
            // окно уже убрали
        }
        moved();
    }

    // Голова сдвинулась или сменила размер: облачко — за ней, меню — закрыть
    private void moved() {
        if (bubble != null) bubble.follow();
        if (menu != null) menu.dismiss();
    }

    private Rect headRect() {
        return lp == null ? new Rect() : new Rect(lp.x, lp.y, lp.x + lp.width, lp.y + lp.height);
    }

    // ── Разговор ──────────────────────────────────────────────────────

    private void onSaid(String who, String kind, String text) {
        if (head == null || !who.equals(persona)) return;
        String t = text == null ? "" : text.trim();
        long now = SystemClock.uptimeMillis();
        switch (kind) {
            case "heard":
                // Окно «Написать» уже сказало об этой реплике — лента о ней же не начинает заново
                if (thinking && t.equals(lastHeard) && now - heardAt < THINK_MAX_MS) return;
                lastHeard = t;
                heardAt = now;
                recentSaid.clear();
                if (bubble != null) bubble.hide();
                head.wake();
                head.nod();
                setThinking(true);
                break;
            case "settled":
                setThinking(false);
                break;
            case "reply":
            case "spoke": {
                if ("reply".equals(kind)) setThinking(false);
                if (t.isEmpty()) return;
                Long at = recentSaid.get(t);
                if (at != null && now - at < RECENT_MS) return;
                recentSaid.put(t, now);
                if (BackgroundInboxService.appVisible || bubble == null) {
                    // Приложение открыто — ответ и так в чате: только губы, коротко
                    head.speak(true);
                    main.removeCallbacks(quietHead);
                    main.postDelayed(quietHead, Math.max(800, Math.min(4_000, 60L * t.length())));
                } else {
                    bubble.say(t);
                }
                break;
            }
            default:
                break;
        }
    }

    private void setThinking(boolean on) {
        thinking = on;
        main.removeCallbacks(stopThink);
        if (on) main.postDelayed(stopThink, THINK_MAX_MS);
        if (head != null) head.think(on);
    }

    private void dock(String who, int top) {
        if (head == null || !who.equals(persona) || top <= 0) return;
        if (!docked) {
            docked = true;
            dockFromX = lp.x;
            dockFromY = lp.y;
        }
        float dp = getResources().getDisplayMetrics().density;
        Rect sc = screen();
        // Справа, низ головы чуть заходит на панель — сидит на ней
        place(sc.width() - lp.width - Math.round(10 * dp), top - lp.height + Math.round(lp.height * 0.06f));
    }

    /**
     * «На виджет» (меню головы, стекло «Вернуть» на виджете): окно исчезает
     * сразу, и виджет снова показывает персону — без переезда и подмены
     * (две головы внахлёст при подмене были заметны).
     */
    private void toWidget() {
        backOnWidget();
        hide();
        stopSelf();
    }

    // Есть куда вернуться: виджет, с которого вышла, или виджет этой персоны
    private boolean hasWidgetToReturn() {
        int from = prefs(this).getInt("fromWidget", -1);
        return (from != -1 && PortraitWidget.hasWidget(this, from)) || PortraitWidget.hasWidgetFor(this, persona);
    }

    // Персона снова на виджете: выключено; вышла с виджета — на нём теперь та,
    // что была поверх; виджет — перерисовать
    private void backOnWidget() {
        prefs(this).edit().putBoolean("on", false).apply();
        settleWidget(this, persona, prefs(this).getString("name", persona));
        PortraitWidget.renderAll(this);
    }

    // Плавно: окно головы — в прямоугольник to (размер и место), потом done
    private void animateTo(Rect to, Runnable done) {
        if (head == null || lp == null) return;
        if (moving != null) moving.cancel();
        final Rect from = headRect();
        ValueAnimator a = ValueAnimator.ofFloat(0f, 1f);
        a.setDuration(MOVE_MS);
        a.setInterpolator(new DecelerateInterpolator());
        a.addUpdateListener(v -> {
            if (head == null) return;
            float t = (float) v.getAnimatedValue();
            int side = Math.round(from.width() + (to.width() - from.width()) * t);
            lp.width = side;
            lp.height = side;
            lp.x = Math.round(from.left + (to.left - from.left) * t);
            lp.y = Math.round(from.top + (to.top - from.top) * t);
            try {
                wm.updateViewLayout(head, lp);
            } catch (RuntimeException ignored) {
                // окно уже убрали
            }
            if (bubble != null) bubble.follow();
        });
        a.addListener(new AnimatorListenerAdapter() {
            private boolean cancelled;

            @Override
            public void onAnimationCancel(Animator animation) {
                cancelled = true;
            }

            @Override
            public void onAnimationEnd(Animator animation) {
                if (moving == a) moving = null;
                if (!cancelled && done != null) done.run();
            }
        });
        moving = a;
        a.start();
    }

    private void undock() {
        if (!docked || head == null) return;
        docked = false;
        place(dockFromX, dockFromY);
    }

    // ── Действия меню ─────────────────────────────────────────────────

    /** Окно «Написать» (то же, что у виджета): поле над клавиатурой. */
    private void openInput() {
        Intent i = new Intent(this, PortraitInputActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_NO_ANIMATION)
                .putExtra(PortraitInputActivity.EXTRA_PERSONA, persona)
                .putExtra(PortraitInputActivity.EXTRA_NAME, prefs(this).getString("name", persona));
        try {
            startActivity(i);
        } catch (RuntimeException e) {
            Log.i(TAG, "«Написать» не открылось: " + e.getMessage());
        }
    }

    /** Приложение; who — сразу чат этой персоны (null — где было). */
    private void openApp(String who) {
        Intent i = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        if (who != null) i.putExtra(BackgroundInboxService.EXTRA_PERSONA, who);
        try {
            startActivity(i);
        } catch (RuntimeException e) {
            Log.i(TAG, "приложение не открылось: " + e.getMessage());
        }
    }

    /**
     * Персоны для меню: те, у кого на телефоне есть листы головы. Имена — из
     * приложения (setPersonas); не присылало — с диска, имена виджета или id.
     */
    private List<String[]> personaList() {
        List<String[]> out = new ArrayList<>();
        try {
            JSONArray arr = new JSONArray(prefs(this).getString("personas", "[]"));
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.optJSONObject(i);
                if (o == null) continue;
                String id = o.optString("id", "");
                if (id.isEmpty() || AvatarCells.dirOf(this, id) == null) continue;
                out.add(new String[]{id, o.optString("name", id)});
            }
        } catch (Exception ignored) {
            // испорченный список — как будто его нет
        }
        if (!out.isEmpty()) return out;
        SharedPreferences names = PortraitWidget.prefs(this);
        java.util.Set<String> seen = new java.util.LinkedHashSet<>();
        for (String root : new String[]{"portrait_widget", "portrait_head"}) {
            File[] dirs = new File(getFilesDir(), root).listFiles();
            if (dirs == null) continue;
            for (File d : dirs) {
                if (new File(d, "A.webp").isFile()) seen.add(d.getName());
            }
        }
        for (String id : seen) out.add(new String[]{id, names.getString("n." + id, id)});
        return out;
    }

    private void clampInto(Rect s) {
        lp.x = Math.max(0, Math.min(s.width() - lp.width, lp.x));
        lp.y = Math.max(0, Math.min(s.height() - lp.height, lp.y));
    }

    private Rect screen() {
        if (Build.VERSION.SDK_INT >= 30) return wm.getCurrentWindowMetrics().getBounds();
        DisplayMetrics m = new DisplayMetrics();
        //noinspection deprecation
        wm.getDefaultDisplay().getRealMetrics(m);
        return new Rect(0, 0, m.widthPixels, m.heightPixels);
    }

    @Override
    public void onConfigurationChanged(Configuration c) {
        super.onConfigurationChanged(c);
        // Повернули экран — голова остаётся на экране
        if (head != null) place(lp.x, lp.y);
    }

    // Погладили — ядру тычок pet, как виджет (персона узнает, если включены
    // «тычки»), не чаще раза в минуту
    private void pokeCore() {
        long now = System.currentTimeMillis();
        if (now - petPokedAt < PET_POKE_MS || !RoomWidget.serviceEnabled(this)) return;
        petPokedAt = now;
        final String who = persona;
        final Context app = getApplicationContext();
        net.execute(() -> {
            try {
                JSONObject body = new JSONObject().put("chat_id", "auto").put("kind", "pet");
                RoomWidget.request(app, "POST", "/api/personas/" + RoomWidget.enc(who) + "/room/poke",
                        body.toString());
            } catch (Exception e) {
                Log.i(TAG, "погладили " + who + ", ядру не сказали: " + e.getMessage());
            }
        });
    }

    private static boolean unlocked(Context c) {
        KeyguardManager km = (KeyguardManager) c.getSystemService(KEYGUARD_SERVICE);
        return km == null || !km.isKeyguardLocked();
    }

    private Notification notification() {
        SharedPreferences p = prefs(this);
        String name = p.getString("name", "");
        boolean pass = p.getBoolean("pass", false);
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26 && nm != null && nm.getNotificationChannel(CHANNEL) == null) {
            NotificationChannel ch = new NotificationChannel(CHANNEL,
                    getString(R.string.avatar_overlay_channel), NotificationManager.IMPORTANCE_MIN);
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
        }
        Intent open = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent openPi = PendingIntent.getActivity(this, 0x5A7A, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stopPi = PendingIntent.getService(this, 0x5A7B,
                new Intent(this, AvatarOverlayService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent passPi = PendingIntent.getService(this, 0x5A7C,
                new Intent(this, AvatarOverlayService.class).setAction(ACTION_PASS),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        String title = name == null || name.isEmpty()
                ? getString(R.string.avatar_overlay_title_any)
                : getString(R.string.avatar_overlay_title, name);
        return new NotificationCompat.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_stat_vpc)
                .setContentTitle(title)
                .setContentText(getString(pass ? R.string.avatar_overlay_text_pass : R.string.avatar_overlay_text))
                .setContentIntent(openPi)
                .addAction(0, getString(pass ? R.string.avatar_overlay_catch : R.string.avatar_overlay_pass), passPi)
                .addAction(0, getString(R.string.avatar_overlay_hide), stopPi)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setShowWhen(false)
                .setSilent(true)
                .setPriority(NotificationCompat.PRIORITY_MIN)
                .setCategory(NotificationCompat.CATEGORY_SERVICE)
                .setGroup(NotifyStack.GROUP) // в общую стопку уведомлений приложения
                .build();
    }

    @Override
    public void onDestroy() {
        hide();
        NotifyStack.gone(this, null, NOTE_ID); // значок службы уходит вместе с ней
        // Убрали (уведомление, приложение) — персона уходит на виджет, с которого вышла
        if (!prefs(this).getBoolean("on", false)) settleWidget(this, persona, prefs(this).getString("name", persona));
        if (instance == this) instance = null;
        net.shutdown();
        AvatarGazeService.sync();
        PortraitSaidFeed.update(this);
        // Персона снова на своём виджете (если он есть)
        PortraitWidget.renderAll(this);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
