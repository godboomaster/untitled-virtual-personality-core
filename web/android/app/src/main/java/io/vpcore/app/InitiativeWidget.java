package io.vpcore.app;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.widget.RemoteViews;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.List;

/**
 * Виджет «Инициатива» на рабочем столе (во всю ширину, в один ряд, без
 * заголовка): последние сообщения, которые персоны пишут сами
 * (самоинициатива, утро/ночь/погода — kind "initiative" в /api/inbox;
 * напоминаний, уроков и уведомлений сервисов здесь нет), новые сверху,
 * остальные — прокруткой.
 *
 * Своего опроса у виджета нет: сообщения приносит фоновая служба
 * (BackgroundInboxService) — она и так на долгом опросе ядра, поэтому виджет
 * обновляется за секунды, а не раз в полчаса по расписанию системы. Служба
 * выключена — виджет показывает сохранённое и подсказку, где её включить.
 *
 * Сообщения лежат в SharedPreferences (последние MAX_ITEMS): виджет,
 * поставленный позже, сразу видит недавние, а после перезагрузки телефона
 * список не пустеет. Тап по сообщению — чат персоны (как тап уведомления),
 * по пустому виджету — приложение.
 */
public class InitiativeWidget extends AppWidgetProvider {

    static final String KIND = "initiative";

    private static final String PREFS = "vpc_initiative_widget";
    private static final String KEY_ITEMS = "items";
    private static final int MAX_ITEMS = 30;
    // В виджете видно 2 строки — длинный текст хранить незачем
    private static final int MAX_TEXT = 500;

    @Override
    public void onUpdate(Context ctx, AppWidgetManager mgr, int[] ids) {
        for (int id : ids) mgr.updateAppWidget(id, build(ctx, id));
    }

    // ── Хранилище ─────────────────────────────────────────────────────

    /** Сообщения инициативы из ответа ядра — в список виджета (новые в конец). */
    static void add(Context ctx, List<JSONObject> messages) {
        if (messages.isEmpty()) return;
        synchronized (InitiativeWidget.class) {
            JSONArray items = load(ctx);
            for (JSONObject m : messages) {
                String persona = m.optString("persona", "");
                String text = m.optString("text", "").trim();
                if (persona.isEmpty() || text.isEmpty()) continue;
                double ts = m.optDouble("ts", System.currentTimeMillis() / 1000.0);
                // Ответ, потерянный по дороге, ядро отдаёт ещё раз — второй раз не добавляем
                if (contains(items, persona, ts)) continue;
                try {
                    JSONObject it = new JSONObject();
                    it.put("persona", persona);
                    it.put("name", m.optString("name", persona));
                    it.put("text", text.length() > MAX_TEXT ? text.substring(0, MAX_TEXT) : text);
                    it.put("ts", ts);
                    items.put(it);
                } catch (Exception ignored) {
                    // JSONObject.put бросает только на NaN — ts не NaN
                }
            }
            JSONArray kept = new JSONArray();
            for (int i = Math.max(0, items.length() - MAX_ITEMS); i < items.length(); i++) {
                kept.put(items.opt(i));
            }
            prefs(ctx).edit().putString(KEY_ITEMS, kept.toString()).apply();
        }
        refresh(ctx);
    }

    /** Сохранённые сообщения, старые сначала. */
    static JSONArray load(Context ctx) {
        try {
            return new JSONArray(prefs(ctx).getString(KEY_ITEMS, "[]"));
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    private static boolean contains(JSONArray items, String persona, double ts) {
        for (int i = 0; i < items.length(); i++) {
            JSONObject it = items.optJSONObject(i);
            if (it != null && persona.equals(it.optString("persona"))
                    && Math.abs(it.optDouble("ts", 0) - ts) < 1e-3) return true;
        }
        return false;
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    // ── Отрисовка ─────────────────────────────────────────────────────

    /** Перерисовать все виджеты. */
    @SuppressWarnings("deprecation") // notifyAppWidgetViewDataChanged — см. build()
    static void refresh(Context ctx) {
        AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
        if (mgr == null) return;
        int[] ids = mgr.getAppWidgetIds(new ComponentName(ctx, InitiativeWidget.class));
        if (ids == null || ids.length == 0) return;
        for (int id : ids) mgr.updateAppWidget(id, build(ctx, id));
        mgr.notifyAppWidgetViewDataChanged(ids, R.id.widget_list);
    }

    // Список — через RemoteViewsService: RemoteCollectionItems есть только с
    // Android 12 (а приложение — с 7), а androidx-обёртки над ним в
    // зависимостях нет. Старый путь устарел, но работает на всех версиях
    @SuppressWarnings("deprecation")
    private static RemoteViews build(Context ctx, int widgetId) {
        RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_initiative);

        SharedPreferences bg = BackgroundInboxService.prefs(ctx);
        boolean enabled = bg.getBoolean(BackgroundInboxService.KEY_ENABLED, false)
                && !bg.getString(BackgroundInboxService.KEY_BASE_URL, "").isEmpty();
        v.setTextViewText(R.id.widget_empty, ctx.getString(enabled
                ? R.string.widget_initiative_empty : R.string.widget_initiative_off));

        // data — своя у каждого виджета: иначе система отдаст всем одну фабрику
        Intent svc = new Intent(ctx, InitiativeWidgetService.class)
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId);
        svc.setData(Uri.parse(svc.toUri(Intent.URI_INTENT_SCHEME)));
        v.setRemoteAdapter(R.id.widget_list, svc);
        v.setEmptyView(R.id.widget_list, R.id.widget_empty);

        Intent open = new Intent(ctx, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        v.setOnClickPendingIntent(R.id.widget_empty, PendingIntent.getActivity(ctx, 0x1d17, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
        // Шаблон тапа по сообщению: персону дописывает строка списка
        // (setOnClickFillInIntent) — поэтому изменяемый (Android 12+)
        int mutable = Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0;
        v.setPendingIntentTemplate(R.id.widget_list, PendingIntent.getActivity(ctx, 0x1d18, open,
                PendingIntent.FLAG_UPDATE_CURRENT | mutable));
        return v;
    }
}
