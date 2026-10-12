package io.vpcore.app;

import android.Manifest;
import android.content.SharedPreferences;
import android.os.Build;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

/**
 * Плагин Capacitor «WakeWord»: веб включает и выключает голосовую активацию
 * по имени (WakeWordService) и показывает её состояние.
 *
 * JS: start({baseUrl, token}) → {started, reason?: 'mic-denied'} (спрашивает
 * микрофон и, Android 13+, уведомления), stop(), configure({baseUrl, token})
 * — свежие адрес и токен (пустой адрес — служба останавливается, выбор
 * «включено» остаётся), status() → {enabled, running, state, names,
 * lastError, micGranted}.
 */
@CapacitorPlugin(
        name = "WakeWord",
        permissions = {
                @Permission(strings = { Manifest.permission.RECORD_AUDIO }, alias = "microphone"),
                @Permission(strings = { Manifest.permission.POST_NOTIFICATIONS }, alias = "notifications")
        }
)
public class WakeWordPlugin extends Plugin {

    private boolean saveConnection(PluginCall call) {
        String baseUrl = call.getString("baseUrl", "");
        String token = call.getString("token", "");
        if (baseUrl == null || !baseUrl.matches("(?i)^https?://.+")) {
            call.reject("baseUrl: нужен адрес http(s)://…", "no-address");
            return false;
        }
        WakeWordService.prefs(getContext()).edit()
                .putString(WakeWordService.KEY_BASE_URL, baseUrl)
                .putString(WakeWordService.KEY_TOKEN, token == null ? "" : token)
                .apply();
        return true;
    }

    @PluginMethod
    public void start(PluginCall call) {
        if (!saveConnection(call)) return;
        if (getPermissionState("microphone") != PermissionState.GRANTED) {
            requestPermissionForAlias("microphone", call, "micCallback");
            return;
        }
        afterMic(call);
    }

    @PermissionCallback
    private void micCallback(PluginCall call) {
        if (getPermissionState("microphone") != PermissionState.GRANTED) {
            JSObject ret = new JSObject();
            ret.put("started", false);
            ret.put("reason", "mic-denied");
            call.resolve(ret);
            return;
        }
        afterMic(call);
    }

    // Уведомление службы — не обязательно (служба работает и без него), но
    // без него не видно, что телефон слушает, и не выключить из шторки
    private void afterMic(PluginCall call) {
        if (Build.VERSION.SDK_INT >= 33 && getPermissionState("notifications") != PermissionState.GRANTED) {
            requestPermissionForAlias("notifications", call, "notificationsCallback");
            return;
        }
        launch(call);
    }

    @PermissionCallback
    private void notificationsCallback(PluginCall call) {
        launch(call);
    }

    private void launch(PluginCall call) {
        WakeWordService.prefs(getContext()).edit().putBoolean(WakeWordService.KEY_ENABLED, true).apply();
        try {
            WakeWordService.start(getContext());
        } catch (RuntimeException e) {
            call.reject("Не удалось запустить службу: " + e.getMessage(), "start");
            return;
        }
        JSObject ret = new JSObject();
        ret.put("started", true);
        call.resolve(ret);
    }

    @PluginMethod
    public void stop(PluginCall call) {
        WakeWordService.prefs(getContext()).edit().putBoolean(WakeWordService.KEY_ENABLED, false).apply();
        WakeWordService.stop(getContext());
        call.resolve();
    }

    @PluginMethod
    public void configure(PluginCall call) {
        String baseUrl = call.getString("baseUrl", "");
        SharedPreferences p = WakeWordService.prefs(getContext());
        if (baseUrl == null || baseUrl.isEmpty()) {
            // Сервер сменили или отвязали: прежнее ядро больше не спрашиваем
            p.edit().remove(WakeWordService.KEY_BASE_URL).remove(WakeWordService.KEY_TOKEN).apply();
            WakeWordService.stop(getContext());
            call.resolve();
            return;
        }
        if (!saveConnection(call)) return;
        if (p.getBoolean(WakeWordService.KEY_ENABLED, false) && WakeWordService.micGranted(getContext())) {
            try {
                WakeWordService.start(getContext());
            } catch (RuntimeException e) {
                call.reject("Не удалось запустить службу: " + e.getMessage(), "start");
                return;
            }
        }
        call.resolve();
    }

    @PluginMethod
    public void status(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("enabled", WakeWordService.prefs(getContext()).getBoolean(WakeWordService.KEY_ENABLED, false));
        ret.put("running", WakeWordService.running);
        ret.put("state", WakeWordService.state);
        JSArray names = new JSArray();
        for (String n : WakeWordService.names()) names.put(n);
        ret.put("names", names);
        ret.put("lastError", WakeWordService.lastError);
        ret.put("micGranted", WakeWordService.micGranted(getContext()));
        call.resolve(ret);
    }
}
