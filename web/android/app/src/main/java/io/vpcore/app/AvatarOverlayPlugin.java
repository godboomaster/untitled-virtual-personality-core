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

/* Аватар поверх приложений — со стороны приложения (web/src/phone/avatarOverlay.ts,
   «Аватар» → «Поверх приложений»): включить для персоны, выключить, состояние
   (право «Поверх других приложений», служба взгляда, есть ли листы головы) и
   переходы в настройки Android. Само окно — AvatarOverlayService. */
@CapacitorPlugin(name = "AvatarOverlay")
public class AvatarOverlayPlugin extends Plugin {

    @PluginMethod
    public void status(PluginCall call) {
        String persona = call.getString("persona", "");
        JSObject res = new JSObject();
        res.put("canDraw", AvatarOverlayService.canDraw(getContext()));
        res.put("running", AvatarOverlayService.running());
        res.put("on", AvatarOverlayService.prefs(getContext()).getBoolean("on", false));
        res.put("persona", AvatarOverlayService.prefs(getContext()).getString("persona", ""));
        res.put("size", AvatarOverlayService.prefs(getContext()).getInt("size", AvatarOverlayService.SIZE_DP_DEFAULT));
        res.put("error", AvatarOverlayService.lastError);
        res.put("passThrough", AvatarOverlayService.prefs(getContext()).getBoolean("pass", false));
        res.put("keyboard", AvatarOverlayService.imeVisible());
        res.put("sizeMin", AvatarOverlayService.SIZE_DP_MIN);
        res.put("sizeMax", AvatarOverlayService.SIZE_DP_MAX);
        res.put("gazeEnabled", AvatarGazeService.enabled(getContext()));
        res.put("gazeRunning", AvatarGazeService.running());
        res.put("hasSheets", persona != null && !persona.isEmpty()
                && AvatarCells.dirOf(getContext(), persona) != null);
        res.put("sdk", Build.VERSION.SDK_INT);
        call.resolve(res);
    }

    /** Показать голову персоны поверх приложений (или сменить персону/размер). */
    @PluginMethod
    public void start(PluginCall call) {
        String persona = call.getString("persona", "");
        String name = call.getString("name", persona);
        int size = call.getInt("size", AvatarOverlayService.SIZE_DP_DEFAULT);
        if (persona == null || persona.isEmpty()) {
            call.reject("persona required", "bad_request");
            return;
        }
        if (!AvatarOverlayService.canDraw(getContext())) {
            call.reject("no overlay permission", "no_permission");
            return;
        }
        if (AvatarCells.dirOf(getContext(), persona) == null) {
            call.reject("no head sheets", "no_sheets");
            return;
        }
        try {
            AvatarOverlayService.startFromApp(getContext(), persona, name == null ? persona : name, size);
        } catch (RuntimeException e) {
            call.reject(e.getMessage(), "start");
            return;
        }
        call.resolve();
    }

    /** «Сквозь касания»: голова не ловит нажатия — они уходят приложению под ней. */
    @PluginMethod
    public void setPassThrough(PluginCall call) {
        AvatarOverlayService.setPassThrough(getContext(), Boolean.TRUE.equals(call.getBoolean("on", false)));
        call.resolve();
    }

    /** Персоны для меню аватара «Сменить персону»: [{id, name}]. */
    @PluginMethod
    public void setPersonas(PluginCall call) {
        com.getcapacitor.JSArray list = call.getArray("personas");
        AvatarOverlayService.setPersonas(getContext(), list == null ? "[]" : list.toString());
        call.resolve();
    }

    @PluginMethod
    public void stop(PluginCall call) {
        AvatarOverlayService.stop(getContext());
        call.resolve();
    }

    /** Право «Поверх других приложений» — страница этого приложения. */
    @PluginMethod
    public void openOverlaySettings(PluginCall call) {
        Intent i = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:" + getContext().getPackageName()));
        if (!open(i) && !open(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION))) {
            call.reject("overlay settings did not open", "failed");
            return;
        }
        call.resolve();
    }

    /** Спец. возможности: «Virtual Persona: куда вы нажимаете». */
    @PluginMethod
    public void openGazeSettings(PluginCall call) {
        if (!open(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))) {
            call.reject("accessibility settings did not open", "failed");
            return;
        }
        call.resolve();
    }

    private boolean open(Intent intent) {
        try {
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(intent);
            return true;
        } catch (RuntimeException e) {
            return false;
        }
    }
}
