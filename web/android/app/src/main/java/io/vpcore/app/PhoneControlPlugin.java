package io.vpcore.app;

import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import org.json.JSONObject;

/* Управление экраном телефона — со стороны приложения (web/src/phone/control.ts):
   адрес ядра и токен для службы (configure), состояние для карточки в
   «Настройках», переход в настройки спец. возможностей и пробный снимок
   экрана. Само управление — PhoneControlService и PhoneControlLink. */
@CapacitorPlugin(name = "PhoneControl")
public class PhoneControlPlugin extends Plugin {

    /** Включена ли служба, на связи ли с ядром, последняя ошибка. */
    @PluginMethod
    public void status(PluginCall call) {
        JSObject res = new JSObject();
        res.put("enabled", PhoneControlService.enabled(getContext()));
        res.put("running", PhoneControlService.get() != null);
        res.put("state", PhoneControlLink.state);
        res.put("error", PhoneControlLink.lastError);
        res.put("lastSeen", PhoneControlLink.lastSeenAt);
        res.put("lastCommand", PhoneControlLink.lastCommand);
        res.put("configured", !PhoneControlLink.prefs(getContext())
            .getString(PhoneControlLink.KEY_BASE_URL, "").isEmpty());
        res.put("deviceId", PhoneControlLink.deviceId(getContext()));
        res.put("sdk", Build.VERSION.SDK_INT);
        call.resolve(res);
    }

    /** Адрес ядра и токен — службе; пустой адрес — ядра нет, служба ждёт. */
    @PluginMethod
    public void configure(PluginCall call) {
        String base = call.getString("baseUrl", "");
        String token = call.getString("token", "");
        PhoneControlLink.prefs(getContext()).edit()
            .putString(PhoneControlLink.KEY_BASE_URL, base == null ? "" : base.trim())
            .putString(PhoneControlLink.KEY_TOKEN, token == null ? "" : token)
            .apply();
        PhoneControlLink.kick();
        call.resolve();
    }

    /** Настройки спец. возможностей (страницу самой службы открывает только
     * система — ACTION_ACCESSIBILITY_DETAILS_SETTINGS требует её разрешения). */
    @PluginMethod
    public void openSettings(PluginCall call) {
        if (!start(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))) {
            call.reject("Настройки спец. возможностей не открылись", "failed");
            return;
        }
        call.resolve();
    }

    /** «О приложении»: там, в меню ⋮, Android 13+ прячет «Разрешить
     * ограниченные настройки» для приложений, поставленных не из магазина. */
    @PluginMethod
    public void openAppInfo(PluginCall call) {
        Intent intent = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
            Uri.fromParts("package", getContext().getPackageName(), null));
        if (!start(intent)) {
            call.reject("Сведения о приложении не открылись", "failed");
            return;
        }
        call.resolve();
    }

    /** Пробный снимок экрана (то, что видит ядро) — для проверки службы. */
    @PluginMethod
    public void snapshot(PluginCall call) {
        PhoneControlService s = PhoneControlService.get();
        if (s == null) {
            call.reject("Служба управления экраном не включена", "off");
            return;
        }
        new Thread(() -> {
            try {
                JSONObject snap = PhoneScreen.snapshot(s);
                call.resolve(JSObject.fromJSONObject(snap));
            } catch (PhoneScreen.Failure f) {
                call.reject(f.getMessage(), f.code);
            } catch (Exception e) {
                call.reject(String.valueOf(e.getMessage()), "failed");
            }
        }, "phone-control-snapshot").start();
    }

    private boolean start(Intent intent) {
        if (intent == null) return false;
        try {
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(intent);
            return true;
        } catch (RuntimeException e) {
            return false;
        }
    }
}
