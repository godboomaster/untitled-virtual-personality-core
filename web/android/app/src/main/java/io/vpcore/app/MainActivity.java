package io.vpcore.app;

import android.content.Intent;
import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        // Свои плагины регистрируются до super.onCreate — там поднимается мост
        registerPlugin(BackgroundInboxPlugin.class);
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
    }

    @Override
    public void onPause() {
        BackgroundInboxService.appVisible = false;
        super.onPause();
    }

    // Открыли тапом уведомления персоны — открыть её чат (через плагин в веб)
    private void handleTap(Intent intent) {
        if (intent == null) return;
        // Запуск из «Недавних» повторяет исходный интент — тап уже обработан
        if ((intent.getFlags() & Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY) != 0) return;
        String persona = intent.getStringExtra(BackgroundInboxService.EXTRA_PERSONA);
        if (persona == null) return;
        intent.removeExtra(BackgroundInboxService.EXTRA_PERSONA);
        BackgroundInboxPlugin.deliverTap(persona);
    }
}
