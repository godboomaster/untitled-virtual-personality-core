package io.vpcore.app;

import android.accessibilityservice.AccessibilityService;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.SystemClock;
import android.provider.Settings;
import android.text.TextUtils;
import android.view.accessibility.AccessibilityEvent;

/* Управление экраном телефона (способ 2): служба специальных возможностей.
   Включает её только человек сам: «Настройки» → «Спец. возможности» →
   «Virtual Persona: управление экраном» (Android не даёт приложению сделать
   это за него). Пока служба включена, телефон — устройство ядра
   (app/core/devices): PhoneControlLink держит долгий опрос ядра и исполняет
   команды screen.* через PhoneScreen. Что нажать, решает ядро
   (app/features/phone_control.py) — здесь только исполнение и снимок экрана.

   Служба запущена системой и живёт, пока включена: ей можно открывать
   приложения из фона (запрет запуска активностей из фона её не касается) и
   ходить в сеть, когда приложение закрыто. */
public class PhoneControlService extends AccessibilityService {

    private static volatile PhoneControlService instance;

    // uptimeMillis последнего события окна — по нему settle() ждёт, пока экран
    // перестанет меняться после нажатия
    private volatile long lastEventAt;
    private PhoneControlLink link;

    /** Служба сейчас работает (включена и подключена системой). */
    static PhoneControlService get() {
        return instance;
    }

    /** Служба отмечена в настройках спец. возможностей (может ещё не подняться). */
    static boolean enabled(Context ctx) {
        String list = Settings.Secure.getString(ctx.getContentResolver(),
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        if (TextUtils.isEmpty(list)) return false;
        ComponentName me = new ComponentName(ctx, PhoneControlService.class);
        TextUtils.SimpleStringSplitter split = new TextUtils.SimpleStringSplitter(':');
        split.setString(list);
        for (String s : split) {
            ComponentName c = ComponentName.unflattenFromString(s);
            if (me.equals(c)) return true;
        }
        return false;
    }

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
        lastEventAt = SystemClock.uptimeMillis();
        if (link == null) {
            link = new PhoneControlLink(this);
            link.start();
        }
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        lastEventAt = SystemClock.uptimeMillis();
    }

    @Override
    public void onInterrupt() {
        // Обратной связи (озвучки, вибрации) у службы нет — прерывать нечего
    }

    @Override
    public boolean onUnbind(Intent intent) {
        shutdown();
        return super.onUnbind(intent);
    }

    @Override
    public void onDestroy() {
        shutdown();
        super.onDestroy();
    }

    private void shutdown() {
        if (instance == this) instance = null;
        if (link != null) {
            link.stop();
            link = null;
        }
    }

    /** Ждём, пока экран успокоится после действия: не меньше minMs, потом
     * quietMs без событий окна подряд, но всего не дольше maxMs. */
    void settle(long minMs, long quietMs, long maxMs) {
        long start = SystemClock.uptimeMillis();
        SystemClock.sleep(minMs);
        while (true) {
            long now = SystemClock.uptimeMillis();
            if (now - start >= maxMs || now - lastEventAt >= quietMs) return;
            SystemClock.sleep(Math.min(80, Math.max(10, quietMs - (now - lastEventAt))));
        }
    }
}
