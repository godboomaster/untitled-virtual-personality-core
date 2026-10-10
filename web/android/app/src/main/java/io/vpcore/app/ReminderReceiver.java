package io.vpcore.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * Будильник напоминания (ReminderAlarms): проба ноутбука и уведомление —
 * в фоновом потоке (goAsync: на сеть есть несколько секунд).
 * Перезагрузка телефона и обновление приложения снимают будильники —
 * их ставит заново ReminderBootReceiver.
 */
public class ReminderReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!ReminderAlarms.ACTION_FIRE.equals(intent.getAction())) return;
        final String ref = intent.getStringExtra(ReminderAlarms.EXTRA_REF);
        if (ref == null || ref.isEmpty()) return;
        final boolean deferred = intent.getBooleanExtra(ReminderAlarms.EXTRA_DEFERRED, false);
        final Context app = context.getApplicationContext();
        final PendingResult result = goAsync();
        new Thread(() -> {
            try {
                ReminderAlarms.onFire(app, ref, deferred);
            } finally {
                result.finish();
            }
        }, "vpc-reminder").start();
    }
}
