package io.vpcore.app;

import android.Manifest;
import android.os.Build;

import androidx.core.app.NotificationManagerCompat;

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
 * Плагин Capacitor «BackgroundInbox»: веб включает и выключает фоновую
 * службу уведомлений (BackgroundInboxService) и узнаёт, по какому
 * уведомлению открыли приложение.
 *
 * JS: start({baseUrl, token, clientId, personas?}), stop(), status() →
 * {running, lastOk, lastError}, requestNotificationPermission() →
 * {granted}, testNotification() → {shown, problem}, getLaunchPersona() →
 * {persona, section}; событие notificationTap {persona, section}. section —
 * "room" у тапа по виджету комнаты, "voice" — по виджету портрета (голосовой
 * режим чата), иначе null (чат).
 */
@CapacitorPlugin(
        name = "BackgroundInbox",
        permissions = {
                @Permission(strings = { Manifest.permission.POST_NOTIFICATIONS }, alias = "notifications")
        }
)
public class BackgroundInboxPlugin extends Plugin {

    private static final String EVENT_TAP = "notificationTap";

    // Живой экземпляр плагина (мост поднят) — ему MainActivity отдаёт тапы
    private static BackgroundInboxPlugin instance;
    // Тап, который ещё некому отдать: приложение запущено уведомлением, а
    // веб ещё грузится — заберёт его getLaunchPersona()
    private static String pendingPersona;
    private static String pendingSection;

    @Override
    public void load() {
        instance = this;
    }

    @Override
    protected void handleOnDestroy() {
        if (instance == this) instance = null;
    }

    /** Приложение открыто тапом уведомления или виджета (из MainActivity). */
    static synchronized void deliverTap(String persona, String section) {
        if (persona == null || persona.isEmpty()) return;
        BackgroundInboxPlugin p = instance;
        if (p != null && p.hasListeners(EVENT_TAP)) {
            pendingPersona = null;
            pendingSection = null;
            JSObject data = new JSObject();
            data.put("persona", persona);
            data.put("section", section);
            p.notifyListeners(EVENT_TAP, data);
        } else {
            pendingPersona = persona;
            pendingSection = section;
        }
    }

    @PluginMethod
    public void getLaunchPersona(PluginCall call) {
        String persona;
        String section;
        synchronized (BackgroundInboxPlugin.class) {
            persona = pendingPersona;
            section = pendingSection;
            pendingPersona = null;
            pendingSection = null;
        }
        JSObject ret = new JSObject();
        ret.put("persona", persona);
        ret.put("section", section);
        call.resolve(ret);
    }

    @PluginMethod
    public void start(PluginCall call) {
        String baseUrl = call.getString("baseUrl", "");
        String token = call.getString("token", "");
        String clientId = call.getString("clientId", "");
        // Чьи сообщения показывать (отдельное приложение персоны — только её);
        // без списка — всех
        JSArray personas = call.getArray("personas", new JSArray());
        if (baseUrl == null || !baseUrl.matches("(?i)^https?://.+")) {
            call.reject("baseUrl: нужен адрес http(s)://…");
            return;
        }
        if (clientId == null || !clientId.matches("^[A-Za-z0-9_-]{1,61}$")) {
            call.reject("clientId: 1–61 символ A–Z, a–z, 0–9, _ и -");
            return;
        }
        BackgroundInboxService.prefs(getContext()).edit()
                .putBoolean(BackgroundInboxService.KEY_ENABLED, true)
                .putString(BackgroundInboxService.KEY_BASE_URL, baseUrl)
                .putString(BackgroundInboxService.KEY_TOKEN, token == null ? "" : token)
                .putString(BackgroundInboxService.KEY_CLIENT_ID, clientId)
                .putString(BackgroundInboxService.KEY_PERSONAS, personas.toString())
                .apply();
        // Виджеты: снять подсказку «включите уведомления»; комната — свежий
        // снимок; портрет (тяжёлый, меняется редко) — если сохранённый не свежий
        InitiativeWidget.refresh(getContext());
        RoomWidget.refreshAll(getContext());
        PortraitWidget.refreshIfStale(getContext());
        try {
            BackgroundInboxService.start(getContext());
        } catch (RuntimeException e) {
            // Запуск из фона запрещён (Android 12+) — веб вызывает start, когда
            // приложение на экране, так что это редкость; выбор сохранён
            call.reject("Не удалось запустить службу: " + e.getMessage());
            return;
        }
        call.resolve();
    }

    @PluginMethod
    public void stop(PluginCall call) {
        BackgroundInboxService.prefs(getContext()).edit()
                .putBoolean(BackgroundInboxService.KEY_ENABLED, false)
                .remove(BackgroundInboxService.KEY_TOKEN)
                .apply();
        BackgroundInboxService.stop(getContext());
        // Виджеты: «выкл.» и подсказка
        InitiativeWidget.refresh(getContext());
        RoomWidget.renderAll(getContext());
        PortraitWidget.renderAll(getContext());
        call.resolve();
    }

    @PluginMethod
    public void status(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("running", BackgroundInboxService.running);
        ret.put("lastOk", BackgroundInboxService.lastOk);
        ret.put("lastError", BackgroundInboxService.lastError);
        ret.put("notificationsEnabled", NotificationManagerCompat.from(getContext()).areNotificationsEnabled());
        call.resolve(ret);
    }

    /** «Проверить» в настройках: настоящее уведомление в канал «Сообщения
     * персон». problem — "denied" или "channel", если показать нельзя. */
    @PluginMethod
    public void testNotification(PluginCall call) {
        String problem = BackgroundInboxService.showTest(getContext());
        JSObject ret = new JSObject();
        ret.put("shown", problem == null);
        ret.put("problem", problem);
        call.resolve(ret);
    }

    @PluginMethod
    public void requestNotificationPermission(PluginCall call) {
        // До Android 13 разрешения нет — уведомления разрешены, если их не
        // выключили в настройках приложения
        if (Build.VERSION.SDK_INT < 33 || getPermissionState("notifications") == PermissionState.GRANTED) {
            resolvePermission(call);
            return;
        }
        requestPermissionForAlias("notifications", call, "notificationPermissionCallback");
    }

    @PermissionCallback
    private void notificationPermissionCallback(PluginCall call) {
        resolvePermission(call);
    }

    private void resolvePermission(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("granted", NotificationManagerCompat.from(getContext()).areNotificationsEnabled());
        call.resolve(ret);
    }
}
