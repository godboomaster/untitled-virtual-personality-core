package io.vpcore.app;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.AccessibilityServiceInfo;
import android.content.ComponentName;
import android.content.Context;
import android.graphics.Rect;
import android.os.SystemClock;
import android.provider.Settings;
import android.text.TextUtils;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import java.util.List;

/**
 * «Аватар: куда вы нажимаете» — служба специальных возможностей только для
 * взгляда аватара поверх приложений (AvatarOverlayService). Касаний в
 * чужих приложениях Android обычному приложению не показывает (окно поверх
 * получает «где-то нажали» без координат), а службе спец. возможностей —
 * показывает событие «нажали на элемент». Отсюда берётся только
 * прямоугольник элемента на экране: аватар смотрит в его середину. Текст
 * экрана, названия элементов, что именно нажато — не читаются и никуда не
 * уходят.
 *
 * События: нажатие, долгое нажатие, ввод текста в поле (смотрит, как вы
 * печатаете — не чаще раза в TYPE_MS). Нажатия в пустоту, в играх и на
 * картинках без кнопок события не дают — туда аватар не посмотрит.
 *
 * И ещё одно: смена окон — есть ли среди них клавиатура (тип окна, без
 * содержимого). Пока она открыта, голова пропускает касания сквозь себя
 * (AvatarOverlayService.imeFromA11y). Вставки своего окна говорят о том же,
 * но не во всех прошивках — служба надёжнее.
 *
 * Энергия: пока аватара на экране нет, служба подписана на ноль событий
 * (sync) — система ей ничего не шлёт.
 */
public class AvatarGazeService extends AccessibilityService {

    private static final int EVENTS = AccessibilityEvent.TYPE_VIEW_CLICKED
            | AccessibilityEvent.TYPE_VIEW_LONG_CLICKED
            | AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED
            | AccessibilityEvent.TYPE_WINDOWS_CHANGED;
    private static final long TYPE_MS = 600;

    private static volatile AvatarGazeService instance;
    private long typedAt;

    static boolean running() {
        return instance != null;
    }

    /** Отмечена в настройках спец. возможностей (может ещё не подняться). */
    static boolean enabled(Context ctx) {
        String list = Settings.Secure.getString(ctx.getContentResolver(),
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        if (TextUtils.isEmpty(list)) return false;
        ComponentName me = new ComponentName(ctx, AvatarGazeService.class);
        TextUtils.SimpleStringSplitter split = new TextUtils.SimpleStringSplitter(':');
        split.setString(list);
        for (String s : split) {
            if (me.equals(ComponentName.unflattenFromString(s))) return true;
        }
        return false;
    }

    /** Аватар появился или ушёл — подписаться на события или отписаться от всех. */
    static void sync() {
        AvatarGazeService s = instance;
        if (s != null) s.subscribe(AvatarOverlayService.running());
    }

    private void subscribe(boolean on) {
        AccessibilityServiceInfo info = getServiceInfo();
        if (info == null) return;
        int want = on ? EVENTS : 0;
        // Список окон (клавиатура) — только пока аватар на экране
        int flags = on
                ? info.flags | AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                : info.flags & ~AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS;
        if (info.eventTypes != want || info.flags != flags) {
            info.eventTypes = want;
            info.flags = flags;
            setServiceInfo(info);
        }
        AvatarOverlayService.imeFromA11y(on && imeShown());
    }

    // Клавиатура среди окон на экране
    private boolean imeShown() {
        try {
            List<AccessibilityWindowInfo> windows = getWindows();
            for (AccessibilityWindowInfo w : windows) {
                if (w.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD) return true;
            }
        } catch (RuntimeException ignored) {
            // окна недоступны (флаг ещё не применился)
        }
        return false;
    }

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
        // Система подняла службу (обновление, перезагрузка) — аватар был включён: вернуть
        AvatarOverlayService.restore(this);
        subscribe(AvatarOverlayService.running());
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (!AvatarOverlayService.running()) return;
        int type = event.getEventType();
        if (type == AccessibilityEvent.TYPE_WINDOWS_CHANGED) {
            AvatarOverlayService.imeFromA11y(imeShown());
            return;
        }
        // Своя голова: тап по ней — погладить, смотреть на себя незачем
        CharSequence cls = event.getClassName();
        if (cls != null && AvatarHeadView.class.getName().contentEquals(cls)) return;
        if (type == AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED) {
            long now = SystemClock.uptimeMillis();
            if (now - typedAt < TYPE_MS) return;
            typedAt = now;
        }
        AccessibilityNodeInfo src = event.getSource();
        if (src == null) return;
        Rect r = new Rect();
        src.getBoundsInScreen(r);
        if (r.isEmpty()) return;
        // Большое поле ввода — туда, где пишут (верх), а не в его середину
        int y = type == AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED && r.height() > r.width() / 2
                ? r.top + Math.min(r.height() / 2, r.width() / 4)
                : r.centerY();
        AvatarOverlayService.lookAt(r.centerX(), y);
    }

    @Override
    public void onInterrupt() {
        // обратной связи нет — прерывать нечего
    }

    @Override
    public void onDestroy() {
        if (instance == this) instance = null;
        super.onDestroy();
    }
}
