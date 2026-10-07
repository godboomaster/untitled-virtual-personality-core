package io.vpcore.app;

import android.Manifest;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/* Голосовой режим в приложении: в WebView Android нет Web Speech API
   (ни распознавания, ни озвучки), поэтому веб зовёт этот плагин.

   Распознавание — SpeechRecognizer телефона (обычно Google). Каждая сессия
   прослушивания — свой экземпляр распознавателя: создаётся и живёт только
   в главном потоке, после конца сессии уничтожается. События в веб:
   partial {text}, final {text}, error {code, message}; любая сессия
   заканчивается ровно одним end. У всех событий есть session — номер сессии
   из ответа startListening: веб отличает свою сессию от прошлой.

   Озвучка — TextToSpeech. Движок поднимается при первом обращении
   асинхронно; вызовы до готовности ждут её. speak резолвится, когда фраза
   договорена, прервана или сорвалась — {status: done|interrupted|error|
   unavailable}: веб ждёт конца реплики и в любом случае идёт дальше. */
@CapacitorPlugin(
    name = "NativeSpeech",
    permissions = { @Permission(alias = "microphone", strings = { Manifest.permission.RECORD_AUDIO }) }
)
public class NativeSpeechPlugin extends Plugin {

    // Сколько ждать ответа движка озвучки на инициализацию
    private static final long TTS_INIT_TIMEOUT_MS = 8000;
    // Сколько ждать итога после stopListening: часть движков после остановки
    // молчит — тогда сессию закрываем сами, иначе веб так и «слушает».
    // Онлайн-распознавание при медленной сети отвечает и через 5–8 с, поэтому
    // запас — около собственного сетевого таймаута движка
    private static final long STOP_GRACE_MS = 10000;

    private final Handler main = new Handler(Looper.getMainLooper());

    // ===== Распознавание (поля трогаются только в главном потоке) =====

    private SpeechRecognizer recognizer;
    // Номер текущей сессии: колбэки уже закрытой сессии не считаются
    private int sessionSeq = 0;
    private boolean sessionOpen = false;

    @PluginMethod
    public void isRecognitionAvailable(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("available", SpeechRecognizer.isRecognitionAvailable(getContext()));
        call.resolve(ret);
    }

    @PluginMethod
    public void startListening(PluginCall call) {
        if (getPermissionState("microphone") != PermissionState.GRANTED) {
            call.reject("Microphone permission is not granted", "not-allowed");
            return;
        }
        if (!SpeechRecognizer.isRecognitionAvailable(getContext())) {
            call.reject("Speech recognition service is not available", "unavailable");
            return;
        }
        final String lang = call.getString("lang", "");
        final boolean partial = Boolean.TRUE.equals(call.getBoolean("partialResults", true));
        main.post(() -> {
            // Прошлая сессия ещё жива — закрываем её, не дожидаясь колбэков
            closeSession(null, null);
            final int seq = ++sessionSeq;
            try {
                recognizer = SpeechRecognizer.createSpeechRecognizer(getContext());
            } catch (Exception e) {
                recognizer = null;
                call.reject("Cannot create speech recognizer: " + e.getMessage(), "unavailable");
                return;
            }
            recognizer.setRecognitionListener(new Listener(seq));
            Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
            intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
            intent.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, partial);
            intent.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1);
            intent.putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, getContext().getPackageName());
            if (!lang.isEmpty()) {
                // Без EXTRA_LANGUAGE_PREFERENCE часть движков берёт язык телефона
                intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, lang);
                intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_PREFERENCE, lang);
            }
            sessionOpen = true;
            try {
                recognizer.startListening(intent);
            } catch (Exception e) {
                closeSession("client", "startListening failed: " + e.getMessage());
                call.reject("Cannot start listening: " + e.getMessage(), "client");
                return;
            }
            JSObject ret = new JSObject();
            ret.put("session", seq);
            call.resolve(ret);
        });
    }

    @PluginMethod
    public void stopListening(PluginCall call) {
        main.post(() -> {
            // Остановка микрофона: движок ещё пришлёт итог (final) или
            // «не расслышал» (error) — сессию закроет его колбэк
            if (recognizer != null && sessionOpen) {
                final int seq = sessionSeq;
                try {
                    recognizer.stopListening();
                    // Страховка: движок так и не ответил — закрываем сессию
                    // с сетевой ошибкой: фраза потеряна, и веб скажет почему,
                    // а не промолчит
                    main.postDelayed(() -> {
                        if (seq == sessionSeq && sessionOpen) closeSession("network", "No result after stopListening");
                    }, STOP_GRACE_MS);
                } catch (Exception e) {
                    closeSession("client", "stopListening failed: " + e.getMessage());
                }
            }
            call.resolve();
        });
    }

    // Закрыть текущую сессию: при ошибке — событие error, затем ровно один end.
    // Только из главного потока
    private void closeSession(String errorCode, String errorMessage) {
        boolean wasOpen = sessionOpen;
        sessionOpen = false;
        if (recognizer != null) {
            try {
                recognizer.destroy();
            } catch (Exception ignored) {
                /* распознаватель уже отвязан от службы */
            }
            recognizer = null;
        }
        if (!wasOpen) return;
        if (errorCode != null) {
            JSObject err = new JSObject();
            err.put("session", sessionSeq);
            err.put("code", errorCode);
            err.put("message", errorMessage == null ? errorCode : errorMessage);
            notifyListeners("error", err);
        }
        JSObject end = new JSObject();
        end.put("session", sessionSeq);
        notifyListeners("end", end);
    }

    private static String firstResult(Bundle results) {
        if (results == null) return "";
        ArrayList<String> list = results.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
        if (list == null || list.isEmpty() || list.get(0) == null) return "";
        return list.get(0);
    }

    // Коды ошибок — в духе Web Speech API, чтобы веб обрабатывал оба движка одинаково
    private static String errorCode(int error) {
        switch (error) {
            case SpeechRecognizer.ERROR_NO_MATCH:
                return "no-match";
            case SpeechRecognizer.ERROR_SPEECH_TIMEOUT:
                return "no-speech";
            case SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS:
                return "not-allowed";
            case SpeechRecognizer.ERROR_NETWORK:
            case SpeechRecognizer.ERROR_NETWORK_TIMEOUT:
                return "network";
            case SpeechRecognizer.ERROR_AUDIO:
                return "audio-capture";
            case SpeechRecognizer.ERROR_RECOGNIZER_BUSY:
                return "busy";
            case SpeechRecognizer.ERROR_SERVER:
            case SpeechRecognizer.ERROR_SERVER_DISCONNECTED:
            case SpeechRecognizer.ERROR_TOO_MANY_REQUESTS:
                return "server";
            case SpeechRecognizer.ERROR_LANGUAGE_NOT_SUPPORTED:
            case SpeechRecognizer.ERROR_LANGUAGE_UNAVAILABLE:
                return "language-not-supported";
            default:
                return "client";
        }
    }

    private class Listener implements RecognitionListener {

        private final int seq;

        Listener(int seq) {
            this.seq = seq;
        }

        private boolean current() {
            return seq == sessionSeq && sessionOpen;
        }

        @Override
        public void onReadyForSpeech(Bundle params) {}

        @Override
        public void onBeginningOfSpeech() {}

        @Override
        public void onRmsChanged(float rmsdB) {}

        @Override
        public void onBufferReceived(byte[] buffer) {}

        @Override
        public void onEndOfSpeech() {}

        @Override
        public void onEvent(int eventType, Bundle params) {}

        @Override
        public void onPartialResults(Bundle partialResults) {
            if (!current()) return;
            JSObject ev = new JSObject();
            ev.put("session", seq);
            ev.put("text", firstResult(partialResults));
            notifyListeners("partial", ev);
        }

        @Override
        public void onResults(Bundle results) {
            if (!current()) return;
            String text = firstResult(results);
            if (text.trim().isEmpty()) {
                // Пустой итог — то же, что «не расслышал»
                closeSession("no-match", "Empty recognition result");
                return;
            }
            JSObject ev = new JSObject();
            ev.put("session", seq);
            ev.put("text", text);
            notifyListeners("final", ev);
            closeSession(null, null);
        }

        @Override
        public void onError(int error) {
            // Часть движков присылает ошибку и после итога — сессия уже закрыта
            if (!current()) return;
            String code = errorCode(error);
            closeSession(code, "SpeechRecognizer error " + error);
        }
    }

    // ===== Озвучка =====

    private final Object ttsLock = new Object();
    private TextToSpeech tts;
    // null — инициализация идёт (или не начиналась), true/false — итог
    private Boolean ttsReady = null;
    private final List<Runnable> ttsWaiting = new ArrayList<>();
    // Текущая реплика: длинный текст уходит в движок кусками. job и jobSeq
    // меняются только в главном потоке — реплики не обгоняют друг друга
    private SpeakJob job;
    private int jobSeq = 0;
    // Счётчик stopSpeaking: реплика, заказанная до остановки, но ждавшая
    // готовности движка, после остановки уже не звучит
    private volatile int stopSeq = 0;

    private static final class SpeakJob {

        final PluginCall call;
        final String prefix;
        final String lastId;
        boolean finished = false;

        SpeakJob(PluginCall call, String prefix, String lastId) {
            this.call = call;
            this.prefix = prefix;
            this.lastId = lastId;
        }
    }

    // Выполнить action в главном потоке, когда движок озвучки готов (или
    // окончательно не поднялся). Всё про реплики идёт через главный поток:
    // так doSpeak и stopSpeaking не бегут параллельно
    private void whenTtsReady(Runnable action) {
        boolean runNow;
        boolean startInit = false;
        synchronized (ttsLock) {
            runNow = ttsReady != null;
            if (!runNow) {
                ttsWaiting.add(action);
                if (tts == null) startInit = true;
            }
        }
        if (runNow) {
            main.post(action);
            return;
        }
        if (startInit) main.post(this::initTts);
    }

    // Только из главного потока: TextToSpeech зовёт onInit в нём же
    private void initTts() {
        synchronized (ttsLock) {
            if (tts != null || ttsReady != null) return;
        }
        TextToSpeech engine;
        try {
            engine = new TextToSpeech(getContext(), status -> finishTtsInit(status == TextToSpeech.SUCCESS));
        } catch (Exception e) {
            finishTtsInit(false);
            return;
        }
        boolean keep;
        synchronized (ttsLock) {
            // Без движка в телефоне onInit(ERROR) приходит прямо из конструктора —
            // тогда итог уже записан, а экземпляр не нужен
            keep = !Boolean.FALSE.equals(ttsReady) && tts == null;
            if (keep) tts = engine;
        }
        if (!keep) {
            engine.shutdown();
            return;
        }
        engine.setOnUtteranceProgressListener(new UtteranceProgressListener() {
            @Override
            public void onStart(String utteranceId) {}

            // Колбэки движка приходят в его потоке — разбираем их в главном,
            // рядом с doSpeak/stopSpeaking
            @Override
            public void onDone(String utteranceId) {
                main.post(() -> {
                    SpeakJob j = job;
                    if (j != null && utteranceId != null && utteranceId.equals(j.lastId)) finishJob(j, "done");
                });
            }

            @Override
            @Deprecated
            public void onError(String utteranceId) {
                main.post(() -> failUtterance(utteranceId));
            }

            @Override
            public void onError(String utteranceId, int errorCode) {
                main.post(() -> failUtterance(utteranceId));
            }

            @Override
            public void onStop(String utteranceId, boolean interrupted) {
                main.post(() -> {
                    SpeakJob j = job;
                    if (j != null && utteranceId != null && utteranceId.startsWith(j.prefix)) finishJob(j, "interrupted");
                });
            }
        });
        // Движок может не ответить вовсе — тогда считаем озвучку недоступной
        main.postDelayed(() -> {
            boolean pending;
            synchronized (ttsLock) {
                pending = ttsReady == null;
            }
            if (pending) finishTtsInit(false);
        }, TTS_INIT_TIMEOUT_MS);
    }

    private void finishTtsInit(boolean ok) {
        List<Runnable> waiting;
        synchronized (ttsLock) {
            // Поздний успех после таймаута тоже принимаем: следующие реплики зазвучат
            if (ttsReady != null) {
                if (ok && tts != null) ttsReady = true;
                return;
            }
            ttsReady = ok;
            waiting = new ArrayList<>(ttsWaiting);
            ttsWaiting.clear();
        }
        for (Runnable r : waiting) r.run();
    }

    // Сорвался кусок реплики: реплика окончена с ошибкой, а её хвост, уже
    // стоящий в очереди движка, снимаем — иначе голос звучит после резолва.
    // Только из главного потока
    private void failUtterance(String utteranceId) {
        SpeakJob j = job;
        if (j == null || utteranceId == null || !utteranceId.startsWith(j.prefix)) return;
        finishJob(j, "error");
        stopEngine();
    }

    private void stopEngine() {
        TextToSpeech engine;
        synchronized (ttsLock) {
            engine = tts;
        }
        if (engine == null) return;
        try {
            engine.stop();
        } catch (Exception ignored) {
            /* движок уже остановлен */
        }
    }

    private void finishJob(SpeakJob j, String status) {
        synchronized (ttsLock) {
            if (j.finished) return;
            j.finished = true;
            if (job == j) job = null;
        }
        JSObject ret = new JSObject();
        ret.put("status", status);
        j.call.resolve(ret);
    }

    private boolean ttsUsable() {
        synchronized (ttsLock) {
            return Boolean.TRUE.equals(ttsReady) && tts != null;
        }
    }

    @PluginMethod
    public void isTtsAvailable(PluginCall call) {
        whenTtsReady(() -> {
            JSObject ret = new JSObject();
            ret.put("available", ttsUsable());
            call.resolve(ret);
        });
    }

    @PluginMethod
    public void speak(PluginCall call) {
        final String text = call.getString("text", "");
        final String lang = call.getString("lang", "");
        final Float rate = call.getFloat("rate", 1.0f);
        final int stopAtCall = stopSeq;
        whenTtsReady(() -> {
            if (stopAtCall != stopSeq) {
                // Пока ждали движок, озвучку остановили — реплика не звучит
                JSObject ret = new JSObject();
                ret.put("status", "interrupted");
                call.resolve(ret);
                return;
            }
            doSpeak(call, text, lang, rate == null ? 1.0f : rate);
        });
    }

    // Только из главного потока
    private void doSpeak(PluginCall call, String text, String lang, float rate) {
        if (!ttsUsable()) {
            JSObject ret = new JSObject();
            ret.put("status", "unavailable");
            call.resolve(ret);
            return;
        }
        // Новая реплика вытесняет прошлую
        SpeakJob old = job;
        if (old != null) finishJob(old, "interrupted");
        if (text.trim().isEmpty()) {
            JSObject ret = new JSObject();
            ret.put("status", "done");
            call.resolve(ret);
            return;
        }
        TextToSpeech engine;
        synchronized (ttsLock) {
            engine = tts;
        }
        if (!lang.isEmpty()) {
            // Нет голоса для языка — говорим голосом по умолчанию, но не молчим
            try {
                engine.setLanguage(Locale.forLanguageTag(lang));
            } catch (Exception ignored) {
                /* движок не принял язык */
            }
        }
        engine.setSpeechRate(rate > 0 ? rate : 1.0f);
        List<String> chunks = splitForTts(text, Math.max(200, TextToSpeech.getMaxSpeechInputLength() - 100));
        String prefix = "vpc" + (++jobSeq) + "-";
        SpeakJob j = new SpeakJob(call, prefix, prefix + (chunks.size() - 1));
        synchronized (ttsLock) {
            job = j;
        }
        for (int i = 0; i < chunks.size(); i++) {
            int mode = i == 0 ? TextToSpeech.QUEUE_FLUSH : TextToSpeech.QUEUE_ADD;
            int res = engine.speak(chunks.get(i), mode, null, prefix + i);
            if (res != TextToSpeech.SUCCESS) {
                // Уже принятые куски не договариваем: реплика сорвалась
                finishJob(j, "error");
                stopEngine();
                return;
            }
        }
    }

    // Длинный ответ — кусками не длиннее max, по границам предложений/слов
    static List<String> splitForTts(String text, int max) {
        List<String> out = new ArrayList<>();
        String rest = text.trim();
        while (rest.length() > max) {
            int cut = -1;
            for (int i = max; i > max / 2; i--) {
                char c = rest.charAt(i - 1);
                if (c == '.' || c == '!' || c == '?' || c == '\n') {
                    cut = i;
                    break;
                }
            }
            if (cut < 0) cut = rest.lastIndexOf(' ', max);
            if (cut <= 0) cut = max;
            out.add(rest.substring(0, cut).trim());
            rest = rest.substring(cut).trim();
        }
        if (!rest.isEmpty() || out.isEmpty()) out.add(rest);
        return out;
    }

    @PluginMethod
    public void stopSpeaking(PluginCall call) {
        stopSeq++;
        main.post(() -> {
            stopEngine();
            SpeakJob j = job;
            if (j != null) finishJob(j, "interrupted");
            call.resolve();
        });
    }

    // ===== Жизненный цикл =====

    // Приложение ушло в фон (свернули, погас экран): микрофон отпускаем —
    // сессия закрывается без ошибки, веб получит только end. Иначе служба
    // распознавания сама оборвёт её «нет разрешения», и веб покажет ложную
    // подсказку про настройки. Озвучку не трогаем: как и скрытая вкладка
    // браузера, она договаривает
    @Override
    protected void handleOnPause() {
        super.handleOnPause();
        main.post(() -> {
            if (sessionOpen) closeSession(null, null);
        });
    }

    @Override
    protected void handleOnDestroy() {
        main.removeCallbacksAndMessages(null);
        // Распознаватель — только в главном потоке; onDestroy в нём и приходит
        sessionOpen = false;
        if (recognizer != null) {
            try {
                recognizer.destroy();
            } catch (Exception ignored) {
                /* уже отвязан */
            }
            recognizer = null;
        }
        SpeakJob j;
        TextToSpeech engine;
        List<Runnable> waiting;
        synchronized (ttsLock) {
            j = job;
            job = null;
            engine = tts;
            tts = null;
            ttsReady = false;
            waiting = new ArrayList<>(ttsWaiting);
            ttsWaiting.clear();
        }
        if (j != null && !j.finished) {
            j.finished = true;
            JSObject ret = new JSObject();
            ret.put("status", "interrupted");
            j.call.resolve(ret);
        }
        if (engine != null) {
            try {
                engine.stop();
                engine.shutdown();
            } catch (Exception ignored) {
                /* движок уже отвязан */
            }
        }
        // Ждавшие готовности получат «недоступно»
        for (Runnable r : waiting) r.run();
    }
}
