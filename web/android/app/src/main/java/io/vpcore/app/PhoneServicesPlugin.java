package io.vpcore.app;

import android.Manifest;
import android.content.Intent;
import android.net.Uri;
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

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Arrays;
import java.util.Iterator;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Плагин Capacitor «PhoneServices»: «Сервисы» на телефоне без компьютера
 * (PhoneServices.java) — веб видит настройки, статус и ленту, ключи доступа —
 * только масками.
 *
 * JS: status() → {…}; setMode({standalone, personas: [{id, name}], lang});
 * updateSettings({notify?, services?, interval_min?}); addRule({action,
 * source, match}); deleteRule({id}); setDnd({minutes}); checkRss({url});
 * connectGithub({token}); connectImap({user, password, host?, port?,
 * folder?}); imapPreset({user}); setGoogleClient({client_id?,
 * client_secret?, json_text?}); googleLogin() → {url} (и открывает браузер);
 * googleLoginResult(); googleFinish({url}); disconnect({account});
 * checkService({service}); checkNow({service?}) → {ok, polled, new,
 * delivered}; digestNow(); testNotify(); clearFeed(); takeDelivered() →
 * {items}; exportForCore() → тело для ядра; markMoved({services});
 * resume({service}); requestNotificationPermission() → {granted};
 * setTestEndpoints({github?, token?, gmail?, auth?}) — только отладочная
 * сборка. События: delivered (есть что забрать в чат), changed (статус).
 */
@CapacitorPlugin(
        name = "PhoneServices",
        permissions = {
                @Permission(strings = {Manifest.permission.POST_NOTIFICATIONS}, alias = "notifications")
        }
)
public class PhoneServicesPlugin extends Plugin {

    private static volatile PhoneServicesPlugin instance;
    private final ExecutorService io = Executors.newCachedThreadPool();
    private static final List<String> ACCOUNTS = Arrays.asList("rss", "github", "imap", "google");
    private static final int FEED_LIMIT = 80;

    @Override
    public void load() {
        instance = this;
    }

    @Override
    protected void handleOnDestroy() {
        if (instance == this) instance = null;
    }

    static void notifyDelivered() {
        PhoneServicesPlugin p = instance;
        if (p != null) p.notifyListeners("delivered", new JSObject());
    }

    static void notifyChanged() {
        PhoneServicesPlugin p = instance;
        if (p != null) p.notifyListeners("changed", new JSObject());
    }

    private static JSObject js(JSONObject o) {
        try {
            return JSObject.fromJSONObject(o);
        } catch (Exception e) {
            return new JSObject();
        }
    }

    private void bg(PluginCall call, Runnable r) {
        io.execute(() -> {
            try {
                r.run();
            } catch (IllegalArgumentException e) {
                call.reject(e.getMessage(), "invalid");
            } catch (Exception e) {
                call.reject(e.getClass().getSimpleName() + ": " + e.getMessage());
            }
        });
    }

    // ── Статус ──────────────────────────────────────────────────────────

    private static JSONObject publicItem(JSONObject it) {
        JSONObject out = new JSONObject();
        for (String k : new String[]{"id", "source", "kind", "title", "sender", "url", "ts", "received", "group",
                "level", "summary", "reason", "by", "status", "notified_at"}) {
            SvcUtil.put(out, k, it.opt(k));
        }
        SvcUtil.put(out, "text", SvcUtil.head(SvcUtil.str(it, "text"), 600));
        return out;
    }

    JSONObject buildStatus() throws Exception {
        android.content.Context ctx = getContext();
        JSONObject s = PhoneServices.settings(ctx);
        JSONObject sec = PhoneServices.secrets(ctx);
        JSONObject st = PhoneServices.state(ctx);
        JSONObject feed = PhoneServices.feed(ctx);
        JSONObject out = new JSONObject();
        out.put("standalone", PhoneServices.standalone(ctx));
        out.put("running", PhoneServices.running);
        out.put("scheduled", PhoneServices.scheduled(ctx));
        out.put("settings", s);
        out.put("personas", PhoneServices.personas(ctx));
        JSONObject p = PhoneServices.persona(ctx, s);
        out.put("persona", p == null ? JSONObject.NULL : p);
        out.put("notificationsEnabled", NotificationManagerCompat.from(ctx).areNotificationsEnabled());

        JSONObject accounts = new JSONObject();
        JSONObject gh = SvcUtil.obj(sec, "github");
        accounts.put("github", new JSONObject().put("connected", !SvcUtil.str(gh, "token").isEmpty())
                .put("login", SvcUtil.str(gh, "login")).put("token_hint", PhoneServices.mask(SvcUtil.str(gh, "token"))));
        JSONObject im = SvcUtil.obj(sec, "imap");
        accounts.put("imap", new JSONObject().put("connected", !SvcUtil.str(im, "password").isEmpty())
                .put("user", SvcUtil.str(im, "user")).put("host", SvcUtil.str(im, "host"))
                .put("port", (int) SvcUtil.num(im, "port", 993)).put("folder", SvcUtil.str(im, "folder")));
        JSONObject g = SvcUtil.obj(sec, "google");
        accounts.put("google", new JSONObject()
                .put("client", !SvcUtil.str(g, "client_id").isEmpty() && !SvcUtil.str(g, "client_secret").isEmpty())
                .put("client_id", SvcUtil.str(g, "client_id"))
                .put("connected", !SvcUtil.str(g, "refresh_token").isEmpty())
                .put("email", SvcUtil.str(g, "email"))
                .put("gmail", PhoneServices.configured("gmail", s, sec)));
        out.put("accounts", accounts);

        JSONObject status = new JSONObject();
        for (String name : SvcTriage.PHONE_SOURCES) {
            JSONObject one = SvcUtil.obj(st, name);
            status.put(name, new JSONObject()
                    .put("last_ok", one.has("last_ok") ? one.opt("last_ok") : JSONObject.NULL)
                    .put("last_error", SvcUtil.str(one, "last_error"))
                    .put("auth_error", SvcUtil.str(one, "auth_error"))
                    .put("active", PhoneServices.active(name, s, sec)));
        }
        out.put("status", status);
        out.put("last_run", SvcUtil.num(SvcUtil.obj(st, "run"), "last_run", 0));

        JSONArray items = SvcUtil.arr(feed, "items");
        double dayAgo = PhoneServices.now() - 86400;
        int queued = 0;
        int digest = 0;
        int today = 0;
        int notifiedToday = 0;
        JSONArray recent = new JSONArray();
        for (int i = items.length() - 1; i >= 0; i--) {
            JSONObject it = items.optJSONObject(i);
            if (it == null) continue;
            String stt = SvcUtil.str(it, "status");
            if (stt.equals("queued")) queued++;
            if (stt.equals("digest")) digest++;
            if (SvcUtil.num(it, "received", 0) >= dayAgo) today++;
            if (stt.equals("notified") && SvcUtil.num(it, "notified_at", 0) >= dayAgo) notifiedToday++;
            if (recent.length() < FEED_LIMIT) recent.put(publicItem(it));
        }
        out.put("counts", new JSONObject().put("queued", queued).put("digest", digest).put("today", today)
                .put("notified_today", notifiedToday));
        out.put("feed", recent);
        out.put("movable", SvcTransfer.movable(s, sec));
        out.put("now", PhoneServices.now());
        out.put("debuggable", PhoneServices.debuggable(ctx));
        return out;
    }

    @PluginMethod
    public void status(PluginCall call) {
        bg(call, () -> {
            try {
                call.resolve(js(buildStatus()));
            } catch (Exception e) {
                call.reject(e.getMessage());
            }
        });
    }

    @PluginMethod
    public void setMode(PluginCall call) {
        boolean standalone = Boolean.TRUE.equals(call.getBoolean("standalone", false));
        JSArray personas = call.getArray("personas", null);
        String lang = call.getString("lang");
        bg(call, () -> {
            JSONArray clean = null;
            if (personas != null) {
                clean = new JSONArray();
                for (int i = 0; i < personas.length(); i++) {
                    JSONObject p = personas.optJSONObject(i);
                    String id = SvcUtil.str(p, "id");
                    if (!id.matches("^[A-Za-z0-9_-]{1,64}$")) continue;
                    JSONObject one = new JSONObject();
                    SvcUtil.put(one, "id", id);
                    SvcUtil.put(one, "name", SvcUtil.head(SvcUtil.str(p, "name"), 80));
                    clean.put(one);
                }
            }
            PhoneServices.setMode(getContext(), standalone, clean, lang);
            call.resolve();
        });
    }

    // ── Настройки ───────────────────────────────────────────────────────

    /** _overlay ядра: только ключи, что есть в настройках; вложенные — рекурсивно. */
    private static void overlay(JSONObject target, JSONObject patch) {
        Iterator<String> keys = patch.keys();
        while (keys.hasNext()) {
            String k = keys.next();
            if (!target.has(k)) continue;
            Object cur = target.opt(k);
            Object val = patch.opt(k);
            if (cur instanceof JSONObject && val instanceof JSONObject) overlay((JSONObject) cur, (JSONObject) val);
            else SvcUtil.put(target, k, val);
        }
    }

    @PluginMethod
    public void updateSettings(PluginCall call) {
        JSObject data = call.getData();
        bg(call, () -> {
            android.content.Context ctx = getContext();
            synchronized (PhoneServices.LOCK) {
                JSONObject s = PhoneServices.settings(ctx);
                JSONObject patch = new JSONObject();
                for (String k : new String[]{"notify", "services", "interval_min"}) {
                    if (data.has(k)) SvcUtil.put(patch, k, data.opt(k));
                }
                overlay(s, patch);
                PhoneServices.saveSettings(ctx, s);
            }
            PhoneServices.reschedule(ctx);
            call.resolve();
        });
    }

    @PluginMethod
    public void addRule(PluginCall call) {
        JSONObject raw = call.getData();
        bg(call, () -> {
            JSONObject rule = SvcTriage.normalizeRule(raw);
            if (rule == null) throw new IllegalArgumentException("пустое правило");
            synchronized (PhoneServices.LOCK) {
                JSONObject s = PhoneServices.settings(getContext());
                JSONArray rules = new JSONArray().put(rule);
                JSONArray old = SvcUtil.arr(s, "rules");
                for (int i = 0; i < old.length(); i++) rules.put(old.opt(i));
                SvcUtil.put(s, "rules", rules);
                PhoneServices.saveSettings(getContext(), s);
            }
            call.resolve(js(rule));
        });
    }

    @PluginMethod
    public void deleteRule(PluginCall call) {
        String id = call.getString("id", "");
        bg(call, () -> {
            synchronized (PhoneServices.LOCK) {
                JSONObject s = PhoneServices.settings(getContext());
                JSONArray keep = new JSONArray();
                JSONArray old = SvcUtil.arr(s, "rules");
                for (int i = 0; i < old.length(); i++) {
                    if (!id.equals(SvcUtil.str(old.optJSONObject(i), "id"))) keep.put(old.opt(i));
                }
                SvcUtil.put(s, "rules", keep);
                PhoneServices.saveSettings(getContext(), s);
            }
            call.resolve();
        });
    }

    @PluginMethod
    public void setDnd(PluginCall call) {
        int minutes = Math.max(0, Math.min(48 * 60, call.getInt("minutes", 0)));
        bg(call, () -> {
            synchronized (PhoneServices.LOCK) {
                JSONObject s = PhoneServices.settings(getContext());
                SvcUtil.put(s, "dnd_until", minutes > 0 ? PhoneServices.now() + minutes * 60 : 0);
                PhoneServices.saveSettings(getContext(), s);
            }
            call.resolve();
        });
    }

    // ── Подключения ─────────────────────────────────────────────────────

    @PluginMethod
    public void checkRss(PluginCall call) {
        String url = call.getString("url", "").trim();
        if (!url.matches("(?i)^https?://\\S+$")) {
            call.reject("нужен адрес http(s)://", "invalid");
            return;
        }
        bg(call, () -> call.resolve(js(SvcSources.checkFeed(url))));
    }

    private void enable(String service) {
        synchronized (PhoneServices.LOCK) {
            JSONObject s = PhoneServices.settings(getContext());
            SvcUtil.put(SvcUtil.obj(s, "services").optJSONObject(service), "enabled", true);
            SvcUtil.obj(s, "moved").remove(service);
            PhoneServices.saveSettings(getContext(), s);
        }
        PhoneServices.reschedule(getContext());
    }

    @PluginMethod
    public void connectGithub(PluginCall call) {
        String token = call.getString("token", "").trim();
        if (!SvcSources.validGithubToken(token)) {
            call.reject("не похоже на токен GitHub (ghp_…, github_pat_…)", "invalid");
            return;
        }
        bg(call, () -> {
            PhoneServices.applyEndpoints(getContext());
            JSObject ret = new JSObject();
            try {
                JSONObject info = SvcSources.githubCheck(token);
                JSONObject sec = new JSONObject();
                SvcUtil.put(sec, "token", token);
                SvcUtil.put(sec, "login", SvcUtil.str(info, "account"));
                PhoneServices.saveSecret(getContext(), "github", sec);
                PhoneServices.updateState(getContext(), "github", null, true);
                enable("github");
                ret.put("ok", true);
                ret.put("login", SvcUtil.str(info, "account"));
            } catch (SvcSources.SourceError e) {
                ret.put("ok", false);
                ret.put("error", e.getMessage());
            }
            call.resolve(ret);
        });
    }

    @PluginMethod
    public void connectImap(PluginCall call) {
        JSONObject raw = call.getData();
        bg(call, () -> {
            JSONObject acc = SvcImap.normalizeAccount(raw);
            JSObject ret = new JSObject();
            try {
                SvcImap.check(acc, PhoneServices.TLS);
                PhoneServices.saveSecret(getContext(), "imap", acc);
                PhoneServices.updateState(getContext(), "imap", null, true);
                enable("imap");
                ret.put("ok", true);
                ret.put("host", SvcUtil.str(acc, "host"));
            } catch (SvcSources.SourceError e) {
                ret.put("ok", false);
                ret.put("error", e.getMessage());
            }
            call.resolve(ret);
        });
    }

    @PluginMethod
    public void imapPreset(PluginCall call) {
        String[] p = SvcImap.presetFor(call.getString("user", ""));
        JSObject ret = new JSObject();
        ret.put("host", p == null ? "" : p[0]);
        ret.put("port", p == null ? 993 : Integer.parseInt(p[1]));
        call.resolve(ret);
    }

    @PluginMethod
    public void setGoogleClient(PluginCall call) {
        String json = call.getString("json_text", "");
        String cid = call.getString("client_id", "");
        String secret = call.getString("client_secret", "");
        bg(call, () -> {
            JSONObject client = SvcGoogleLogin.parseClient(json, cid, secret);
            android.content.Context ctx = getContext();
            JSONObject old = PhoneServices.secret(ctx, "google");
            // Другой клиент — прежние токены от него не подойдут
            if (!SvcUtil.str(old, "client_id").isEmpty() && !SvcUtil.str(old, "client_id").equals(SvcUtil.str(client, "client_id"))) {
                PhoneServices.disconnect(ctx, "google");
                old = new JSONObject();
            }
            SvcUtil.put(old, "client_id", SvcUtil.str(client, "client_id"));
            SvcUtil.put(old, "client_secret", SvcUtil.str(client, "client_secret"));
            PhoneServices.saveSecret(ctx, "google", old);
            call.resolve();
        });
    }

    @PluginMethod
    public void googleLogin(PluginCall call) {
        bg(call, () -> {
            String url;
            try {
                url = PhoneServices.startGoogleLogin(getContext());
            } catch (java.io.IOException e) {
                call.reject("Не удалось начать вход: " + e.getMessage());
                return;
            }
            // Системный браузер (Chrome): во встроенном WebView Google вход не пускает
            try {
                Intent view = new Intent(Intent.ACTION_VIEW, Uri.parse(url)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                getContext().startActivity(view);
            } catch (Exception e) {
                // Браузера нет — ссылку покажет веб
            }
            JSObject ret = new JSObject();
            ret.put("url", url);
            call.resolve(ret);
        });
    }

    @PluginMethod
    public void googleLoginResult(PluginCall call) {
        JSObject ret = new JSObject();
        JSONObject r = PhoneServices.loginResult;
        ret.put("result", r == null ? JSONObject.NULL : js(r));
        JSONObject g = PhoneServices.secret(getContext(), "google");
        ret.put("connected", !SvcUtil.str(g, "refresh_token").isEmpty());
        ret.put("email", SvcUtil.str(g, "email"));
        call.resolve(ret);
    }

    @PluginMethod
    public void googleFinish(PluginCall call) {
        String url = call.getString("url", "");
        bg(call, () -> call.resolve(js(PhoneServices.finishGoogle(getContext(), null, null, null, url))));
    }

    @PluginMethod
    public void disconnect(PluginCall call) {
        String account = call.getString("account", "");
        if (!ACCOUNTS.contains(account)) {
            call.reject("нет такого сервиса", "invalid");
            return;
        }
        bg(call, () -> {
            PhoneServices.disconnect(getContext(), account);
            call.resolve();
        });
    }

    // ── Проверки ────────────────────────────────────────────────────────

    @PluginMethod
    public void checkService(PluginCall call) {
        String service = call.getString("service", "");
        bg(call, () -> {
            android.content.Context ctx = getContext();
            PhoneServices.applyEndpoints(ctx);
            JSObject ret = new JSObject();
            try {
                JSONObject res;
                switch (service) {
                    case "github":
                        res = SvcSources.githubCheck(SvcUtil.str(PhoneServices.secret(ctx, "github"), "token"));
                        break;
                    case "imap":
                        res = SvcImap.check(PhoneServices.secret(ctx, "imap"), PhoneServices.TLS);
                        break;
                    case "gmail": {
                        JSONObject g = PhoneServices.secret(ctx, "google");
                        String before = g.toString();
                        res = SvcSources.gmailCheck(g, PhoneServices.now());
                        if (!before.equals(g.toString())) PhoneServices.saveSecret(ctx, "google", g);
                        break;
                    }
                    default:
                        call.reject("нет такого сервиса", "invalid");
                        return;
                }
                JSONObject clear = new JSONObject();
                SvcUtil.put(clear, "last_error", "");
                SvcUtil.put(clear, "auth_error", "");
                PhoneServices.updateState(ctx, service, clear, false);
                call.resolve(js(res));
            } catch (SvcSources.SourceError e) {
                ret.put("ok", false);
                ret.put("error", e.getMessage());
                ret.put("auth", e instanceof SvcSources.AuthError);
                call.resolve(ret);
            }
        });
    }

    @PluginMethod
    public void checkNow(PluginCall call) {
        String only = call.getString("service");
        bg(call, () -> call.resolve(js(PhoneServices.runCheck(getContext(), true, only))));
    }

    @PluginMethod
    public void digestNow(PluginCall call) {
        bg(call, () -> {
            JSObject ret = new JSObject();
            ret.put("sent", PhoneServices.digestNow(getContext()));
            call.resolve(ret);
        });
    }

    @PluginMethod
    public void testNotify(PluginCall call) {
        bg(call, () -> {
            JSObject ret = new JSObject();
            ret.put("ok", PhoneServices.testNotify(getContext()));
            call.resolve(ret);
        });
    }

    @PluginMethod
    public void clearFeed(PluginCall call) {
        bg(call, () -> {
            synchronized (PhoneServices.LOCK) {
                JSONObject f = PhoneServices.feed(getContext());
                SvcUtil.put(f, "items", new JSONArray());
                PhoneServices.saveFeed(getContext(), f);
            }
            call.resolve();
        });
    }

    @PluginMethod
    public void takeDelivered(PluginCall call) {
        bg(call, () -> {
            JSObject ret = new JSObject();
            try {
                ret.put("items", new JSArray(PhoneServices.takeDelivered(getContext()).toString()));
            } catch (Exception e) {
                ret.put("items", new JSArray());
            }
            call.resolve(ret);
        });
    }

    // ── Перенос на компьютер ────────────────────────────────────────────

    /** Тело POST /api/integrations/import — ключи целиком, только для ядра. */
    @PluginMethod
    public void exportForCore(PluginCall call) {
        bg(call, () -> {
            android.content.Context ctx = getContext();
            JSONObject bundle = SvcTransfer.bundle(PhoneServices.settings(ctx), PhoneServices.secrets(ctx),
                    PhoneServices.state(ctx), PhoneServices.feed(ctx));
            call.resolve(js(bundle));
        });
    }

    /** Перенесено на компьютер — телефон эти источники больше не проверяет. */
    @PluginMethod
    public void markMoved(PluginCall call) {
        JSArray services = call.getArray("services", new JSArray());
        bg(call, () -> {
            synchronized (PhoneServices.LOCK) {
                JSONObject s = PhoneServices.settings(getContext());
                JSONObject moved = SvcUtil.obj(s, "moved");
                for (int i = 0; i < services.length(); i++) {
                    String name = services.optString(i);
                    if (SvcTriage.PHONE_SOURCES.contains(name)) SvcUtil.put(moved, name, PhoneServices.now());
                }
                SvcUtil.put(s, "moved", moved);
                PhoneServices.saveSettings(getContext(), s);
            }
            PhoneServices.reschedule(getContext());
            call.resolve();
        });
    }

    /** Снова проверять с телефона (перенесённое на компьютер). */
    @PluginMethod
    public void resume(PluginCall call) {
        String name = call.getString("service", "");
        bg(call, () -> {
            synchronized (PhoneServices.LOCK) {
                JSONObject s = PhoneServices.settings(getContext());
                SvcUtil.obj(s, "moved").remove(name);
                PhoneServices.saveSettings(getContext(), s);
            }
            PhoneServices.reschedule(getContext());
            call.resolve();
        });
    }

    @PluginMethod
    public void setTestEndpoints(PluginCall call) {
        if (!PhoneServices.debuggable(getContext())) {
            call.reject("только в отладочной сборке");
            return;
        }
        PhoneServices.setEndpoints(getContext(), call.getData());
        call.resolve();
    }

    // ── Разрешение на уведомления (Android 13+) ─────────────────────────

    @PluginMethod
    public void requestNotificationPermission(PluginCall call) {
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
