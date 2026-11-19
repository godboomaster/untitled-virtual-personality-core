package io.vpcore.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.Dialog;
import android.content.Context;
import android.content.MutableContextWrapper;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebStorage;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import androidx.webkit.ProfileStore;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;
import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

/* Веб-чаты без ключей — ответы персон без ядра (web/src/offline/webchat.ts).
   Невидимый WebView (движок Chromium, без окна) открывает сайт чата, вводит
   промпт, жмёт отправку и читает ответ со страницы — как веб-чаты ядра
   (app/features/web_llm.py), только в телефоне. Проба 30.10 на Honor:
   duck.ai, ChatGPT без входа и Google AI Mode пускают WebView, ответ
   приходит и в фоне, и с погашенным экраном.

   Куки и данные сайтов — в своём профиле WebView «webchat», отдельно от
   данных приложения (его память без ядра — в IndexedDB основного WebView):
   сброс стирает профиль целиком и не трогает приложение. Старый WebView без
   профилей — общий профиль, сброс чистит только куки и хранилище сайтов.

   Вход (DeepSeek) и капчу проходит человек: open показывает вкладку на весь
   экран. Сайтам со входом нельзя заводить много новых чатов — блокируют
   аккаунт (замер ядра 01.10 и 07.10): там один чат на персону, новый — после
   MAX_TURNS реплик и не больше NEW_CHATS_PER_DAY в день. Сайты без входа —
   новый чат на каждый вопрос. Мостов из страниц в приложение нет. */
@CapacitorPlugin(name = "WebChat")
public class WebChatPlugin extends Plugin {

    private static final String TAG = "WebChat";
    private static final String PROFILE = "webchat";
    private static final String PREFS = "webchat";
    private static final int MAX_TURNS = 12; // реплик в одном чате сайта со входом
    private static final int NEW_CHATS_PER_DAY = 6; // новых чатов в день на сайте со входом
    private static final long READY_WAIT_MS = 25_000;
    private static final long IDLE_DESTROY_MS = 5 * 60_000; // вкладка без дела — закрыть (куки остаются)

    private final Handler main = new Handler(Looper.getMainLooper());
    // По одному вопросу за раз: две персоны не делят вкладку одного сайта
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final Map<String, Tab> tabs = new HashMap<>();
    private volatile String busySite;
    private Dialog shown;
    private Tab shownTab; // вкладка на экране у человека (вход, капча) — не усыплять

    private static final class Tab {
        WebView web;
        MutableContextWrapper ctx;
        volatile boolean mainFrameError;
        long usedAt;
        volatile long pumpUntil; // до какого момента подкачивать кадры (pump)
        boolean awake; // onResume — страница видима; после хода — снова onPause
    }

    // Кадры вкладке без окна. У WebView без окна кадров нет: молчат
    // requestAnimationFrame, IntersectionObserver и ResizeObserver, и сайт с
    // виртуальным списком сообщений (DeepSeek) не выводит переписку — ответа
    // в DOM нет, ход ждёт до срока и падает в no_answer. Пока вкладка занята,
    // её рисуют в маленькую картинку: отрисовка прогоняет кадр страницы.
    // После отрисовки страница считает себя видимой и рисует 60 кадров в
    // секунду и дальше — ход кончился, вкладку усыпляют (onPause: страница
    // скрыта, кадров нет), перед ходом будят (onResume).
    private static final long PUMP_MS = 250;
    private Canvas pumpCanvas;
    private boolean pumping;

    private final Runnable pump = new Runnable() {
        @Override
        public void run() {
            long now = SystemClock.elapsedRealtime();
            boolean more = false;
            for (Tab t : tabs.values()) {
                if (t.web == null) continue;
                if (t.pumpUntil < now) {
                    sleep(t);
                    continue;
                }
                more = true;
                if (t.web.isAttachedToWindow()) continue; // на экране — кадры свои
                try {
                    if (pumpCanvas == null) {
                        DisplayMetrics dm = getContext().getResources().getDisplayMetrics();
                        Bitmap b = Bitmap.createBitmap(Math.max(1, dm.widthPixels / 4), Math.max(1, dm.heightPixels / 4),
                            Bitmap.Config.ARGB_8888);
                        pumpCanvas = new Canvas(b);
                        pumpCanvas.scale(0.25f, 0.25f);
                    }
                    t.web.draw(pumpCanvas);
                } catch (Throwable e) {
                    Log.w(TAG, "кадр вкладки: " + e);
                }
            }
            if (more) {
                main.postDelayed(this, PUMP_MS);
            } else {
                pumping = false;
                pumpCanvas = null; // картинка — сборщику
            }
        }
    };

    private static void wake(Tab t) {
        if (t.web != null && !t.awake) {
            t.web.onResume();
            t.awake = true;
        }
    }

    private void sleep(Tab t) {
        if (t.web != null && t.awake && t != shownTab) {
            t.web.onPause();
            t.awake = false;
        }
    }

    /** Подкачивать кадры вкладке ещё ms (только с главного потока). */
    private void pumpFor(Tab t, long ms) {
        wake(t);
        t.pumpUntil = Math.max(t.pumpUntil, SystemClock.elapsedRealtime() + ms);
        if (!pumping) {
            pumping = true;
            main.post(pump);
        }
    }

    // Состояния страницы, ввод, ступени отправки и чтение ответа — по
    // селекторам сайта (как ADAPTERS ядра). Аргумент — JSON {phase, cfg, …}
    private static final String PAGE_JS =
        "(function(a){var c=a.cfg;"
            + "function qa(s){try{return [].slice.call(document.querySelectorAll(s));}catch(e){return [];}}"
            + "function vis(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0;}"
            + "function input(){var l=qa(c.input).filter(vis);return l[0]||null;}"
            + "function answers(){for(var i=0;i<c.answer.length;i++){var l=qa(c.answer[i]);if(l.length)return l;}return [];}"
            + "function body(){return document.body?document.body.innerText:'';}"
            // Ответ — последний блок и его соседи с тем же родителем (у
            // мобильного Google AI Mode каждый абзац — свой блок)
            // Текст блока без служебного внутри (рассуждения модели — c.exclude)
            + "function txt(x){var ex=[];if(c.exclude){try{ex=[].slice.call(x.querySelectorAll(c.exclude));}catch(e){}}"
            + "if(!ex.length)return x.innerText;var was=ex.map(function(e){var d=e.style.display;e.style.display='none';return d;});"
            + "var t=x.innerText;ex.forEach(function(e,i){e.style.display=was[i];});return t;}"
            + "function joined(l){if(!l.length)return '';var par=l[l.length-1].parentElement;"
            + "return l.filter(function(x){return x.parentElement===par;}).map(txt).join('\\n\\n');}"
            + "var CHAL=/verify you are human|are you a robot|checking your browser|unusual traffic|"
            + "подтвердите, что вы (?:не робот|человек)|я не робот|проверка браузера|необычный трафик/i;"
            + "var LIMIT=/daily limit|limit reached|reached (?:the|your) limit|too many (?:requests|messages)|rate limit|"
            + "try again later|дневн\\S* лимит|лимит (?:исчерпан|сообщений)|достигли (?:дневного )?лимита|"
            + "слишком много (?:запросов|сообщений)|попробуйте (?:снова )?позже/i;"
            + "if(a.phase==='probe'){var u=location.href;"
            + "if(c.login&&new RegExp(c.login).test(u))return JSON.stringify({state:'login',url:u});"
            + "var el=input();if(el)return JSON.stringify({state:'ready',url:u});"
            + "var fr=qa('iframe').some(function(f){return /challenges\\.cloudflare|recaptcha|hcaptcha|captcha/.test(f.src||'');});"
            + "if(fr||CHAL.test(body().slice(0,3000))||/\\/sorry\\//.test(u))return JSON.stringify({state:'challenge',url:u});"
            + "return JSON.stringify({state:'loading',url:u});}"
            // Баннер куки — «только необходимые», где он есть
            + "if(a.phase==='consent'){var b=qa('button').filter(function(x){return vis(x)&&"
            + "/^(?:сохранять только обязательные|только необходимые|отклонить(?: все)?|reject (?:all|non-essential)|"
            + "only necessary|necessary only)/i.test((x.innerText||'').trim());})[0];if(b)b.click();"
            + "return JSON.stringify({ok:!!b});}"
            // Режимы сайта, что только замедляют ответ персоны (DeepSeek: «Умный
            // поиск» и «Глубокое мышление») — выключить, если включены
            + "if(a.phase==='modes'){var n=0;if(c.off)qa(c.off).filter(vis).forEach(function(x){x.click();n++;});"
            + "return JSON.stringify({ok:true,off:n});}"
            + "if(a.phase==='fill'){var el=input();if(!el)return JSON.stringify({ok:false,why:'no_input'});"
            + "el.focus();var l=answers();var base=joined(l);"
            + "if(el.isContentEditable){document.execCommand('selectAll',false,null);"
            + "document.execCommand('insertText',false,a.prompt);}"
            + "else{var P=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
            + "Object.getOwnPropertyDescriptor(P,'value').set.call(el,a.prompt);"
            + "el.dispatchEvent(new Event('input',{bubbles:true}));}"
            + "var got=(el.isContentEditable?el.innerText:el.value)||'';"
            + "return JSON.stringify({ok:got.indexOf(a.prompt.slice(0,20))>=0,n0:l.length,base:base});}"
            // Ступени отправки: кнопка (в мобильной вёрстке Enter — новая
            // строка), submit формы, Enter скриптом
            + "if(a.phase==='click'){var el=input();if(!el)return JSON.stringify({ok:false,why:'no_input'});var b=null;"
            + "for(var i=0;i<c.send.length&&!b;i++){b=qa(c.send[i]).filter(function(x){return vis(x)&&!x.disabled"
            + "&&x.getAttribute('aria-disabled')!=='true';})[0];}"
            + "if(!b){var f=el.closest('form');b=f&&[].slice.call(f.querySelectorAll('button[type=submit]'))"
            + ".filter(function(x){return vis(x)&&!x.disabled;})[0];}"
            // Селекторы сайта устарели — крайняя кнопка с иконкой у поля ввода
            + "if(!b){var box=el;for(var k=0;k<4&&box.parentElement;k++)box=box.parentElement;"
            + "var cand=[].slice.call(box.querySelectorAll('button,[role=button]')).filter(function(x){"
            + "return vis(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true'&&x.querySelector('svg');});"
            + "cand.sort(function(x,y){return x.getBoundingClientRect().right-y.getBoundingClientRect().right;});"
            + "b=cand[cand.length-1]||null;}"
            + "if(!b)return JSON.stringify({ok:false,why:'no_button'});b.click();return JSON.stringify({ok:true});}"
            + "if(a.phase==='submit'){var el=input();var f=el&&el.closest('form');if(!f)return JSON.stringify({ok:false});"
            + "f.requestSubmit();return JSON.stringify({ok:true});}"
            + "if(a.phase==='enter'){var el=input();if(!el)return JSON.stringify({ok:false});"
            + "['keydown','keypress','keyup'].forEach(function(t){el.dispatchEvent(new KeyboardEvent(t,"
            + "{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true}));});"
            + "return JSON.stringify({ok:true});}"
            // Новый ответ — блоков стало больше или текст последнего сменился
            // (DeepSeek держит в DOM только последний обмен)
            + "if(a.phase==='read'){var l=answers();var last=l.length?l[l.length-1]:null;var all=joined(l);"
            + "var fresh=last&&(l.length>a.n0||all!==a.base);"
            + "var el=input();var left=el?((el.isContentEditable?el.innerText:el.value)||'').trim().length:-1;"
            // Признак конца — у последнего ответа; doneUp — сколько уровней до
            // его хода (дальше — старые ответы со своими кнопками)
            + "var done=false;if(fresh&&c.done&&c.doneUp){var q=last;for(var k=0;k<c.doneUp&&q;k++)q=q.parentElement;"
            + "done=!!(q&&q.querySelector(c.done));}"
            + "else if(fresh&&c.done){var p=last;for(var k=0;k<6&&p&&!done;k++){"
            + "done=!!(p.matches&&p.matches(c.done))||!!(p.querySelector&&p.querySelector(c.done));p=p.parentElement;}}"
            + "var text=fresh?all:'';"
            + "return JSON.stringify({n:l.length,text:text,done:done,left:left,"
            + "limit:!text&&a.limit?LIMIT.test(body().slice(-3000)):false});}"
            + "return JSON.stringify({ok:false,why:'phase'});})(%ARG%)";

    /** Сайты: id — как в web/src/offline/webchatSites.ts. */
    static JSONObject site(String id) {
        try {
            JSONObject s = new JSONObject();
            switch (id) {
                case "duckai":
                    // Без аккаунта; первая отправка («Запрос») принимает условия сайта
                    s.put("home", "https://duck.ai/");
                    s.put("input", "textarea[name=user-prompt]");
                    s.put("answer", new JSONArray()
                        .put("div[id*='-assistant-message-']:not([id^='heading-']) > div:has(.space-y-4)")
                        .put("div[id*='-assistant-message-']:not([id^='heading-']) .space-y-4"));
                    s.put("done", "button[aria-label*='copy' i], button[aria-label*='копир' i]");
                    s.put("send", new JSONArray().put("button[type=submit]"));
                    break;
                case "chatgpt":
                    // Без входа; у мобильной вёрстки своё поле и разметка ответа
                    s.put("home", "https://chatgpt.com/");
                    s.put("input", "#mobile-composer-prompt, #prompt-textarea");
                    s.put("answer", new JSONArray().put("li[data-message-role=assistant] [data-assistant-markdown-root]")
                        .put("[data-message-author-role=assistant] .markdown"));
                    s.put("done", "li[data-message-complete]");
                    s.put("send", new JSONArray().put("button[data-testid=send-button]")
                        .put("button[aria-label='Отправить сообщение']").put("button[aria-label='Send message']"));
                    s.put("login", "/auth/login");
                    // Вошли — свой чат на персону (адрес /c/…), как у сайтов со входом
                    s.put("thread", "/c/");
                    break;
                case "google":
                    // AI Mode без входа: свежий тред на каждый вопрос
                    s.put("home", "https://www.google.com/search?udm=50");
                    s.put("input", "textarea.ITIRGe, textarea[name=q]");
                    s.put("answer", new JSONArray().put(".pWvJNd").put(".mZJni > div > .n6owBd"));
                    s.put("done", "button[aria-label*='copy' i], button[aria-label*='копир' i]");
                    s.put("send", new JSONArray().put("button[aria-label*='send' i]").put("button[aria-label*='отправ' i]"));
                    break;
                case "deepseek":
                    s.put("home", "https://chat.deepseek.com/");
                    s.put("input", "textarea");
                    s.put("answer", new JSONArray().put(".ds-assistant-message-main-content").put(".ds-markdown"));
                    // Дописан — под сообщением полоска кнопок (копировать, «Читать
                    // вслух»…): ответ → .ds-message → его ход
                    s.put("done", ".ds-message + .ds-flex [role=button]");
                    s.put("doneUp", 2);
                    // Поиск и глубокое мышление сайта ответу персоны не нужны (поиск
                    // делает приложение), а ответ с ними — в разы дольше
                    s.put("off", ".ds-toggle-button[aria-pressed=true]");
                    s.put("send", new JSONArray().put("div[role=button].ds-button--primary")
                        .put("div[role=button][aria-disabled=false]:has(svg)"));
                    s.put("login", "/sign_in|/sign_up|/login");
                    s.put("account", true);
                    s.put("thread", "/a/chat/s/");
                    break;
                // Дальше — сайты ядра со входом (ADAPTERS в app/features/web_llm.py);
                // селекторы — с компьютера, мобильная вёрстка проверяется после входа
                case "qwen":
                    s.put("home", "https://chat.qwen.ai/");
                    s.put("input", "textarea.message-input-textarea, textarea");
                    s.put("answer", new JSONArray()
                        .put(".qwen-chat-message-assistant .response-message-content.phase-answer")
                        .put(".qwen-chat-message-assistant .chat-response-message"));
                    s.put("done", "");
                    s.put("send", new JSONArray().put("button.send-button").put("#send-message-button")
                        .put("button[aria-label*='send' i]"));
                    s.put("login", "/auth");
                    s.put("account", true);
                    s.put("thread", "/c/");
                    break;
                case "kimi":
                    s.put("home", "https://www.kimi.com/");
                    s.put("input", "div.chat-input-editor, div[contenteditable=true]");
                    s.put("answer", new JSONArray().put(".segment-content-box > .markdown-container")
                        .put(".segment-content-box:not(:has(.user-content))"));
                    s.put("exclude", ".thinking-container");
                    s.put("done", ".segment-assistant-actions");
                    s.put("send", new JSONArray().put(".send-button-container:not(.disabled)").put("div.send-button")
                        .put("button[aria-label*='send' i]"));
                    s.put("login", "/login");
                    s.put("account", true);
                    s.put("thread", "/chat/");
                    break;
                case "zai":
                    s.put("home", "https://chat.z.ai/");
                    s.put("input", "textarea#chat-input, textarea");
                    s.put("answer", new JSONArray()
                        .put(".chat-assistant #response-content-container > .markdown-prose")
                        .put(".chat-assistant.markdown-prose"));
                    s.put("exclude", ".thinking-chain-container");
                    s.put("done", "");
                    s.put("send", new JSONArray().put("#send-message-button").put("button[type=submit]"));
                    s.put("login", "/auth");
                    s.put("account", true);
                    s.put("thread", "/c/");
                    break;
                case "claude":
                    s.put("home", "https://claude.ai/new");
                    s.put("input", "div[contenteditable=true]");
                    s.put("answer", new JSONArray().put("div.font-claude-response").put(".font-claude-response-body"));
                    s.put("exclude", ".sr-only");
                    s.put("done", "");
                    s.put("send", new JSONArray().put("button[aria-label*='send' i]").put("button[aria-label*='отправ' i]"));
                    s.put("login", "/login");
                    s.put("account", true);
                    s.put("thread", "/chat/");
                    break;
                default:
                    return null;
            }
            return s;
        } catch (org.json.JSONException e) {
            return null;
        }
    }

    private static final String[] SITES = {"duckai", "chatgpt", "google", "deepseek", "qwen", "kimi", "zai", "claude"};

    private SharedPreferences prefs() {
        return getContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static boolean profiles() {
        return WebViewFeature.isFeatureSupported(WebViewFeature.MULTI_PROFILE);
    }

    // ── Вкладки ──────────────────────────────────────────────────────────

    @SuppressLint("SetJavaScriptEnabled")
    private Tab tab(String id) {
        Tab t = tabs.get(id);
        if (t != null) {
            t.usedAt = SystemClock.elapsedRealtime();
            return t;
        }
        t = new Tab();
        // Без окна — контекст приложения; на экране (вход, капча) — окна
        // приложения: выпадающие списки форм рисуются только с ним
        t.ctx = new MutableContextWrapper(getContext().getApplicationContext());
        WebView w = new WebView(t.ctx);
        if (profiles()) {
            ProfileStore.getInstance().getOrCreateProfile(PROFILE);
            WebViewCompat.setProfile(w, PROFILE);
        }
        WebSettings ws = w.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setAllowFileAccess(false);
        ws.setAllowContentAccess(false);
        ws.setSupportMultipleWindows(false);
        ws.setMediaPlaybackRequiresUserGesture(true);
        cookies(w).setAcceptCookie(true);
        cookies(w).setAcceptThirdPartyCookies(w, true);
        final Tab tt = t;
        w.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                String s = req.getUrl().getScheme();
                // intent:, market: и прочее — ничего вне вкладки
                return !("http".equals(s) || "https".equals(s));
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) tt.mainFrameError = true;
            }
        });
        w.setWebChromeClient(new android.webkit.WebChromeClient());
        w.setBackgroundColor(Color.WHITE);
        layoutDetached(w);
        t.web = w;
        t.usedAt = SystemClock.elapsedRealtime();
        tabs.put(id, t);
        scheduleIdle();
        return t;
    }

    private CookieManager cookies(WebView w) {
        return profiles() ? WebViewCompat.getProfile(w).getCookieManager() : CookieManager.getInstance();
    }

    /** Без окна у WebView нет размера — размер экрана, иначе сайт верстается в ноль. */
    private void layoutDetached(WebView w) {
        DisplayMetrics dm = getContext().getResources().getDisplayMetrics();
        w.measure(View.MeasureSpec.makeMeasureSpec(dm.widthPixels, View.MeasureSpec.EXACTLY),
            View.MeasureSpec.makeMeasureSpec(dm.heightPixels, View.MeasureSpec.EXACTLY));
        w.layout(0, 0, dm.widthPixels, dm.heightPixels);
    }

    private final Runnable idleSweep = () -> {
        long now = SystemClock.elapsedRealtime();
        for (String id : SITES) {
            Tab t = tabs.get(id);
            if (t != null && shown == null && !id.equals(busySite) && now - t.usedAt > IDLE_DESTROY_MS) {
                t.web.destroy();
                tabs.remove(id);
            }
        }
        if (!tabs.isEmpty()) scheduleIdle();
    };

    private void scheduleIdle() {
        main.removeCallbacks(idleSweep);
        main.postDelayed(idleSweep, IDLE_DESTROY_MS + 1000);
    }

    // ── Ход вопроса ──────────────────────────────────────────────────────

    private static final class Fail extends Exception {
        final String code;

        Fail(String code, String msg) {
            super(msg);
            this.code = code;
        }
    }

    /**
     * ask({site, persona, prompt, fresh?, timeoutMs?}) → {text, ms, site}.
     * Отказ — код: login, challenge, limit, offline, budget, no_page,
     * not_sent, no_answer, busy, bad_args.
     */
    @PluginMethod
    public void ask(PluginCall call) {
        final String id = call.getString("site", "");
        final JSONObject cfg = site(id);
        final String persona = call.getString("persona", "");
        final String prompt = call.getString("prompt", "");
        final boolean fresh = Boolean.TRUE.equals(call.getBoolean("fresh", false));
        final long timeout = Math.max(10_000, call.getInt("timeoutMs", 90_000));
        if (cfg == null || prompt.trim().isEmpty()) {
            call.reject("site/prompt", "bad_args");
            return;
        }
        io.execute(() -> {
            busySite = id;
            long t0 = SystemClock.elapsedRealtime();
            try {
                String text = runAsk(id, cfg, persona, prompt, fresh, timeout);
                setState(id, "ok");
                JSObject ret = new JSObject();
                ret.put("text", text);
                ret.put("ms", SystemClock.elapsedRealtime() - t0);
                ret.put("site", id);
                Log.i(TAG, id + ": ответ " + text.length() + " симв. за " + (SystemClock.elapsedRealtime() - t0) + " мс");
                call.resolve(ret);
            } catch (Fail f) {
                if (!"busy".equals(f.code) && !"budget".equals(f.code)) setState(id, f.code);
                Log.i(TAG, id + ": " + f.code + " " + f.getMessage());
                call.reject(f.getMessage(), f.code);
            } catch (Exception e) {
                setState(id, "failed");
                Log.w(TAG, id + ": " + e);
                call.reject(String.valueOf(e.getMessage()), "failed");
            } finally {
                busySite = null;
            }
        });
    }

    private String runAsk(String id, JSONObject cfg, String persona, String prompt, boolean fresh, long timeout)
        throws Exception {
        if (shown != null) throw new Fail("busy", "вкладка открыта для человека");
        boolean account = cfg.optBoolean("account");
        String key = id + "." + safe(persona);
        SharedPreferences p = prefs();
        // Свой чат на персону — у сайтов с адресом чата (со входом; ChatGPT — когда вошли)
        String thread = cfg.has("thread") && !fresh ? p.getString("thread." + key, "") : "";
        int turns = p.getInt("turns." + key, 0);
        boolean newChat = thread.isEmpty() || turns >= MAX_TURNS;
        if (account && newChat) {
            // Новый чат на сайте со входом — из дневного запаса; запас кончился,
            // а прежний чат есть — дальше в нём
            if (newChatsToday(id) >= NEW_CHATS_PER_DAY) {
                if (thread.isEmpty()) throw new Fail("budget", "новые чаты на сегодня кончились");
                newChat = false;
            }
        }
        final String url = newChat ? cfg.optString("home") : thread;
        final long budget = READY_WAIT_MS + timeout + 30_000;
        Tab t = onMainT(() -> {
            Tab x = tab(id);
            x.mainFrameError = false;
            pumpFor(x, budget);
            x.web.loadUrl(url);
            return x;
        });
        try {
            return runAskIn(t, id, cfg, prompt, account, key, newChat, turns, timeout);
        } finally {
            // Ход окончен — кадры ещё пару секунд (сайт дописывает своё) и хватит
            onMain(() -> t.pumpUntil = SystemClock.elapsedRealtime() + 2000);
        }
    }

    private String runAskIn(Tab t, String id, JSONObject cfg, String prompt, boolean account, String key,
                            boolean newChat, int turns, long timeout) throws Exception {
        SharedPreferences p = prefs();
        long tStart = SystemClock.elapsedRealtime();
        waitReady(t, cfg);
        long tReady = SystemClock.elapsedRealtime();
        // Поле видно раньше, чем страница оживает (ChatGPT): текст, введённый
        // до того, сайт не видит — пауза, а не вышло — ввод и отправка заново
        Thread.sleep(1500);
        page(t, "consent", cfg, null);
        if (cfg.has("off")) {
            int off = page(t, "modes", cfg, null).optInt("off");
            if (off > 0) {
                Log.i(TAG, id + ": выключено режимов сайта: " + off);
                Thread.sleep(500);
            }
        }
        int n0 = 0;
        String base = "";
        boolean sent = false;
        for (int attempt = 0; attempt < 2 && !sent; attempt++) {
            if (attempt > 0) Thread.sleep(2500);
            JSONObject fill = page(t, "fill", cfg, new JSONObject().put("prompt", prompt));
            if (!fill.optBoolean("ok")) throw new Fail("not_sent", "поле ввода не приняло текст");
            if (attempt == 0) {
                n0 = fill.optInt("n0");
                base = fill.optString("base");
            }
            Thread.sleep(400);
            for (String how : new String[] {"click", "submit", "enter"}) {
                if (!page(t, how, cfg, null).optBoolean("ok")) continue;
                // Ушло — поле очистилось или пошёл ответ; ждём до 1,5 с, смотрим чаще
                for (int w = 0; w < 5 && !sent; w++) {
                    Thread.sleep(300);
                    JSONObject chk = read(t, cfg, n0, base, false);
                    sent = chk.optInt("left") == 0 || !chk.optString("text").isEmpty();
                }
                if (sent) break;
            }
        }
        if (!sent) throw new Fail("not_sent", "сайт не отправил сообщение");
        if (account && newChat) bumpNewChats(id);
        long t0 = SystemClock.elapsedRealtime();
        long tFirst = 0; // первые слова ответа
        String text = "";
        long stableSince = SystemClock.elapsedRealtime();
        boolean stableDone = false;
        while (SystemClock.elapsedRealtime() - t0 < timeout) {
            Thread.sleep(700);
            JSONObject r = read(t, cfg, n0, base, false);
            String now = r.optString("text").trim();
            boolean same = now.equals(text);
            if (!same) {
                if (text.isEmpty()) tFirst = SystemClock.elapsedRealtime();
                text = now;
                stableSince = SystemClock.elapsedRealtime();
            }
            if (!text.isEmpty() && same && r.optBoolean("done")) {
                stableDone = true;
                break;
            }
            // Без признака конца — текст не менялся 5 с
            if (!text.isEmpty() && SystemClock.elapsedRealtime() - stableSince > 5000) {
                stableDone = true;
                break;
            }
            // Ответа всё нет — не лимит ли
            if (text.isEmpty() && SystemClock.elapsedRealtime() - t0 > 6000
                && read(t, cfg, n0, base, true).optBoolean("limit")) {
                throw new Fail("limit", "сайт сообщил о лимите");
            }
        }
        if (text.isEmpty()) throw new Fail("no_answer", "ответа нет");
        if (!stableDone) Log.i(TAG, id + ": ответ не дописан к сроку — берём что есть");
        long tEnd = SystemClock.elapsedRealtime();
        Log.i(TAG, id + ": страница " + (tReady - tStart) + " мс, отправка " + (t0 - tReady) + " мс, первые слова "
            + (tFirst > 0 ? tFirst - t0 : -1) + " мс, весь ответ " + (tEnd - t0) + " мс"
            + (newChat ? ", новый чат" : ""));
        if (cfg.has("thread")) {
            String at = onMainS(() -> t.web.getUrl());
            if (at != null && at.contains(cfg.optString("thread", "\u0000"))) {
                p.edit().putString("thread." + key, at)
                    .putInt("turns." + key, (newChat ? 0 : turns) + 1).apply();
            }
        }
        onMain(() -> t.usedAt = SystemClock.elapsedRealtime());
        return text;
    }

    private void waitReady(Tab t, JSONObject cfg) throws Exception {
        long t0 = SystemClock.elapsedRealtime();
        String last = "loading";
        while (SystemClock.elapsedRealtime() - t0 < READY_WAIT_MS) {
            Thread.sleep(500);
            if (t.mainFrameError) throw new Fail("offline", "страница не загрузилась");
            last = page(t, "probe", cfg, null).optString("state");
            if ("ready".equals(last)) return;
            if ("login".equals(last)) throw new Fail("login", "нужен вход");
            // Капча — не сразу: проверка Cloudflare часто проходит сама
            if ("challenge".equals(last) && SystemClock.elapsedRealtime() - t0 > 8000) {
                throw new Fail("challenge", "сайт просит проверку");
            }
        }
        // Сайт не отрисовался (старый WebView не тянет его скрипты) или висит
        throw new Fail("challenge".equals(last) ? "challenge" : "no_page", "поле ввода не появилось");
    }

    private JSONObject read(Tab t, JSONObject cfg, int n0, String base, boolean limit) throws Exception {
        return page(t, "read", cfg, new JSONObject().put("n0", n0).put("base", base).put("limit", limit));
    }

    private JSONObject page(Tab t, String phase, JSONObject cfg, JSONObject extra) throws Exception {
        JSONObject arg = extra != null ? extra : new JSONObject();
        arg.put("phase", phase).put("cfg", cfg);
        final String js = PAGE_JS.replace("%ARG%", arg.toString());
        final String[] out = new String[1];
        final CountDownLatch latch = new CountDownLatch(1);
        main.post(() -> {
            if (t.web == null) {
                latch.countDown();
                return;
            }
            t.web.evaluateJavascript(js, v -> {
                out[0] = v;
                latch.countDown();
            });
        });
        if (!latch.await(15, TimeUnit.SECONDS)) return new JSONObject();
        return parse(out[0]);
    }

    // ── Человеку: вход и капча ───────────────────────────────────────────

    /** open({site, title?}) — вкладка сайта на весь экран; закрыли — {state}. */
    @PluginMethod
    public void open(PluginCall call) {
        final String id = call.getString("site", "");
        final JSONObject cfg = site(id);
        final String title = call.getString("title", "");
        if (cfg == null) {
            call.reject("site", "bad_args");
            return;
        }
        main.post(() -> {
            Activity act = getActivity();
            if (act == null || shown != null || id.equals(busySite)) {
                call.reject("busy", "busy");
                return;
            }
            Tab t = tab(id);
            t.ctx.setBaseContext(act);
            shownTab = t;
            wake(t);
            String now = t.web.getUrl();
            if (now == null || now.isEmpty() || "about:blank".equals(now)) t.web.loadUrl(cfg.optString("home"));
            Dialog d = new Dialog(act, android.R.style.Theme_DeviceDefault_Light_NoActionBar);
            LinearLayout col = new LinearLayout(act);
            col.setOrientation(LinearLayout.VERTICAL);
            col.setBackgroundColor(Color.WHITE);
            LinearLayout bar = new LinearLayout(act);
            bar.setGravity(Gravity.CENTER_VERTICAL);
            int pad = Math.round(8 * act.getResources().getDisplayMetrics().density);
            bar.setPadding(pad * 2, pad, pad, pad);
            TextView label = new TextView(act);
            label.setText(title);
            label.setTextColor(Color.BLACK);
            bar.addView(label, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
            Button ok = new Button(act);
            ok.setText(call.getString("done", "Готово"));
            ok.setOnClickListener(v -> d.dismiss());
            bar.addView(ok);
            col.addView(bar);
            col.addView(t.web, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
            // Окно во весь экран рисуется под системными панелями — отступы по ним
            col.setOnApplyWindowInsetsListener((v, in) -> {
                int top = 0;
                int bottom = 0;
                if (android.os.Build.VERSION.SDK_INT >= 30) {
                    android.graphics.Insets i = in.getInsets(WindowInsets.Type.systemBars());
                    top = i.top;
                    bottom = i.bottom;
                } else {
                    top = in.getSystemWindowInsetTop();
                    bottom = in.getSystemWindowInsetBottom();
                }
                v.setPadding(0, top, 0, bottom);
                return in;
            });
            d.setContentView(col);
            Window w = d.getWindow();
            if (w != null) {
                w.setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
                // Клавиатура формы входа — окно сжимается, поле не прячется под ней
                w.setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
            }
            d.setOnKeyListener((dlg, code, ev) -> {
                if (code == KeyEvent.KEYCODE_BACK && ev.getAction() == KeyEvent.ACTION_UP && t.web.canGoBack()) {
                    t.web.goBack();
                    return true;
                }
                return false;
            });
            d.setOnDismissListener(dlg -> {
                col.removeView(t.web);
                t.ctx.setBaseContext(getContext().getApplicationContext());
                layoutDetached(t.web);
                shown = null;
                shownTab = null;
                sleep(t); // без окна — спит до следующего хода
                io.execute(() -> {
                    String state = "loading";
                    try {
                        state = page(t, "probe", cfg, null).optString("state", "loading");
                    } catch (Exception ignored) {
                    }
                    if (!"loading".equals(state)) setState(id, "ready".equals(state) ? "ok" : state);
                    JSObject ret = new JSObject();
                    ret.put("state", state);
                    call.resolve(ret);
                });
            });
            shown = d;
            d.show();
        });
    }

    /** check({site}) — открыть сайт и посмотреть, пускает ли (вход, капча), ничего не отправляя. */
    @PluginMethod
    public void check(PluginCall call) {
        final String id = call.getString("site", "");
        final JSONObject cfg = site(id);
        if (cfg == null) {
            call.reject("site", "bad_args");
            return;
        }
        io.execute(() -> {
            busySite = id;
            String state;
            try {
                Tab t = onMainT(() -> {
                    Tab x = tab(id);
                    x.mainFrameError = false;
                    pumpFor(x, READY_WAIT_MS + 5000);
                    x.web.loadUrl(cfg.optString("home"));
                    return x;
                });
                waitReady(t, cfg);
                state = "ok";
            } catch (Fail f) {
                state = f.code;
            } catch (Exception e) {
                state = "failed";
            } finally {
                busySite = null;
            }
            setState(id, state);
            JSObject ret = new JSObject();
            ret.put("state", state);
            call.resolve(ret);
        });
    }

    /** Что известно о сайтах: последнее состояние, запас новых чатов, профиль. */
    @PluginMethod
    public void status(PluginCall call) {
        SharedPreferences p = prefs();
        JSArray list = new JSArray();
        for (String id : SITES) {
            JSObject s = new JSObject();
            s.put("id", id);
            s.put("state", p.getString("state." + id, ""));
            s.put("stateAt", p.getLong("stateAt." + id, 0));
            JSONObject cfg = site(id);
            if (cfg != null && cfg.optBoolean("account")) {
                s.put("newChatsLeft", Math.max(0, NEW_CHATS_PER_DAY - newChatsToday(id)));
            }
            list.put(s);
        }
        JSObject ret = new JSObject();
        ret.put("sites", list);
        ret.put("profile", profiles() ? "separate" : "shared");
        ret.put("busy", busySite);
        call.resolve(ret);
    }

    /** forget({persona}) — забыть чаты персоны на сайтах со входом (переписку очистили). */
    @PluginMethod
    public void forget(PluginCall call) {
        String persona = safe(call.getString("persona", ""));
        SharedPreferences.Editor e = prefs().edit();
        for (String id : SITES) {
            e.remove("thread." + id + "." + persona).remove("turns." + id + "." + persona);
        }
        e.apply();
        call.resolve();
    }

    /** Сбросить веб-чаты: вкладки, куки и данные сайтов, входы, чаты персон. */
    @PluginMethod
    public void reset(PluginCall call) {
        main.post(() -> {
            if (shown != null || busySite != null) {
                call.reject("busy", "busy");
                return;
            }
            for (Tab t : tabs.values()) t.web.destroy();
            tabs.clear();
            boolean separate = profiles();
            boolean wiped;
            if (separate) {
                // Куки и хранилище — только этого профиля; сам профиль удаляется,
                // когда закрытые вкладки его отпустят (не вышло — данные и так стёрты)
                androidx.webkit.Profile prof = ProfileStore.getInstance().getProfile(PROFILE);
                if (prof != null) {
                    prof.getCookieManager().removeAllCookies(null);
                    prof.getCookieManager().flush();
                    prof.getWebStorage().deleteAllData();
                }
                try {
                    ProfileStore.getInstance().deleteProfile(PROFILE);
                } catch (IllegalStateException e) {
                    Log.i(TAG, "профиль ещё занят — данные стёрты, профиль останется");
                }
                wiped = true;
            } else {
                wiped = wipeShared();
            }
            prefs().edit().clear().apply();
            JSObject ret = new JSObject();
            ret.put("profile", separate ? "separate" : "shared");
            ret.put("wiped", wiped);
            call.resolve(ret);
        });
    }

    // Общий профиль (старый WebView): куки и хранилище только сайтов чатов
    private boolean wipeShared() {
        CookieManager cm = CookieManager.getInstance();
        String[] hosts = {"duck.ai", "duckduckgo.com", "chatgpt.com", "openai.com", "www.google.com",
            "google.com", "chat.deepseek.com", "deepseek.com"};
        for (String h : hosts) {
            String url = "https://" + h;
            String all = cm.getCookie(url);
            if (all != null) {
                for (String part : all.split(";")) {
                    String name = part.split("=", 2)[0].trim();
                    if (name.isEmpty()) continue;
                    cm.setCookie(url, name + "=; Max-Age=0; Path=/");
                    cm.setCookie(url, name + "=; Max-Age=0; Path=/; Domain=." + h.replaceFirst("^www\\.", ""));
                }
            }
            WebStorage.getInstance().deleteOrigin(url);
        }
        cm.flush();
        return true;
    }

    // ── Вспомогательное ──────────────────────────────────────────────────

    private void setState(String id, String state) {
        prefs().edit().putString("state." + id, state).putLong("stateAt." + id, System.currentTimeMillis()).apply();
    }

    private static String today() {
        return new SimpleDateFormat("yyyyMMdd", Locale.ROOT).format(new Date());
    }

    private int newChatsToday(String id) {
        return prefs().getInt("newchats." + id + "." + today(), 0);
    }

    private void bumpNewChats(String id) {
        SharedPreferences p = prefs();
        String k = "newchats." + id + "." + today();
        SharedPreferences.Editor e = p.edit();
        // Прошлые дни — убрать
        for (String old : p.getAll().keySet()) {
            if (old.startsWith("newchats." + id + ".") && !old.equals(k)) e.remove(old);
        }
        e.putInt(k, p.getInt(k, 0) + 1).apply();
    }

    private static String safe(String s) {
        return s == null ? "" : s.replaceAll("[^A-Za-z0-9_\\-]", "_");
    }

    private static JSObject parse(String v) {
        try {
            // evaluateJavascript отдаёт строку как JSON-литерал
            Object raw = new JSONTokener(v == null ? "null" : v).nextValue();
            return JSObject.fromJSONObject(new JSONObject(String.valueOf(raw)));
        } catch (Exception e) {
            return new JSObject();
        }
    }

    private interface MainT {
        Tab run();
    }

    private interface MainS {
        String run();
    }

    private Tab onMainT(MainT job) throws Exception {
        final Tab[] out = new Tab[1];
        final CountDownLatch latch = new CountDownLatch(1);
        main.post(() -> {
            try {
                out[0] = job.run();
            } finally {
                latch.countDown();
            }
        });
        if (!latch.await(10, TimeUnit.SECONDS) || out[0] == null) throw new Fail("failed", "вкладка не открылась");
        return out[0];
    }

    private String onMainS(MainS job) throws Exception {
        final String[] out = new String[1];
        final CountDownLatch latch = new CountDownLatch(1);
        main.post(() -> {
            try {
                out[0] = job.run();
            } finally {
                latch.countDown();
            }
        });
        latch.await(5, TimeUnit.SECONDS);
        return out[0];
    }

    private void onMain(Runnable r) {
        main.post(r);
    }
}
