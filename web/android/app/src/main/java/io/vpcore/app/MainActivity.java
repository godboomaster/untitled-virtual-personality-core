package io.vpcore.app;

import android.content.Intent;
import android.os.Bundle;
import android.view.MotionEvent;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        // Свои плагины регистрируются до super.onCreate — там поднимается мост
        registerPlugin(BackgroundInboxPlugin.class);
        registerPlugin(NativeSpeechPlugin.class);
        registerPlugin(OfflineLlmPlugin.class);
        registerPlugin(PhoneActionsPlugin.class);
        registerPlugin(FileSavePlugin.class);
        registerPlugin(WakeWordPlugin.class);
        registerPlugin(CoreFinderPlugin.class);
        registerPlugin(PhoneServicesPlugin.class);
        registerPlugin(PhoneControlPlugin.class);
        registerPlugin(WebChatPlugin.class);
        registerPlugin(AvatarOverlayPlugin.class);
        super.onCreate(savedInstanceState);
        handleTap(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleTap(intent);
    }

    @Override
    public void onResume() {
        super.onResume();
        // Приложение на экране: фоновая служба уведомлений не показывает,
        // а уже показанные снимаем — их сообщения теперь видно в чате
        BackgroundInboxService.appVisible = true;
        BackgroundInboxService.clearMessageNotifications(this);
        // И облачка с репликами на виджете портрета
        PortraitWidget.clearBubbles(this);
        // Слушать по имени включено, а службы нет (убита системой, телефон
        // перезагружался): Android 14+ разрешает запустить её только отсюда
        WakeWordService.startIfEnabled(this);
        // Виджеты комнаты и портрета: приложение открыли — заодно освежить, если данные старые
        RoomWidget.refreshIfStale(this);
        PortraitWidget.refreshIfStale(this);
        // Аватар поверх приложений включён, а окна нет (обновление, система убила) — вернуть
        AvatarOverlayService.restore(this);
    }

    // Аватар поверх приложений смотрит, куда нажали и здесь, — без службы
    // спец. возможностей: касания своего окна приложение видит само
    @Override
    public boolean dispatchTouchEvent(MotionEvent ev) {
        if (ev.getActionMasked() == MotionEvent.ACTION_DOWN) {
            AvatarOverlayService.lookAt(Math.round(ev.getRawX()), Math.round(ev.getRawY()));
        }
        return super.dispatchTouchEvent(ev);
    }

    @Override
    public void onPause() {
        BackgroundInboxService.appVisible = false;
        super.onPause();
    }

    // Открыли тапом уведомления или виджета — открыть чат персоны, её
    // комнату или голосовой режим (через плагин в веб)
    private void handleTap(Intent intent) {
        if (intent == null) return;
        // Запуск из «Недавних» повторяет исходный интент — тап уже обработан
        if ((intent.getFlags() & Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY) != 0) return;
        String persona = intent.getStringExtra(BackgroundInboxService.EXTRA_PERSONA);
        if (persona == null) return;
        String section = intent.getStringExtra(BackgroundInboxService.EXTRA_SECTION);
        intent.removeExtra(BackgroundInboxService.EXTRA_PERSONA);
        intent.removeExtra(BackgroundInboxService.EXTRA_SECTION);
        BackgroundInboxPlugin.deliverTap(persona, section);
    }
}
