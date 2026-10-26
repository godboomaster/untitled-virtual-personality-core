package io.vpcore.app;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.os.PowerManager;
import android.text.format.DateFormat;
import android.text.format.DateUtils;
import android.util.Log;
import android.view.View;
import android.widget.RemoteViews;

import androidx.core.content.ContextCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

/**
 * Виджет «Комната» на рабочем столе: сама комната персоны — её арт (фон
 * комнаты + спрайт) целиком, персона на своём месте и в своей позе
 * (RoomWidgetScene), — и под ней строка: имя, занятие и место. Персону
 * выбирают при установке (PersonaWidgetConfigure). У персоны без арта вместо
 * картинки — подсказка, где его добавить.
 *
 * Данные — тот же снимок, что у раздела «Комната» (GET /api/personas/{p}/room),
 * арт — /room/art, только когда он поменялся (art_updated снимка: арт бывает в
 * десятки МБ). Адрес и токен — фоновой службы (BackgroundInboxService):
 * выключена — виджет показывает последнее и пометку «выкл.».
 *
 * Обновляется: при установке; пока служба работает и экран включён — раз в
 * REFRESH_EVERY_MS и при включении экрана (если данным больше STALE_MS); после
 * сообщения персоны; при открытии приложения. Холостых пробуждений спящего
 * телефона нет: состояние комнаты меняется тиками жизни персоны (раз в
 * десятки минут), а смотрят на виджет при включённом экране.
 *
 * Тап — комната персоны в приложении. Сеть и отрисовка — в одном фоновом
 * потоке IO, по очереди.
 */
public class RoomWidget extends AppWidgetProvider {

    private static final String TAG = "RoomWidget";

    static final String SECTION_ROOM = "room";

    private static final String PREFS = "vpc_room_widget";
    // Ключи: w.<виджет> → персона; n.<персона> → имя; s.<персона> → снимок;
    // t.<персона> → когда получен; e.<персона> → ошибка последнего запроса;
    // a.<персона> → art_updated сохранённого арта; список персон для выбора — personas
    static final String KEY_PERSONAS = "personas";

    private static final long REFRESH_EVERY_MS = 15 * 60_000L;
    private static final long STALE_MS = 5 * 60_000L;
    // Версия сохранённого арта (RoomWidgetScene.save): новая — арт перекачать
    private static final int ART_CACHE_VERSION = 2;
    // Картинка комнаты: RemoteViews передаёт её лаунчеру пакетом Binder (предел
    // около 1 МБ). Не прошла — та же картинка меньше
    private static final int SCENE_PIXELS = 560_000;
    private static final int SCENE_PIXELS_FALLBACK = 250_000;

    private static final ScheduledExecutorService IO = Executors.newSingleThreadScheduledExecutor();
    private static ScheduledFuture<?> periodic;
    private static BroadcastReceiver screenReceiver;

    // ── События системы ───────────────────────────────────────────────

    @Override
    public void onUpdate(Context ctx, AppWidgetManager mgr, int[] ids) {
        // Сначала сохранённое (виджет не пустой), потом свежее с ядра
        Context app = ctx.getApplicationContext();
        PendingResult r = goAsync();
        IO.execute(() -> {
            try {
                renderAllNow(app);
                refreshNow(app, null, STALE_MS);
            } finally {
                r.finish();
            }
        });
    }

    @Override
    public void onDeleted(Context ctx, int[] ids) {
        SharedPreferences.Editor e = prefs(ctx).edit();
        for (int id : ids) e.remove("w." + id);
        e.apply();
    }

    // ── Когда обновлять ───────────────────────────────────────────────

    /** Служба запустилась: обновлять, пока экран включён (BackgroundInboxService.onCreate). */
    static synchronized void startAuto(Context svc) {
        Context app = svc.getApplicationContext();
        if (periodic == null) {
            periodic = IO.scheduleWithFixedDelay(() -> {
                if (!interactive(app)) return;
                refreshNow(app, null, REFRESH_EVERY_MS - 60_000L);
                // Заодно портрет: спит ли персона, ночь, облачко
                PortraitWidget.checkState(app);
            }, REFRESH_EVERY_MS, REFRESH_EVERY_MS, TimeUnit.MILLISECONDS);
        }
        if (screenReceiver == null) {
            screenReceiver = new BroadcastReceiver() {
                @Override
                public void onReceive(Context c, Intent i) {
                    refreshIfStale(app);
                    PortraitWidget.checkState(app);
                }
            };
            // Включение экрана — системная рассылка: приходит и неэкспортированному приёмнику
            ContextCompat.registerReceiver(svc, screenReceiver, new IntentFilter(Intent.ACTION_SCREEN_ON),
                    ContextCompat.RECEIVER_NOT_EXPORTED);
        }
    }

    /** Служба остановилась (BackgroundInboxService.onDestroy). */
    static synchronized void stopAuto(Context svc) {
        if (periodic != null) {
            periodic.cancel(false);
            periodic = null;
        }
        if (screenReceiver != null) {
            try {
                svc.unregisterReceiver(screenReceiver);
            } catch (RuntimeException ignored) {
                // уже снят
            }
            screenReceiver = null;
        }
    }

    /** Освежить, если данным больше STALE_MS (включили экран, открыли приложение). */
    static void refreshIfStale(Context ctx) {
        Context app = ctx.getApplicationContext();
        IO.execute(() -> refreshNow(app, null, STALE_MS));
    }

    /** Освежить все сейчас (включили службу, выбрали персону). */
    static void refreshAll(Context ctx) {
        Context app = ctx.getApplicationContext();
        IO.execute(() -> refreshNow(app, null, 0));
    }

    /** Пришло сообщение персоны — её комната могла поменяться: глянуть чуть позже. */
    static void personaActive(Context ctx, String persona) {
        if (persona == null || persona.isEmpty()) return;
        Context app = ctx.getApplicationContext();
        IO.schedule(() -> refreshNow(app, persona, 60_000L), 5, TimeUnit.SECONDS);
    }

    /** Перерисовать из сохранённого (без сети). */
    static void renderAll(Context ctx) {
        Context app = ctx.getApplicationContext();
        IO.execute(() -> renderAllNow(app));
    }

    private static boolean interactive(Context ctx) {
        PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
        return pm == null || pm.isInteractive();
    }

    // ── Данные ────────────────────────────────────────────────────────

    static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** Виджет выбрал персону (PersonaWidgetConfigure). */
    static void configure(Context ctx, int widgetId, String persona, String name) {
        prefs(ctx).edit()
                .putString("w." + widgetId, persona)
                .putString("n." + persona, name)
                .remove("t." + persona)
                .apply();
        refreshAll(ctx);
    }

    private static int[] widgetIds(Context ctx) {
        AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
        if (mgr == null) return new int[0];
        int[] ids = mgr.getAppWidgetIds(new ComponentName(ctx, RoomWidget.class));
        return ids == null ? new int[0] : ids;
    }

    /** Виджеты и их персоны (без ещё не настроенных). */
    private static Map<Integer, String> configured(Context ctx) {
        Map<Integer, String> out = new LinkedHashMap<>();
        SharedPreferences p = prefs(ctx);
        for (int id : widgetIds(ctx)) {
            String persona = p.getString("w." + id, null);
            if (persona != null) out.put(id, persona);
        }
        return out;
    }

    static boolean serviceEnabled(Context ctx) {
        SharedPreferences bg = BackgroundInboxService.prefs(ctx);
        return bg.getBoolean(BackgroundInboxService.KEY_ENABLED, false)
                && !bg.getString(BackgroundInboxService.KEY_BASE_URL, "").isEmpty();
    }

    private static void refreshNow(Context ctx, String onlyPersona, long maxAgeMs) {
        Map<Integer, String> widgets = configured(ctx);
        if (widgets.isEmpty()) return;
        // Перерисовка (с картинкой комнаты) — только если что-то запросили:
        // пачка сообщений персоны зовёт сюда по разу на каждое
        boolean fetched = false;
        boolean enabled = serviceEnabled(ctx);
        if (enabled) {
            Set<String> personas = new LinkedHashSet<>(widgets.values());
            long now = System.currentTimeMillis();
            SharedPreferences p = prefs(ctx);
            for (String persona : personas) {
                if (onlyPersona != null && !onlyPersona.equals(persona)) continue;
                if (now - p.getLong("t." + persona, 0) < maxAgeMs && p.getString("e." + persona, null) == null) continue;
                fetch(ctx, persona);
                fetched = true;
            }
        }
        // Служба выключена — запросов нет, но виджет должен показать «выкл.»
        if (fetched || !enabled) renderAllNow(ctx);
    }

    private static void fetch(Context ctx, String persona) {
        SharedPreferences p = prefs(ctx);
        String base = "/api/personas/" + enc(persona) + "/room";
        try {
            JSONObject snap = new JSONObject(request(ctx, "GET", base + "?chat_id=auto", null));
            p.edit().putString("s." + persona, snap.toString())
                    .putLong("t." + persona, System.currentTimeMillis())
                    .remove("e." + persona)
                    .apply();
            // Арт качаем, только когда он поменялся (или поменялось, что о нём
            // хранит телефон: ART_CACHE_VERSION)
            double au = snap.optDouble("art_updated", Double.NaN);
            String artKey = Double.isNaN(au) ? "" : ART_CACHE_VERSION + ":" + au;
            if (!artKey.equals(p.getString("a." + persona, ""))) {
                if (artKey.isEmpty()) {
                    RoomWidgetScene.clear(ctx, persona);
                } else {
                    RoomWidgetScene.save(ctx, persona, new JSONObject(request(ctx, "GET", base + "/art", null)));
                }
                p.edit().putString("a." + persona, artKey).apply();
            }
        } catch (HttpError e) {
            p.edit().putString("e." + persona, e.code == 404 ? "gone" : "http").apply();
        } catch (Exception e) {
            Log.i(TAG, "комната " + persona + ": " + e.getMessage());
            p.edit().putString("e." + persona, "offline").apply();
        }
    }

    /** Персоны ядра для выбора (PersonaWidgetConfigure): [{id, name}]; сохраняет копию. */
    static JSONArray fetchPersonas(Context ctx) throws Exception {
        JSONArray all = new JSONArray(request(ctx, "GET", "/api/personas", null));
        JSONArray out = new JSONArray();
        for (int i = 0; i < all.length(); i++) {
            JSONObject p = all.optJSONObject(i);
            if (p == null || p.optString("id").isEmpty()) continue;
            out.put(new JSONObject().put("id", p.optString("id")).put("name", p.optString("name", p.optString("id"))));
        }
        prefs(ctx).edit().putString(KEY_PERSONAS, out.toString()).apply();
        return out;
    }

    static final class HttpError extends Exception {
        final int code;

        HttpError(int code) {
            super("HTTP " + code);
            this.code = code;
        }
    }

    // Запрос к ядру по адресу и токену фоновой службы (и у PortraitWidget)
    static String request(Context ctx, String method, String path, String body) throws Exception {
        SharedPreferences bg = BackgroundInboxService.prefs(ctx);
        String url = bg.getString(BackgroundInboxService.KEY_BASE_URL, "").replaceAll("/+$", "");
        String token = bg.getString(BackgroundInboxService.KEY_TOKEN, "");
        if (url.isEmpty()) throw new IllegalStateException("нет адреса ядра");
        HttpURLConnection c = (HttpURLConnection) new URL(url + path).openConnection();
        try {
            c.setConnectTimeout(8_000);
            c.setReadTimeout(20_000);
            c.setUseCaches(false);
            c.setRequestMethod(method);
            c.setRequestProperty("Accept", "application/json");
            if (!token.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + token);
            if (body != null) {
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                try (OutputStream out = c.getOutputStream()) {
                    out.write(body.getBytes(StandardCharsets.UTF_8));
                }
            }
            int code = c.getResponseCode();
            if (code != 200) throw new HttpError(code);
            try (InputStream in = c.getInputStream()) {
                ByteArrayOutputStream buf = new ByteArrayOutputStream();
                byte[] b = new byte[16384];
                int n;
                while ((n = in.read(b)) > 0) buf.write(b, 0, n);
                return buf.toString("UTF-8");
            }
        } finally {
            c.disconnect();
        }
    }

    static String enc(String s) {
        try {
            return URLEncoder.encode(s, "UTF-8").replace("+", "%20");
        } catch (Exception e) {
            return s;
        }
    }

    // ── Отрисовка ─────────────────────────────────────────────────────

    private static void renderAllNow(Context ctx) {
        AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
        if (mgr == null) return;
        for (int id : widgetIds(ctx)) {
            try {
                mgr.updateAppWidget(id, build(ctx, id, SCENE_PIXELS));
            } catch (RuntimeException e) {
                // Картинка не влезла в пакет Binder — та же, но меньше
                Log.w(TAG, "виджет " + id + ": картинка меньше", e);
                try {
                    mgr.updateAppWidget(id, build(ctx, id, SCENE_PIXELS_FALLBACK));
                } catch (RuntimeException e2) {
                    Log.w(TAG, "виджет " + id + " не обновлён", e2);
                }
            }
        }
    }

    private static RemoteViews build(Context ctx, int widgetId, int scenePixels) {
        RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_room);
        SharedPreferences p = prefs(ctx);
        String persona = p.getString("w." + widgetId, null);
        if (persona == null) {
            // Не настроен (настройку закрыли) — тап открывает выбор персоны
            hint(v, ctx.getString(R.string.room_w_pick));
            v.setTextViewText(R.id.room_name, "");
            v.setTextViewText(R.id.room_activity, "");
            v.setTextViewText(R.id.room_status, "");
            Intent cfg = new Intent(ctx, PersonaWidgetConfigure.class)
                    .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, widgetId)
                    .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            v.setOnClickPendingIntent(R.id.room_root, PendingIntent.getActivity(ctx, 0x3000 + widgetId, cfg,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
            return v;
        }
        String name = p.getString("n." + persona, persona);
        boolean enabled = serviceEnabled(ctx);
        JSONObject snap = null;
        try {
            String s = p.getString("s." + persona, null);
            if (s != null) snap = new JSONObject(s);
        } catch (Exception ignored) {
            // испорченный снимок — как нет снимка
        }
        RoomWidgetText text = RoomWidgetText.of(ctx, snap);
        v.setTextViewText(R.id.room_name, name);
        v.setTextViewText(R.id.room_activity, text.activity);
        v.setTextViewText(R.id.room_status, status(ctx, p, persona, enabled, snap != null));

        // Комната целиком; арта нет — подсказка, где его добавить
        Bitmap scene = snap != null && RoomWidgetScene.has(ctx, persona)
                ? RoomWidgetScene.render(ctx, persona, text.spot, text.pose, text.itemZone, scenePixels)
                : null;
        if (scene != null) {
            v.setViewVisibility(R.id.room_scene, View.VISIBLE);
            v.setViewVisibility(R.id.room_hint, View.GONE);
            v.setImageViewBitmap(R.id.room_scene, scene);
        } else if (snap == null) {
            hint(v, ctx.getString(enabled ? R.string.room_w_loading : R.string.room_w_off_hint));
        } else {
            hint(v, ctx.getString(R.string.room_w_no_art, name));
        }

        Intent open = new Intent(ctx, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra(BackgroundInboxService.EXTRA_PERSONA, persona)
                .putExtra(BackgroundInboxService.EXTRA_SECTION, SECTION_ROOM);
        v.setOnClickPendingIntent(R.id.room_root, PendingIntent.getActivity(ctx, 0x2000 + widgetId, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
        return v;
    }

    private static void hint(RemoteViews v, String text) {
        v.setViewVisibility(R.id.room_scene, View.GONE);
        v.setViewVisibility(R.id.room_hint, View.VISIBLE);
        v.setTextViewText(R.id.room_hint, text);
    }

    /** Пометка справа от подписи: служба выключена, нет связи, персоны нет. */
    private static String status(Context ctx, SharedPreferences p, String persona, boolean enabled, boolean hasData) {
        if (!enabled) return ctx.getString(R.string.widget_status_off);
        String err = p.getString("e." + persona, null);
        if (err == null) return "";
        if ("gone".equals(err)) return ctx.getString(R.string.room_w_gone);
        long at = p.getLong("t." + persona, 0);
        return hasData && at > 0 ? ctx.getString(R.string.room_w_offline_at, clock(ctx, at))
                : ctx.getString(R.string.widget_status_offline);
    }

    static String clock(Context ctx, long ms) {
        if (DateUtils.isToday(ms)) return DateFormat.getTimeFormat(ctx).format(new Date(ms));
        return DateUtils.formatDateTime(ctx, ms, DateUtils.FORMAT_SHOW_DATE | DateUtils.FORMAT_SHOW_TIME
                | DateUtils.FORMAT_ABBREV_MONTH | DateUtils.FORMAT_NO_YEAR);
    }
}
