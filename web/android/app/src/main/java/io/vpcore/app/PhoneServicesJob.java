package io.vpcore.app;

import android.app.job.JobParameters;
import android.app.job.JobService;

/**
 * Периодическая проверка сервисов телефона (PhoneServices.reschedule):
 * JobScheduler будит приложение раз в 15 или 30 минут, когда есть сеть
 * (в Doze — в окно обслуживания). Опрос — в своём потоке: onStartJob идёт
 * в главном.
 */
public class PhoneServicesJob extends JobService {

    @Override
    public boolean onStartJob(JobParameters params) {
        new Thread(() -> {
            try {
                PhoneServices.runCheck(getApplicationContext(), false, null);
            } finally {
                jobFinished(params, false);
            }
        }, "vpc-services").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        // Система оборвала (пропала сеть): следующая — по расписанию
        return false;
    }
}
