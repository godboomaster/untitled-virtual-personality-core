package io.vpcore.app;

import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraManager;
import android.provider.AlarmClock;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/* Исполнение команд телефону — общее для чата (PhoneActionsPlugin) и голоса
   (WakeWordService): интенты «Часов», запуск приложения по пакету, фонарик,
   список приложений. Коды отказа — те же, что видит actions.ts: no_handler,
   not_found, busy, bad_args, failed. */
final class PhoneActionsCore {

    private PhoneActionsCore() {}

    /** Отказ с кодом — как call.reject(message, code) у плагина. */
    static final class Failure extends Exception {
        final String code;

        Failure(String message, String code) {
            super(message);
            this.code = code;
        }
    }

    /** Приложение с иконкой на рабочем столе. */
    static final class App {
        final String label;
        final String pkg;

        App(String label, String pkg) {
            this.label = label;
            this.pkg = pkg;
        }
    }

    // ── Интенты ───────────────────────────────────────────────────────

    /** Будильник без своего экрана; days — 1 (воскресенье) … 7 (суббота). */
    static Intent alarmIntent(Integer hour, Integer minute, List<Integer> days, String label) throws Failure {
        if (hour == null || minute == null || hour < 0 || hour > 23 || minute < 0 || minute > 59) {
            throw new Failure("Неверное время будильника", "bad_args");
        }
        Intent intent = new Intent(AlarmClock.ACTION_SET_ALARM)
            .putExtra(AlarmClock.EXTRA_HOUR, hour)
            .putExtra(AlarmClock.EXTRA_MINUTES, minute)
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true);
        if (label != null && !label.isEmpty()) intent.putExtra(AlarmClock.EXTRA_MESSAGE, label);
        // Повтор по дням (Calendar): вне 1…7 и повторы — выкидываем
        ArrayList<Integer> clean = new ArrayList<>();
        if (days != null) {
            for (Integer d : days) {
                if (d != null && d >= 1 && d <= 7 && !clean.contains(d)) clean.add(d);
            }
        }
        if (!clean.isEmpty()) intent.putExtra(AlarmClock.EXTRA_DAYS, clean);
        return intent;
    }

    static Intent timerIntent(Integer seconds, String label) throws Failure {
        if (seconds == null || seconds < 1 || seconds > 86400) {
            throw new Failure("Неверная длительность таймера", "bad_args");
        }
        Intent intent = new Intent(AlarmClock.ACTION_SET_TIMER)
            .putExtra(AlarmClock.EXTRA_LENGTH, seconds)
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true);
        if (label != null && !label.isEmpty()) intent.putExtra(AlarmClock.EXTRA_MESSAGE, label);
        return intent;
    }

    static Intent showAlarmsIntent() {
        return new Intent(AlarmClock.ACTION_SHOW_ALARMS);
    }

    static Intent openAppIntent(Context ctx, String pkg) throws Failure {
        if (pkg == null || pkg.isEmpty()) throw new Failure("Не указано приложение", "bad_args");
        Intent intent = ctx.getPackageManager().getLaunchIntentForPackage(pkg);
        if (intent == null) throw new Failure("Приложение не найдено: " + pkg, "not_found");
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return intent;
    }

    /** Кто примет интент (пакет; «android» — выбор из нескольких). */
    static String handler(Context ctx, Intent intent) throws Failure {
        ResolveInfo ri = ctx.getPackageManager().resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY);
        if (ri == null || ri.activityInfo == null) {
            throw new Failure("Нет приложения для " + intent.getAction(), "no_handler");
        }
        return ri.activityInfo.packageName;
    }

    /** Запустить экран; ctx — окно приложения или (из службы) любой контекст
     * с FLAG_ACTIVITY_NEW_TASK в интенте. */
    static void start(Context ctx, Intent intent) throws Failure {
        try {
            ctx.startActivity(intent);
        } catch (ActivityNotFoundException e) {
            throw new Failure("Нет приложения для " + intent.getAction(), "no_handler");
        } catch (RuntimeException e) {
            throw new Failure(String.valueOf(e.getMessage()), "failed");
        }
    }

    // ── Фонарик ───────────────────────────────────────────────────────

    /** Не экран, а вызов камеры: работает и из фона, и при выключенном экране. */
    static void torch(Context ctx, boolean on) throws Failure {
        CameraManager cm = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
        if (cm == null) throw new Failure("Нет фонарика", "no_handler");
        try {
            String id = torchCamera(cm);
            if (id == null) throw new Failure("Нет фонарика", "no_handler");
            cm.setTorchMode(id, on);
        } catch (CameraAccessException e) {
            // Камеру держит другое приложение (или съёмка в нашем чате)
            throw new Failure("Камера занята: " + e.getMessage(), "busy");
        } catch (RuntimeException e) {
            throw new Failure("Фонарик: " + e.getMessage(), "failed");
        }
    }

    static boolean hasTorch(Context ctx) {
        try {
            CameraManager cm = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
            return cm != null && torchCamera(cm) != null;
        } catch (CameraAccessException | RuntimeException e) {
            return false;
        }
    }

    // Камера со вспышкой: задняя, если есть
    private static String torchCamera(CameraManager cm) throws CameraAccessException {
        String any = null;
        for (String id : cm.getCameraIdList()) {
            CameraCharacteristics c = cm.getCameraCharacteristics(id);
            if (!Boolean.TRUE.equals(c.get(CameraCharacteristics.FLASH_INFO_AVAILABLE))) continue;
            Integer facing = c.get(CameraCharacteristics.LENS_FACING);
            if (facing != null && facing == CameraCharacteristics.LENS_FACING_BACK) return id;
            if (any == null) any = id;
        }
        return any;
    }

    // ── Приложения ────────────────────────────────────────────────────

    /** Приложения с иконкой на рабочем столе, кроме нашего. */
    static List<App> listApps(Context ctx) {
        PackageManager pm = ctx.getPackageManager();
        Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        List<ResolveInfo> found = pm.queryIntentActivities(launcher, 0);
        String own = ctx.getPackageName();
        Set<String> seen = new HashSet<>();
        List<App> apps = new ArrayList<>();
        for (ResolveInfo ri : found) {
            if (ri.activityInfo == null) continue;
            String pkg = ri.activityInfo.packageName;
            if (pkg.equals(own)) continue;
            String label = String.valueOf(ri.loadLabel(pm)).trim();
            if (label.isEmpty() || !seen.add(pkg + "\n" + label)) continue;
            apps.add(new App(label, pkg));
        }
        return apps;
    }
}
