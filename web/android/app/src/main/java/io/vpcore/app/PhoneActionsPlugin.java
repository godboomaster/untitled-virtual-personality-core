package io.vpcore.app;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraManager;
import android.os.Build;
import android.provider.AlarmClock;
import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/* Команды телефону из чата (web/src/phone/actions.ts): будильник и таймер —
   системными «Часами» через стандартные интенты AlarmClock (EXTRA_SKIP_UI:
   ставятся без своего экрана; часы, которые флаг не знают, покажут готовый
   будильник с кнопкой «Сохранить»), список будильников, запуск приложения по
   пакету, фонарик. Экраны чужих приложений не нажимаются — только интенты.

   Отказы: no_handler — нет приложения часов (или фонарика), not_found — нет
   такого приложения, busy — камера занята, bad_args, failed. */
@CapacitorPlugin(name = "PhoneActions")
public class PhoneActionsPlugin extends Plugin {

    /** Что умеет этот телефон — для проверки в «Настройках»: кто примет
     * будильник, таймер и список будильников ({package, label} или null),
     * есть ли фонарик, сколько приложений видно, модель и версия Android. */
    @PluginMethod
    public void status(PluginCall call) {
        JSObject res = new JSObject();
        res.put("alarm", handlerOf(new Intent(AlarmClock.ACTION_SET_ALARM)));
        res.put("timer", handlerOf(new Intent(AlarmClock.ACTION_SET_TIMER)));
        res.put("alarms", handlerOf(new Intent(AlarmClock.ACTION_SHOW_ALARMS)));
        boolean torch = false;
        try {
            CameraManager cm = (CameraManager) getContext().getSystemService(Context.CAMERA_SERVICE);
            torch = cm != null && torchCamera(cm) != null;
        } catch (CameraAccessException | RuntimeException e) {
            torch = false;
        }
        res.put("torch", torch);
        Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        res.put("apps", getContext().getPackageManager().queryIntentActivities(launcher, 0).size());
        res.put("device", Build.MANUFACTURER + " " + Build.MODEL);
        res.put("sdk", Build.VERSION.SDK_INT);
        call.resolve(res);
    }

    // Кто примет интент: {package, label}; «android» — выбор из нескольких
    private Object handlerOf(Intent intent) {
        PackageManager pm = getContext().getPackageManager();
        ResolveInfo ri = pm.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY);
        if (ri == null || ri.activityInfo == null) return org.json.JSONObject.NULL;
        JSObject h = new JSObject();
        h.put("package", ri.activityInfo.packageName);
        h.put("label", String.valueOf(ri.loadLabel(pm)));
        return h;
    }

    @PluginMethod
    public void setAlarm(PluginCall call) {
        Integer hour = call.getInt("hour");
        Integer minute = call.getInt("minute");
        if (hour == null || minute == null || hour < 0 || hour > 23 || minute < 0 || minute > 59) {
            call.reject("Неверное время будильника", "bad_args");
            return;
        }
        Intent intent = new Intent(AlarmClock.ACTION_SET_ALARM)
            .putExtra(AlarmClock.EXTRA_HOUR, hour)
            .putExtra(AlarmClock.EXTRA_MINUTES, minute)
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true);
        String label = call.getString("label", "");
        if (label != null && !label.isEmpty()) intent.putExtra(AlarmClock.EXTRA_MESSAGE, label);
        // Повтор по дням: 1 — воскресенье … 7 — суббота (Calendar)
        ArrayList<Integer> days = new ArrayList<>();
        JSArray arr = call.getArray("days");
        if (arr != null) {
            for (int i = 0; i < arr.length(); i++) {
                int d = arr.optInt(i, 0);
                if (d >= 1 && d <= 7 && !days.contains(d)) days.add(d);
            }
        }
        if (!days.isEmpty()) intent.putExtra(AlarmClock.EXTRA_DAYS, days);
        start(call, intent);
    }

    @PluginMethod
    public void setTimer(PluginCall call) {
        Integer seconds = call.getInt("seconds");
        if (seconds == null || seconds < 1 || seconds > 86400) {
            call.reject("Неверная длительность таймера", "bad_args");
            return;
        }
        Intent intent = new Intent(AlarmClock.ACTION_SET_TIMER)
            .putExtra(AlarmClock.EXTRA_LENGTH, seconds)
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true);
        String label = call.getString("label", "");
        if (label != null && !label.isEmpty()) intent.putExtra(AlarmClock.EXTRA_MESSAGE, label);
        start(call, intent);
    }

    @PluginMethod
    public void showAlarms(PluginCall call) {
        start(call, new Intent(AlarmClock.ACTION_SHOW_ALARMS));
    }

    /** Приложения с иконкой на рабочем столе: {apps: [{label, package}]}. */
    @PluginMethod
    public void listApps(PluginCall call) {
        PackageManager pm = getContext().getPackageManager();
        Intent launcher = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
        List<ResolveInfo> found = pm.queryIntentActivities(launcher, 0);
        String own = getContext().getPackageName();
        Set<String> seen = new HashSet<>();
        JSArray apps = new JSArray();
        for (ResolveInfo ri : found) {
            if (ri.activityInfo == null) continue;
            String pkg = ri.activityInfo.packageName;
            if (pkg.equals(own)) continue;
            String label = String.valueOf(ri.loadLabel(pm)).trim();
            if (label.isEmpty() || !seen.add(pkg + "\n" + label)) continue;
            JSObject app = new JSObject();
            app.put("label", label);
            app.put("package", pkg);
            apps.put(app);
        }
        JSObject res = new JSObject();
        res.put("apps", apps);
        call.resolve(res);
    }

    @PluginMethod
    public void openApp(PluginCall call) {
        String pkg = call.getString("package", "");
        if (pkg == null || pkg.isEmpty()) {
            call.reject("Не указано приложение", "bad_args");
            return;
        }
        Intent intent = getContext().getPackageManager().getLaunchIntentForPackage(pkg);
        if (intent == null) {
            call.reject("Приложение не найдено: " + pkg, "not_found");
            return;
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        start(call, intent);
    }

    @PluginMethod
    public void torch(PluginCall call) {
        boolean on = Boolean.TRUE.equals(call.getBoolean("on", true));
        CameraManager cm = (CameraManager) getContext().getSystemService(Context.CAMERA_SERVICE);
        if (cm == null) {
            call.reject("Нет фонарика", "no_handler");
            return;
        }
        try {
            String id = torchCamera(cm);
            if (id == null) {
                call.reject("Нет фонарика", "no_handler");
                return;
            }
            cm.setTorchMode(id, on);
            call.resolve();
        } catch (CameraAccessException e) {
            // Камеру держит другое приложение (или съёмка в нашем чате)
            call.reject("Камера занята: " + e.getMessage(), "busy");
        } catch (RuntimeException e) {
            call.reject("Фонарик: " + e.getMessage(), "failed");
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

    // Запуск интента из нашего окна (приложение на экране — человек только что
    // написал); нет обработчика — no_handler. {handler} — пакет того, кто
    // примет («android» — выбор из нескольких)
    private void start(PluginCall call, Intent intent) {
        Activity activity = getActivity();
        if (activity == null) {
            call.reject("Приложение не на экране", "failed");
            return;
        }
        ResolveInfo ri = activity.getPackageManager().resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY);
        if (ri == null || ri.activityInfo == null) {
            call.reject("Нет приложения для " + intent.getAction(), "no_handler");
            return;
        }
        String handler = ri.activityInfo.packageName;
        activity.runOnUiThread(() -> {
            try {
                activity.startActivity(intent);
                JSObject res = new JSObject();
                res.put("handler", handler);
                call.resolve(res);
            } catch (ActivityNotFoundException e) {
                call.reject("Нет приложения для " + intent.getAction(), "no_handler");
            } catch (RuntimeException e) {
                call.reject(String.valueOf(e.getMessage()), "failed");
            }
        });
    }
}
