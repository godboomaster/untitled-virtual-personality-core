package io.vpcore.app;

import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Message;
import android.os.Messenger;
import android.os.Process;
import android.os.RemoteException;
import android.util.Log;

import org.json.JSONArray;

import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Gemma этого приложения — соседнему (приложение ядра ↔ Арродес, см.
 * PhoneGemmaPeer): модель одна на телефоне и считает в одном процессе.
 * Сосед привязывается к службе (bindService) и шлёт запросы Messenger'ом:
 * MSG_STATUS — что со своей моделью, MSG_CHAT — ответ со словами по мере
 * генерации (MSG_TOKEN… MSG_DONE | MSG_ERROR), MSG_WARM — загрузить заранее,
 * MSG_STOP — прервать его ответ, MSG_UNLOAD — выгрузить.
 *
 * Считает только своей моделью (PhoneGemma.chatOwn) — дальше по цепочке не
 * передаёт. Запросы принимаются только от приложения с той же подписью
 * (checkSignatures по uid отправителя): служба открыта (exported) — без
 * этого её мог бы позвать кто угодно.
 */
public class PhoneGemmaService extends Service {

    private static final String TAG = "OfflineLlm";

    private HandlerThread thread;
    private Messenger messenger;
    // Ответы идут по одной в потоке модели; здесь их только ждут
    private final ExecutorService work = Executors.newCachedThreadPool();

    @Override
    public void onCreate() {
        super.onCreate();
        thread = new HandlerThread("vpc-gemma-serve");
        thread.start();
        messenger = new Messenger(new Handler(thread.getLooper(), this::onMessage));
    }

    @Override
    public IBinder onBind(Intent intent) {
        return messenger.getBinder();
    }

    @Override
    public void onDestroy() {
        // Модель не выгружаем: её считает и само приложение, а без дела она
        // выгрузится сама после простоя (PhoneGemma)
        if (thread != null) thread.quitSafely();
        work.shutdown();
        super.onDestroy();
    }

    private boolean trusted(int uid) {
        if (uid < 0) return false;
        if (uid == Process.myUid()) return true;
        return getPackageManager().checkSignatures(Process.myUid(), uid) == PackageManager.SIGNATURE_MATCH;
    }

    /** Ответ соседу; false — его уже нет. */
    private static boolean send(Messenger to, int what, int id, Bundle data) {
        if (to == null) return false;
        Message m = Message.obtain(null, what, id, 0);
        if (data != null) m.setData(data);
        try {
            to.send(m);
            return true;
        } catch (RemoteException e) {
            return false;
        }
    }

    private static Bundle error(String msg) {
        Bundle b = new Bundle();
        b.putString("error", msg == null ? "failed" : (msg.length() > 500 ? msg.substring(0, 500) : msg));
        return b;
    }

    private static String messageOf(Throwable t) {
        return t.getMessage() != null ? t.getMessage() : t.getClass().getSimpleName();
    }

    // Ответы соседа в работе (метки): STOP на другие — статус, уже готовый
    // ответ — без следа, иначе метка пережила бы ответ и сорвала будущий
    // с тем же номером (номера у соседа заново с каждым его запуском)
    private final Set<String> live = ConcurrentHashMap.newKeySet();

    private boolean onMessage(Message msg) {
        final int uid = msg.sendingUid;
        final int id = msg.arg1;
        final Messenger reply = msg.replyTo;
        if (!trusted(uid)) {
            Log.w(TAG, "запрос к Gemma от чужого приложения (uid " + uid + ") — отказ");
            send(reply, PhoneGemmaPeer.MSG_ERROR, id, error("untrusted"));
            return true;
        }
        final PhoneGemma gemma = PhoneGemma.get(this);
        // Метка ответа соседа: прервать можно только его ответ, а не свой
        final String tag = uid + ":" + id;
        switch (msg.what) {
            case PhoneGemmaPeer.MSG_STATUS:
                send(reply, PhoneGemmaPeer.MSG_DONE, id, gemma.ownStatus());
                break;
            case PhoneGemmaPeer.MSG_CHAT: {
                Bundle d = msg.getData();
                final String system = d.getString("system", "");
                final String history = d.getString("history", "[]");
                final String text = d.getString("text", "");
                final int maxTokens = d.getInt("maxTokens", 400);
                final double temperature = d.getDouble("temperature", 0.8);
                final double topP = d.getDouble("topP", 0.95);
                final long deadline = d.getLong("deadline", 0L);
                live.add(tag);
                work.execute(() -> {
                    try {
                        PhoneGemma.Reply r = gemma.chatOwn(system, new JSONArray(history), text, maxTokens,
                                temperature, topP, chunk -> {
                                    Bundle b = new Bundle();
                                    b.putString("text", chunk);
                                    // Соседа нет (умер, отвязался) — считать дальше некому
                                    if (!send(reply, PhoneGemmaPeer.MSG_TOKEN, id, b)) gemma.markCancelled(tag);
                                }, tag, deadline);
                        Bundle b = new Bundle();
                        b.putString("text", r.text);
                        b.putString("backend", r.backend);
                        send(reply, PhoneGemmaPeer.MSG_DONE, id, b);
                    } catch (Throwable t) {
                        send(reply, PhoneGemmaPeer.MSG_ERROR, id, error(messageOf(t)));
                    } finally {
                        live.remove(tag);
                    }
                });
                break;
            }
            case PhoneGemmaPeer.MSG_WARM:
                work.execute(() -> {
                    try {
                        gemma.warmOwn();
                        Bundle b = new Bundle();
                        b.putString("backend", gemma.ownBackend());
                        send(reply, PhoneGemmaPeer.MSG_DONE, id, b);
                    } catch (Throwable t) {
                        send(reply, PhoneGemmaPeer.MSG_ERROR, id, error(messageOf(t)));
                    }
                });
                break;
            case PhoneGemmaPeer.MSG_STOP:
                if (live.contains(tag)) gemma.cancel(tag);
                break;
            case PhoneGemmaPeer.MSG_UNLOAD:
                work.execute(() -> {
                    try {
                        gemma.unloadOwn();
                        send(reply, PhoneGemmaPeer.MSG_DONE, id, new Bundle());
                    } catch (Throwable t) {
                        send(reply, PhoneGemmaPeer.MSG_ERROR, id, error(messageOf(t)));
                    }
                });
                break;
            default:
                send(reply, PhoneGemmaPeer.MSG_ERROR, id, error("unknown_request"));
                break;
        }
        return true;
    }
}
