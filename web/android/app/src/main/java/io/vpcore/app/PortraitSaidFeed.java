package io.vpcore.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Log;

import androidx.core.content.ContextCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Лента реплик ядра (GET /api/said, app/api/said.py) для виджета «Живой
 * портрет»: пауза до ответа — heard (пользователь написал персоне, text —
 * его сообщение: кивнуть, прочесть его в облачке, задуматься), settled (ход
 * кончился) — и сам ответ в чате (reply): во входящих (/api/inbox, их
 * опрашивает BackgroundInboxService) ответов на реплики нет, только фоновые
 * сообщения. Фоновые (инициатива, напоминание…) приходят и сюда — их
 * пропускаем: служба покажет их из входящих, дважды не надо.
 *
 * Энергия — главное. Долгий опрос идёт, только пока на рабочем столе есть
 * портрет с выбранной персоной (или на экране аватар поверх приложений —
 * AvatarOverlayService), служба включена и экран включён. Экран
 * погас — запрос обрывается, поток уходит. Ни блокировки сна, ни
 * будильников: пока экран выключен, на виджет никто не смотрит —
 * пропущенное не нужно, а начатый ход виджет забывает (перестаёт думать).
 * Включили экран — первый запрос без epoch (ядро отвечает сразу, только
 * номером), дальше запрос висит на ядре до WAIT_SEC и возвращается, как
 * только персона что-то скажет: пока экран включён, это одно соединение
 * без дела.
 *
 * Включение и выключение экрана слушает приёмник, пока работает служба
 * (attach — onCreate, detach — onDestroy), как RoomWidget.startAuto; он же —
 * разблокировку: портрет договаривает реплику (PortraitWidget.resumeSpeech). Портрет
 * поставили или убрали, сменились адрес или токен — update.
 */
final class PortraitSaidFeed {

    private static final String TAG = "PortraitSaid";

    // Ожидание на ядре (предел там — MAX_WAIT_SEC в app/api/said.py) и
    // таймаут чтения с запасом: экран включён — процессор не спит, таймаут идёт
    private static final int WAIT_SEC = 300;
    private static final int READ_TIMEOUT_MS = (WAIT_SEC + 30) * 1000;
    // Нет связи — пауза растёт до минуты
    private static final long RETRY_MS = 5_000;
    private static final long MAX_RETRY_MS = 60_000;
    // Ядро без ленты (старая версия) — не дёргать его зря
    private static final long NO_FEED_MS = 10 * 60_000L;
    // Пустой ответ быстрее этого — ядро не ждёт (не должно так быть): не крутиться вхолостую
    private static final long MIN_ROUND_MS = 2_000;

    // Решения «опрашивать или нет» — по очереди и не в главном потоке
    private static final ExecutorService CTRL = Executors.newSingleThreadExecutor();
    private static final Object LOCK = new Object();
    // Служба работает (приёмник экрана стоит)
    private static volatile boolean attached = false;
    private static BroadcastReceiver screenReceiver;
    // Только в потоке CTRL: идущий опрос (null — не идёт)
    private static Poller poller;

    private PortraitSaidFeed() {
    }

    /** Служба запустилась (BackgroundInboxService.onCreate): слушать экран, опрашивать, если нужно. */
    static void attach(Context svc) {
        synchronized (LOCK) {
            if (screenReceiver == null) {
                screenReceiver = new BroadcastReceiver() {
                    @Override
                    public void onReceive(Context c, Intent i) {
                        // Включили или разблокировали — портрет договаривает
                        // реплику, которая ждала взгляда
                        if (!Intent.ACTION_SCREEN_OFF.equals(i.getAction())) PortraitWidget.resumeSpeech(c);
                        if (!Intent.ACTION_USER_PRESENT.equals(i.getAction())) update(c);
                    }
                };
                IntentFilter f = new IntentFilter(Intent.ACTION_SCREEN_ON);
                f.addAction(Intent.ACTION_SCREEN_OFF);
                f.addAction(Intent.ACTION_USER_PRESENT);
                // Экран — системная рассылка: приходит и неэкспортированному приёмнику
                ContextCompat.registerReceiver(svc, screenReceiver, f, ContextCompat.RECEIVER_NOT_EXPORTED);
            }
            attached = true;
        }
        update(svc);
    }

    /** Служба остановилась (BackgroundInboxService.onDestroy). */
    static void detach(Context svc) {
        synchronized (LOCK) {
            attached = false;
            if (screenReceiver != null) {
                try {
                    svc.unregisterReceiver(screenReceiver);
                } catch (RuntimeException ignored) {
                    // уже снят
                }
                screenReceiver = null;
            }
        }
        update(svc);
    }

    /**
     * Решить заново, нужен ли опрос: служба, экран, портрет на рабочем столе.
     * Идёт, а адрес или токен службы сменились — переподключиться.
     */
    static void update(Context ctx) {
        Context app = ctx.getApplicationContext();
        CTRL.execute(() -> {
            boolean want = attached && interactive(app) && RoomWidget.serviceEnabled(app)
                    && (PortraitWidget.hasWidgets(app) || AvatarOverlayService.running());
            if (want && (poller == null || !poller.isAlive())) {
                poller = new Poller(app);
                poller.start();
            } else if (!want && poller != null) {
                poller.halt();
                poller = null;
                PortraitWidget.saidStopped(app);
            } else if (want) {
                poller.reconnectIfChanged();
            }
        });
    }

    private static boolean interactive(Context ctx) {
        PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
        return pm == null || pm.isInteractive();
    }

    /** Поток долгого опроса: один на включённый экран. */
    private static final class Poller extends Thread {
        private final Context app;
        private volatile boolean stopped = false;
        // Запрос оборван ради новых адреса или токена — не сбой
        private volatile boolean reconnecting = false;
        private volatile HttpURLConnection conn;
        // С какими адресом и токеном идёт запрос
        private volatile String usedUrl = "";
        private volatile String usedToken = "";

        Poller(Context app) {
            super("vpc-portrait-said");
            this.app = app;
            setDaemon(true);
        }

        void halt() {
            stopped = true;
            interrupt();
            abort();
        }

        void reconnectIfChanged() {
            SharedPreferences bg = BackgroundInboxService.prefs(app);
            String url = baseUrl(bg);
            String token = bg.getString(BackgroundInboxService.KEY_TOKEN, "");
            if (url.equals(usedUrl) && token.equals(usedToken)) return;
            reconnecting = true;
            interrupt(); // пауза после сбоя — тоже прервать
            abort();
        }

        /** HttpURLConnection прерывание потока не слышит, а disconnect может ждать сеть — не здесь. */
        private void abort() {
            HttpURLConnection c = conn;
            if (c != null) new Thread(c::disconnect).start();
        }

        @Override
        public void run() {
            String epoch = "";
            long cursor = -1;
            long retry = RETRY_MS;
            while (!stopped) {
                long t0 = SystemClock.elapsedRealtime();
                JSONObject r;
                try {
                    r = fetch(epoch, cursor);
                } catch (Exception e) {
                    if (stopped) break;
                    if (reconnecting) {
                        reconnecting = false;
                        Thread.interrupted();
                        continue;
                    }
                    boolean noFeed = e instanceof RoomWidget.HttpError && ((RoomWidget.HttpError) e).code == 404;
                    Log.i(TAG, "лента реплик: " + e.getMessage());
                    pause(noFeed ? NO_FEED_MS : retry);
                    retry = Math.min(retry * 2, MAX_RETRY_MS);
                    continue;
                }
                if (stopped) break;
                retry = RETRY_MS;
                String ep = r.optString("epoch", "");
                long cur = r.optLong("cursor", -1);
                if (ep.isEmpty() || cur < 0) {
                    pause(NO_FEED_MS); // не та лента
                    continue;
                }
                // Скорость текста в облачке (настройка «Общие» в вебе) — до реплик
                JSONObject text = r.optJSONObject("text");
                if (text != null) PortraitWidget.setTextSpeed(app, text);
                // Первый ответ (epoch ещё не было) — только номер
                boolean first = epoch.isEmpty();
                epoch = ep;
                cursor = cur;
                JSONArray items = r.optJSONArray("messages");
                int n = items == null ? 0 : items.length();
                for (int i = 0; i < n && !first; i++) {
                    JSONObject m = items.optJSONObject(i);
                    if (m == null) continue;
                    String kind = m.optString("kind", "");
                    // Сменился режим персоны — у режима бывает свой аватар
                    if ("mode".equals(kind)) {
                        PortraitWidget.modeChanged(app, m.optString("persona", ""));
                        continue;
                    }
                    // Фоновые сообщения служба показывает из входящих
                    if (!"heard".equals(kind) && !"settled".equals(kind) && !"reply".equals(kind)) continue;
                    PortraitWidget.said(app, m.optString("persona", ""), kind, m.optString("text", ""));
                }
                if (n == 0 && !first && SystemClock.elapsedRealtime() - t0 < MIN_ROUND_MS) pause(MIN_ROUND_MS);
            }
        }

        // Пауза; halt и смена адреса её прерывают
        private void pause(long ms) {
            try {
                Thread.sleep(ms);
            } catch (InterruptedException e) {
                reconnecting = false;
            }
        }

        private JSONObject fetch(String epoch, long cursor) throws Exception {
            // Адрес и токен — свежие: прежняя просьба переподключиться исполнена
            reconnecting = false;
            Thread.interrupted();
            SharedPreferences bg = BackgroundInboxService.prefs(app);
            String url = baseUrl(bg);
            String token = bg.getString(BackgroundInboxService.KEY_TOKEN, "");
            usedUrl = url;
            usedToken = token;
            if (url.isEmpty()) throw new IllegalStateException("нет адреса ядра");
            StringBuilder q = new StringBuilder("?chat_id=web_user&wait=").append(epoch.isEmpty() ? 0 : WAIT_SEC);
            if (!epoch.isEmpty()) {
                q.append("&since=").append(cursor).append("&epoch=").append(URLEncoder.encode(epoch, "UTF-8"));
            }
            HttpURLConnection c = (HttpURLConnection) new URL(url + "/api/said" + q).openConnection();
            conn = c;
            try {
                // halt или смена адреса могли прийти, пока соединение
                // создавалось, — их abort его не видел
                if (stopped || reconnecting) throw new IllegalStateException("остановлен");
                c.setConnectTimeout(10_000);
                c.setReadTimeout(READ_TIMEOUT_MS);
                c.setUseCaches(false);
                c.setRequestProperty("Accept", "application/json");
                if (!token.isEmpty()) c.setRequestProperty("Authorization", "Bearer " + token);
                int code = c.getResponseCode();
                if (code != 200) throw new RoomWidget.HttpError(code);
                return new JSONObject(readAll(c.getInputStream()));
            } finally {
                conn = null;
                c.disconnect();
            }
        }
    }

    private static String baseUrl(SharedPreferences bg) {
        return bg.getString(BackgroundInboxService.KEY_BASE_URL, "").replaceAll("/+$", "");
    }

    private static String readAll(InputStream in) throws Exception {
        try (InputStream s = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = s.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString("UTF-8");
        }
    }
}
