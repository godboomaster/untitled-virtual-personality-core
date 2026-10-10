package io.vpcore.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * Перезагрузка телефона, обновление приложения или выданное разрешение на
 * точные будильники — поставить будильники напоминаний заново
 * (ReminderAlarms). Пропущенные, пока телефон был выключен, — показать
 * сейчас. Слушает только системные события, которые другим приложениям не
 * отправить, а сам лишь переставляет уже сохранённые будильники.
 */
public class ReminderBootReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action)
                && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)
                && !"android.app.action.SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED".equals(action)) {
            return;
        }
        final boolean missed = Intent.ACTION_BOOT_COMPLETED.equals(action);
        final Context app = context.getApplicationContext();
        final PendingResult result = goAsync();
        new Thread(() -> {
            try {
                ReminderAlarms.armAll(app, missed);
            } finally {
                result.finish();
            }
        }, "vpc-reminder-boot").start();
    }
}
