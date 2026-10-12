package io.vpcore.app;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.os.Build;
import android.provider.AlarmClock;
import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.util.ArrayList;
import java.util.List;

/* Команды телефону из чата (web/src/phone/actions.ts): будильник и таймер —
   системными «Часами» через стандартные интенты AlarmClock (EXTRA_SKIP_UI:
   ставятся без своего экрана; часы, которые флаг не знают, покажут готовый
   будильник с кнопкой «Сохранить»), список будильников, запуск приложения по
   пакету, фонарик. Экраны чужих приложений не нажимаются — только интенты.

   Отказы: no_handler — нет приложения часов (или фонарика), not_found — нет
   такого приложения, busy — камера занята, bad_args, failed. Само исполнение —
   PhoneActionsCore: те же команды голосом выполняет WakeWordService. */
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
        res.put("torch", PhoneActionsCore.hasTorch(getContext()));
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
        List<Integer> days = new ArrayList<>();
        JSArray arr = call.getArray("days");
        for (int i = 0; arr != null && i < arr.length(); i++) days.add(arr.optInt(i, 0));
        try {
            start(call, PhoneActionsCore.alarmIntent(call.getInt("hour"), call.getInt("minute"), days,
                call.getString("label", "")));
        } catch (PhoneActionsCore.Failure e) {
            call.reject(e.getMessage(), e.code);
        }
    }

    @PluginMethod
    public void setTimer(PluginCall call) {
        try {
            start(call, PhoneActionsCore.timerIntent(call.getInt("seconds"), call.getString("label", "")));
        } catch (PhoneActionsCore.Failure e) {
            call.reject(e.getMessage(), e.code);
        }
    }

    @PluginMethod
    public void showAlarms(PluginCall call) {
        start(call, PhoneActionsCore.showAlarmsIntent());
    }

    /** Приложения с иконкой на рабочем столе: {apps: [{label, package}]}. */
    @PluginMethod
    public void listApps(PluginCall call) {
        JSArray apps = new JSArray();
        for (PhoneActionsCore.App a : PhoneActionsCore.listApps(getContext())) {
            JSObject app = new JSObject();
            app.put("label", a.label);
            app.put("package", a.pkg);
            apps.put(app);
        }
        JSObject res = new JSObject();
        res.put("apps", apps);
        call.resolve(res);
    }

    @PluginMethod
    public void openApp(PluginCall call) {
        try {
            start(call, PhoneActionsCore.openAppIntent(getContext(), call.getString("package", "")));
        } catch (PhoneActionsCore.Failure e) {
            call.reject(e.getMessage(), e.code);
        }
    }

    @PluginMethod
    public void torch(PluginCall call) {
        boolean on = Boolean.TRUE.equals(call.getBoolean("on", true));
        try {
            PhoneActionsCore.torch(getContext(), on);
            call.resolve();
        } catch (PhoneActionsCore.Failure e) {
            call.reject(e.getMessage(), e.code);
        }
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
        String handler;
        try {
            handler = PhoneActionsCore.handler(activity, intent);
        } catch (PhoneActionsCore.Failure e) {
            call.reject(e.getMessage(), e.code);
            return;
        }
        activity.runOnUiThread(() -> {
            try {
                PhoneActionsCore.start(activity, intent);
                JSObject res = new JSObject();
                res.put("handler", handler);
                call.resolve(res);
            } catch (PhoneActionsCore.Failure e) {
                call.reject(e.getMessage(), e.code);
            }
        });
    }
}
