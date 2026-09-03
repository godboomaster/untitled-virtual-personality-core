"""Веб-чат LLM как провайдер без API-ключей («webchat»).

Промпт уходит в чат (deepseek/qwen/claude/zai/chatgpt/kimi) в браузере
пользователя (Chromium — через CDP-механику computer_control; Safari на
macOS — через Apple Events: служебная вкладка видимая, Enter — System
Events): ОДИН постоянный чат на
сайт НА КАНАЛ НА КОНТЕКСТ (персону) —
после первого сообщения сайт переводит вкладку на постоянный URL чата,
он запоминается в состоянии и переиспользуется (свежий чат открывается,
только если сохранённый сломался: удалён/разлогинен). Каналы: у основных
ответов ("main") и побочных задач вроде LTM ("side") — РАЗНЫЕ чаты и
раздельные квоты, чтобы фоновые задачи не замусоривали контекст беседы.
Контексты: состояние лежит в data/{context}/computer_control, поэтому у
каждой персоны свой чат — иначе все персоны писали бы в один разговор
и модель видела бы чужие реплики (утечка памяти между персонами).
Быстрый ввод fill() (посимвольный набор для системного промпта занял бы
минуты), Enter, опрос DOM до конца стриминга (ждём НОВЫЙ блок ответа
относительно счётчика до отправки + текст стабилен несколько замеров
подряд). Пейсинг и оконная квота (дефолт 40/час, сброс каждый час — на
персону настраивается, снимается) — защита аккаунта от машинного паттерна.
Любая неудача — None: роутер идёт по фолбэк-цепочке дальше (local/API).

Непрерывный чат — осознанное решение: веб-чат копит контекст беседы (это
второй, неконтролируемый слой памяти рядом с STM/LTM бота), зато не
плодятся сотни чатов-однодневок и модель видит недавние реплики. Сброс —
удалить chat_url из web_llm_state.json (или сам чат на сайте).

Ограничения честно: ToS веб-чатов автоматизацию не приветствует (риск
флага аккаунта смягчается квотой/пейсингом, не устраняется), селекторы
адаптеров могут сломаться редизайном — smoke-ping ловит это заранее.
"""

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MIN_INTERVAL_SEC = 5.0      # минимум между вызовами (пейсинг)
QUOTA_PER_HOUR = 40         # дефолтный потолок вызовов в час (на сайт+канал)
QUOTA_WINDOW_HOURS = 1      # окно квоты: ёмкость = per_hour × окно, затем полный сброс
ANSWER_TIMEOUT_SEC = 150.0  # максимум ожидания ответа (длинные дневники!)
POLL_SEC = 2.5              # шаг опроса ответа
STABLE_POLLS = 2            # столько одинаковых непустых замеров подряд = конец стриминга
FRESH_CHAT_SETTLE_SEC = 2.0  # пауза после навигации на home (новый чат)
SEND_VERIFY_SEC = 8.0        # сколько ждём появления своего сообщения в ленте

# Тексты-заглушки во время «думания» — ответом не считаются
_THINKING_NOISE = {"", "thinking…", "thinking", "thinking completed",
                   "думаю…", "думаю", "reasoning…"}


class _ChatBroken(Exception):
    """Сайт вернул ошибку вместо ответа (битый/удалённый чат, разлогин)."""


# Баннеры ошибок веб-чатов в ленте (qwen: «Oops! There was an issue
# connecting to … Invalid input chat parent_id … is not exist» — чат
# удалён/побит, ответа не будет). Намеренно узкие паттерны — человеческий
# ответ с «oops» в середине не задеть. Матчатся и с текстом последнего
# answer-блока, и с текстом последнего контейнера error_scope (qwen
# рендерит баннер без content-классов — виден только на контейнере).
_CHAT_ERROR_RES = (
    re.compile(r"^oops!\s*there was an issue", re.I),
    re.compile(r"parent_id\s+\S+\s+is not exist", re.I),
    # deepseek: чат упёрся в лимит длины — сайт просит начать новый;
    # баннер живёт отдельным div с хэш-классом (не в блоках ответа),
    # ловим и текстом ответа, и пробой по странице (см. ниже)
    re.compile(r"length limit reached.*start a new chat", re.I),
)

# Текст последнего видимого контейнера error_scope (аргумент — селектор).
_ERROR_SCOPE_JS = (
    "(function(){var els=document.querySelectorAll(%s);"
    "for(var i=els.length-1;i>=0;i--){var e=els[i];"
    "var r=e.getBoundingClientRect();var st=getComputedStyle(e);"
    "if(st.display==='none'||st.visibility==='hidden'||r.width<2||r.height<2)continue;"
    "return (e.innerText||'').replace(/\\s+/g,' ').trim();}"
    "return '';})()"
)

# Баннер ошибки ОТДЕЛЬНЫМ элементом страницы (не в блоках ответа и не в
# error_scope): deepseek «Length limit reached…» — короткий видимый текст
# в div с хэш-классом; сканим видимые «листья» без привязки к вёрстке
_PAGE_BANNER_ERROR_JS = (
    "(function(){var rx=__RX__;"
    "var els=document.querySelectorAll('div,span,p');"
    "for(var i=0;i<els.length;i++){var e=els[i];"
    "if(e.children.length>2)continue;"
    "var r=e.getBoundingClientRect();var st=getComputedStyle(e);"
    "if(st.display==='none'||st.visibility==='hidden'||r.width<2||r.height<2)continue;"
    "var t=(e.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(!t||t.length>200)continue;"
    "if(rx.test(t))return t;}"
    "return '';})()"
)

# JS переключения режима чата qwen: Auto → Fast (быстрее, и в ленте нет
# блоков «Thinking completed»). Идемпотентно: режим не Auto — ничего не
# делает. evaluate в playwright ждёт промисы — возвращаем Promise.
_QWEN_FAST_MODE_JS = (
    "(function(){"
    "function vis(e){var s=getComputedStyle(e);var r=e.getBoundingClientRect();"
    "return s.display!=='none'&&s.visibility!=='hidden'&&r.width>2&&r.height>2;}"
    "var trig=document.querySelector('.qwen-thinking-selector [class*=trigger]');"
    "if(!trig)return 'no-trigger';"
    "var cur=(trig.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(!/auto/i.test(cur))return 'ok:'+cur;"
    "trig.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));trig.click();"
    "return new Promise(function(res){setTimeout(function(){"
    "var items=document.querySelectorAll('.qwen-chat-v2-dropdown-menu-item');"
    "for(var i=0;i<items.length;i++){var e=items[i];if(!vis(e))continue;"
    "var t=(e.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(/fast|быстр/i.test(t)){"
    "e.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));e.click();"
    "res('ok:fast');return;}}"
    "res('no-fast-item');},700);});"
    "})()"
)

ADAPTERS = {
    "deepseek": {
        "host": "chat.deepseek.com",
        "home": "https://chat.deepseek.com/",
        "input": "textarea",
        "answer": [".ds-assistant-message-main-content",
                   ".ds-markdown"],
        # Своё сообщение: ds-message БЕЗ assistant-контента внутри (класса
        # «user» нет; лента виртуализована — в DOM только последний обмен,
        # поэтому подтверждение отправки текстовое, а не по счётчику).
        "user": [".ds-message:not(:has(.ds-assistant-message-main-content))"],
    },
    "qwen": {
        "host": "chat.qwen.ai",
        "home": "https://chat.qwen.ai/",
        "input": "textarea.message-input-textarea",
        "answer": [".qwen-chat-message-assistant .response-message-content.phase-answer",
                   ".qwen-chat-message-assistant .chat-response-message"],
        "user": [".qwen-chat-message-user"],
        # Баннер ошибки («Oops! … parent_id … is not exist») рендерится
        # ВНУТРИ assistant-контейнера, но БЕЗ content-классов — селекторы
        # answer его не видят, поэтому текст ошибки читаем с последнего
        # контейнера целиком (см. _wait_answer).
        "error_scope": ".qwen-chat-message-assistant",
        "mode_js": _QWEN_FAST_MODE_JS,
        # Сайт принимает картинки вставкой (проверено Cmd+V вручную):
        # включает vision-фолбэк через веб-чат (chat_paste_image)
        "images": True,
    },
    "claude": {
        "host": "claude.ai",
        "home": "https://claude.ai/new",
        "input": "div[contenteditable=true]",
        # Редизайн 08.2026: .font-claude-response-body — теперь класс каждого
        # абзаца <p> внутри ответа, а не контейнер всего сообщения. Читатели
        # берут последний совпавший блок — до пользователя доходил один
        # последний абзац (кейс: ответ по лору схлопнулся в «Вы хорошо
        # отдохнули сегодня?»). Контейнер всего ответа — div.font-claude-response
        # (один на сообщение ассистента); старый селектор — фолбэк.
        # Внутри контейнера лежит пилюля мышления («Thought for 6s»): видимый
        # текст — кнопка (md() её пропускает), но рядом скрытый span.sr-only
        # для скринридеров — его текст утекал в ответ. Срезаем все .sr-only.
        "answer": ["div.font-claude-response",
                   ".font-claude-response-body"],
        "answer_exclude": ".sr-only",
        "user": ["[data-testid=user-message]"],
    },
    "zai": {
        "host": "chat.z.ai",
        "home": "https://chat.z.ai/",
        "input": "textarea#chat-input",
        # Контент ответа — внутренний .markdown-prose в #response-content-container;
        # снаружи лежит .thinking-chain-container («Thought Process») — вырезаем
        # exclude'ом: md() не смотрит на видимость, свёрнутая цепочка иначе
        # попадала бы в текст.
        "answer": [".chat-assistant #response-content-container > .markdown-prose",
                   ".chat-assistant.markdown-prose"],
        "answer_exclude": ".thinking-chain-container",
        "user": [".user-message"],
    },
    "chatgpt": {
        "host": "chatgpt.com",
        "home": "https://chatgpt.com/",
        "input": "#prompt-textarea",
        "answer": ["[data-message-author-role=assistant] .markdown",
                   "[data-message-author-role=assistant]"],
        "user": ["[data-message-author-role=user]"],
    },
    "kimi": {
        "host": "kimi.ai",
        "home": "https://www.kimi.ai/",
        "input": "div.chat-input-editor",
        # Ответ — прямой markdown-контейнер content-box; цепочка «Think»
        # лежит в .thinking-container (не direct child) — основной селектор
        # её не видит, exclude — страховка на случай сдвига вёрстки.
        # Фолбэк-селектор :not(:has(.user-content)) — чтобы не зацепить
        # box user-сообщения (у него та же обёртка, но без markdown-container).
        "answer": [".segment-content-box > .markdown-container",
                   ".segment-content-box:not(:has(.user-content))"],
        "answer_exclude": ".thinking-container",
        # Ряд кнопок действий (copy/…) появляется только у ЗАВЕРШЁННОГО
        # ответа — гейт против преждевременного «стабильного» плейсхолдера
        # при сбойной/оборванной генерации (кейс: «High demand…» → пусто).
        "done_selector": ".segment-assistant-actions",
        "user": [".user-content"],
    },
}


def extract_json(text: str):
    """Терпимый разбор ответа веб-чата как JSON: срез ```json-ограждений,
    затем первая сбалансированная {…}/[…] конструкция. None — не JSON."""
    t = str(text or "").strip()
    if not t:
        return None
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(t)):
            ch = t[i]
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except ValueError:
                        break
    try:
        return json.loads(t)
    except ValueError:
        return None


class WebChatLLM:
    """LLM через веб-чат в браузере пользователя. site — ключ ADAPTERS."""

    def __init__(self, site: str, context: str = "default",
                 base_dir: Optional[Path] = None, channel: str = "main",
                 quota_per_hour: Optional[int] = QUOTA_PER_HOUR):
        if site not in ADAPTERS:
            raise ValueError(f"неизвестный webchat-сайт «{site}» "
                             f"(есть: {', '.join(ADAPTERS)})")
        self.site = site
        self.adapter = ADAPTERS[site]
        # Канал: у побочных задач ("side") свой чат и своя квота — фоновая
        # активность не замусоривает контекст основной беседы
        self.channel = (channel or "main").strip() or "main"
        self._state_key = site if self.channel == "main" \
            else f"{site}#{self.channel}"
        # Лимит вызовов в час (None — снят, остаётся только пейсинг).
        # Персона переопределяет через llm.webchat_limits (роутер передаёт).
        self.quota_per_hour = quota_per_hour
        self.base_dir = base_dir or Path(f"data/{context}/computer_control")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self.base_dir / "web_llm_state.json"
        self._tab_id: Optional[int] = None  # наша служебная вкладка (реестр CDP)
        # Вкладки веб-чатов — служебные: они не попапы кликов и не «крайняя
        # страница» для команд (browser_actions исключает _SERVICE_HOSTS)
        try:
            from app.features import browser_actions as _ba
            _ba.register_service_host(self.adapter.get("host"))
        except Exception:
            pass
        # Сериализация вызовов: вкладка чата одна — параллельные запросы
        # (ответ в чате + фоновые задачи вроде LTM) иначе ломали бы друг
        # другу DOM (навигация/ввод/чтение посреди чужого цикла)
        self._lock = threading.Lock()

    # ── состояние: квота, пейсинг, адрес постоянного чата ──
    # Файл общий для всех сайтов контекста: {"sites": {site: {...}}}

    def _load_state(self) -> dict:
        """Состояние ЭТОГО сайта+канала (квота/пейсинг/chat_url); {} при старте."""
        try:
            st = json.loads(self._state_path.read_text(encoding="utf-8"))
            site_st = st.get("sites", {}).get(self._state_key, {})
            return site_st if isinstance(site_st, dict) else {}
        except Exception:
            return {}

    def _save_state(self, patch: dict):
        """Слить patch в состояние своего сайта+канала, остальные не трогать."""
        try:
            try:
                st = json.loads(self._state_path.read_text(encoding="utf-8"))
            except Exception:
                st = {}
            sites = st.setdefault("sites", {})
            cur = sites.get(self._state_key)
            if not isinstance(cur, dict):
                cur = {}
            cur.update(patch)
            sites[self._state_key] = cur
            self._state_path.write_text(
                json.dumps(st, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug(f"[WebChat] состояние не записано: {e}")

    def set_quota(self, per_hour: Optional[int]):
        """Сменить лимит на живую: вызовов в час; None — снять (только пейсинг)."""
        self.quota_per_hour = None if per_hour is None else max(1, int(per_hour))

    def _quota_capacity(self) -> Optional[int]:
        """Ёмкость окна квоты (per_hour × QUOTA_WINDOW_HOURS); None — без лимита."""
        if self.quota_per_hour is None:
            return None
        return max(1, int(self.quota_per_hour)) * QUOTA_WINDOW_HOURS

    def _quota_check(self) -> bool:
        """True — можно вызывать; оконный счётчик и метка последнего вызова.
        Окно QUOTA_WINDOW_HOURS часов: ёмкость исчерпана — ждём его конца,
        потом счётчик обнуляется полностью (не скользящее окно)."""
        st = self._load_state()
        cap = self._quota_capacity()
        if cap is not None:
            now = time.time()
            start = float(st.get("window_start") or 0)
            count = int(st.get("count", 0)) \
                if now - start < QUOTA_WINDOW_HOURS * 3600 else 0
            if count >= cap:
                left = int((start + QUOTA_WINDOW_HOURS * 3600 - now) / 60) + 1
                logger.warning(f"[WebChat] {self.site}: квота "
                               f"{self.quota_per_hour}/ч исчерпана — "
                               f"сброс через ~{left} мин")
                return False
        last = float(st.get("last_ts") or 0)
        wait = MIN_INTERVAL_SEC - (time.time() - last)
        if wait > 0:
            time.sleep(wait)  # пейсинг: не чаще раза в MIN_INTERVAL_SEC
        return True

    def _quota_bump(self):
        now = time.time()
        st = self._load_state()
        start = float(st.get("window_start") or 0)
        if now - start >= QUOTA_WINDOW_HOURS * 3600:
            start, count = now, 0
        else:
            count = int(st.get("count", 0))
        self._save_state({"window_start": start, "count": count + 1,
                          "last_ts": now})

    # ── постоянный чат (URL запоминается после первого сообщения) ──

    def _chat_url(self) -> Optional[str]:
        url = str(self._load_state().get("chat_url") or "").strip()
        return url or None

    def _remember_chat_url(self, url: str) -> bool:
        """Запомнить постоянный адрес чата: после первого сообщения сайт
        переводит вкладку с home на URL конкретного чата. True — адрес
        постоянный (запомнен или уже был таким), False — ещё «новый чат»."""
        if not url or self.adapter["host"] not in url:
            return False
        if url.rstrip("/") == self.adapter["home"].rstrip("/"):
            return False  # всё ещё home — чат не создан
        last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
        if last in ("new", "new-chat", "new_chat"):
            return False  # страница «нового чата», не постоянный адрес
        if url != self._chat_url():
            self._save_state({"chat_url": url})
            logger.info(f"[WebChat] {self.site}: постоянный чат {url[:70]}")
        return True

    def _capture_chat_url(self, host: str, tab_id: int):
        """Запомнить постоянный URL чата после ответа: сайт присваивает его
        не мгновенно (qwen — к концу ответа), поэтому опрашиваем несколько
        секунд, пока вкладка не уедет со страницы «нового чата»."""
        from app.features import browser_actions as ba
        for _ in range(4):
            try:
                url = ba.tab_url(host, tab_id)
            except Exception:
                return  # вкладка умерла — в следующий раз откроем home
            if not url or self._remember_chat_url(url):
                return
            time.sleep(1.5)

    # ── вкладка и постоянный чат ──

    @staticmethod
    def _same_page(a: str, b: str) -> bool:
        """URL ведут на одну страницу (хост+путь; query и слэш в конце неважны)."""
        try:
            pa, pb = urlsplit(a), urlsplit(b)
            return (pa.netloc, pa.path.rstrip("/")) == \
                   (pb.netloc, pb.path.rstrip("/"))
        except Exception:
            return False

    def _after_nav(self, ba):
        """После навигации/открытия вкладки: дождаться поля ввода (первая
        загрузка чата рендерится дольше settle-паузы — без этого отправка
        падала и вызов уезжал в облачный fallback) и применить режим чата
        (mode_js адаптера — например, qwen Auto→Fast; идемпотентно)."""
        try:
            ba.wait_input(None, self._tab_id, self.adapter["input"],
                          timeout_sec=8.0)
        except Exception:
            pass  # closed-loop отправки сам отловит неготовность поля
        mode_js = self.adapter.get("mode_js")
        if mode_js:
            try:
                out = ba.eval_js(None, self._tab_id, mode_js)
                logger.debug(f"[WebChat] {self.site}#{self.channel}: режим чата: {out}")
            except Exception as e:
                logger.debug(f"[WebChat] {self.site}#{self.channel}: "
                             f"режим не переключён: {e}")

    def _ensure_chat(self, fresh: bool = False) -> Optional[int]:
        """Служебная вкладка сайта на «нашем» чате (сохранённый chat_url;
        fresh=True — принудительно home, т.е. новый чат). Навигация — только
        если вкладка ещё не там, без лишних перезагрузок страницы.
        None — не удалось (браузер недоступен). → tab_id|None"""
        from app.features import browser_actions as ba
        target = self.adapter["home"] if fresh \
            else (self._chat_url() or self.adapter["home"])
        try:
            if self._tab_id is not None:
                try:
                    if not self._same_page(ba.tab_url(tab_id=self._tab_id), target):
                        ba.navigate_tab(target, tab_id=self._tab_id)
                        time.sleep(FRESH_CHAT_SETTLE_SEC)
                        self._after_nav(ba)
                    return self._tab_id
                except Exception:
                    self._tab_id = None  # вкладку закрыли — откроем новую
            self._tab_id = ba.open_new_tab(target, background=True)
            time.sleep(FRESH_CHAT_SETTLE_SEC)
            self._after_nav(ba)
            return self._tab_id
        except Exception as e:
            logger.info(f"[WebChat] {self.site}: вкладка чата не открылась: {e}")
            return None

    # ── главный вызов ──

    def _send_verified(self, ba, host: str, tab_id: int, prompt: str):
        """chat_fill_send + подтверждение, что сообщение РЕАЛЬНО появилось
        в ленте (последний user-блок содержит наш текст). Иначе hydration-
        гонка на свеженавигированной странице обнуляет контролируемое
        React-поле до отправки, closed-loop «поле очистилось» ложно
        срабатывает, и мы 150с ждём ответ на никогда не отправленное
        сообщение (кейс 22.08). Одна повторная отправка — к тому времени
        страница уже прогрета. Без user-селектора у адаптера — как раньше.
        Проверка текстовая, а не по счётчику: лента deepseek виртуализована
        (старый обмен уходит из DOM — счётчик не растёт).
        Возвращает marker (нормализованное начало промпта) — якорь для
        _wait_answer; None — у адаптера нет user-селекторов."""
        user_sels = self.adapter.get("user")
        if not user_sels:
            ba.chat_fill_send(host, tab_id, self.adapter["input"], prompt)
            return None
        want = " ".join(prompt[:80].split()).lower()
        for attempt in (1, 2):
            ba.chat_fill_send(host, tab_id, self.adapter["input"], prompt)
            deadline = time.time() + SEND_VERIFY_SEC
            while time.time() < deadline:
                try:
                    last = ba.last_block_text(host, tab_id, user_sels)
                except Exception:
                    last = ""
                if want and want in " ".join(last.split()).lower():
                    return want
                time.sleep(0.7)
            logger.info(f"[WebChat] {self.site}#{self.channel}: сообщение не "
                        f"появилось в ленте (попытка {attempt})")
        raise TimeoutError("сообщение не появилось в ленте после отправки")

    def _restart_stuck_browser(self, ba, cause: str):
        """Перезапуск браузера бота при залипшей отправке (поле не очистилось
        после Enter / сообщение не появилось в ленте после двух попыток):
        страница в состоянии, которое не лечится навигацией на чат. Вкладка
        умерла вместе с браузером — забываем её (chat_url храним: чат на
        сайте жив, откроется в новой вкладке). Текущий вызов честно уходит
        в фолбэк, свежий браузер подхватит следующий."""
        self._tab_id = None
        try:
            if ba.restart_browser(reason=f"webchat {self.site}: {cause}"):
                logger.info(f"[WebChat] {self.site}: браузер бота "
                            "перезапущен — следующий вызов откроет свежую "
                            "вкладку")
        except Exception as e:
            logger.warning(f"[WebChat] {self.site}: перезапуск браузера "
                           f"не удался: {e}")

    def get_response(self, messages: list, temperature: float = 0.7,
                     max_tokens: int = 2000, top_p: float = 0.9,
                     timeout: float = 60.0) -> Optional[str]:
        """messages как у OpenAI-роутера → текст ответа или None (фолбэк
        вызывающего). system-части склеиваются вводным блоком инструкций.
        temperature/max_tokens/top_p приняты для совместимости сигнатуры с
        API-провайдерами, но НЕ действуют: веб-чаты не дают выставить их на
        запрос — модель отвечает с дефолтами сайта."""
        with self._lock:
            return self._get_response_locked(messages, temperature, max_tokens,
                                             top_p, timeout)

    def get_response_with_image(self, prompt: str, image_bytes: bytes,
                                timeout: float = 150.0,
                                image_mime: str = "image/png") -> Optional[str]:
        """Текст + картинка (vision-фолбэк резолва): изображение вставляется
        в композер синтетическим paste, буфер обмена пользователя не
        трогаем. Только для сайтов с adapter["images"] (проверено, что сайт
        paste принимает). image_mime — реальный тип байтов (jpeg легче png
        для аплоада; имя/тип File в paste-событии ставится по нему).
        None — сайт без картинок / вставка или ответ не удались (честный
        фолбэк вызывающего)."""
        if not self.adapter.get("images") or not image_bytes:
            return None
        with self._lock:
            return self._get_response_locked(
                [{"role": "user", "content": prompt}], 0.7, 2000, 0.9,
                timeout, image_bytes=image_bytes, image_mime=image_mime)

    def _get_response_locked(self, messages: list, temperature: float,
                             max_tokens: float, top_p: float,
                             timeout: float,
                             image_bytes: Optional[bytes] = None,
                             image_mime: str = "image/png"
                             ) -> Optional[str]:
        from app.features import browser_actions as ba
        prompt = self._join_messages(messages)
        if not prompt:
            return None
        if not self._quota_check():
            return None
        host = self.adapter["host"]
        answer = None
        for fresh in (False, True):
            tab_id = self._ensure_chat(fresh=fresh)
            if tab_id is None:
                return None
            marker = None
            try:
                excl = self.adapter.get("answer_exclude")
                baseline = ba.count_blocks(host, tab_id, self.adapter["answer"])
                baseline_text = ba.last_block_text(host, tab_id,
                                                   self.adapter["answer"],
                                                   markdown=True,
                                                   exclude=excl)
                if image_bytes:
                    # Картинка — ДО текста: композер пуст, фокус чистый;
                    # сайт вставку не подтвердил — нет смысла слать голый
                    # текст, честный фолбэк (и квоту не тратим)
                    if not ba.chat_paste_image(host, tab_id,
                                               self.adapter["input"],
                                               image_bytes,
                                               mime=image_mime):
                        logger.warning(f"[WebChat] {self.site}: картинка не "
                                       "прикрепилась — запрос без ответа")
                        return None
                marker = self._send_verified(ba, host, tab_id, prompt)
            except Exception as e:
                if not fresh and self._chat_url():
                    # Сохранённый чат сломался (удалён/разлогинен) — свежий
                    logger.info(f"[WebChat] {self.site}: сохранённый чат не "
                                f"принял ввод ({e}) — уходим на новый")
                    self._save_state({"chat_url": ""})
                    continue
                logger.warning(f"[WebChat] {self.site}: отправка не удалась: {e}")
                # Страница залипла в состоянии, которое не чинится
                # навигацией (двойная неудача закрытого цикла) — лечится
                # только пересозданием: перезапускаем браузер бота
                self._restart_stuck_browser(ba, str(e))
                return None
            self._quota_bump()
            # Ответ уже ушёл в чат — повторной отправки не делаем, даже
            # если ожидание оборвётся таймаутом (во избежание дублей)
            try:
                answer = self._wait_answer(host, tab_id,
                                           timeout=max(timeout, ANSWER_TIMEOUT_SEC),
                                           baseline=baseline,
                                           baseline_text=baseline_text,
                                           marker=marker)
            except _ChatBroken as e:
                if not fresh and self._chat_url():
                    # Сохранённый чат сломан НА САЙТЕ (удалён/битый parent_id)
                    # — сбрасываем адрес и уходим на свежий чат
                    logger.info(f"[WebChat] {self.site}: {e} — уходим на новый чат")
                    self._save_state({"chat_url": ""})
                    continue
                logger.warning(f"[WebChat] {self.site}: {e}")
                return None
            self._capture_chat_url(host, tab_id)
            break
        if answer:
            logger.info(f"[WebChat] {self.site}: ответ {len(answer)} симв.")
        return answer

    @staticmethod
    def _join_messages(messages: list) -> str:
        """OpenAI-messages → один текст для веб-чата: system — блоком
        инструкций в начале, дальше реплики с префиксами ролей."""
        sys_parts: List[str] = []
        convo: List[str] = []
        for m in messages or []:
            content = m.get("content")
            if not isinstance(content, str) or not content.strip():
                continue  # мультимодальные куски веб-чату не отдать
            role = m.get("role")
            if role == "system":
                sys_parts.append(content.strip())
            else:
                prefix = {"user": "Пользователь",
                          "assistant": "Ассистент"}.get(role)
                convo.append(f"{prefix}: {content.strip()}" if prefix
                             else content.strip())
        parts = []
        if sys_parts:
            parts.append("Инструкции (соблюдай строго, не пересказывай):\n"
                         + "\n\n".join(sys_parts))
        parts.extend(convo)
        return "\n\n".join(parts).strip()

    def _wait_answer(self, host: str, tab_id: int, timeout: float,
                     baseline: int = 0, baseline_text: str = "",
                     marker: Optional[str] = None) -> Optional[str]:
        """Опрос ответа до конца стриминга. Два режима детекции новизны:

        1. Якорный (marker задан, у адаптера есть user-селекторы): новизна =
           блоки ответа ПОСЛЕ нашего user-сообщения в DOM (answer_blocks_after).
           Не зависит от прогретости страницы на момент baseline — без него
           при медленном рендере истории SPA старый завершённый ответ
           выглядел «новым» и возвращался вместо настоящего (кейс 22.08:
           реформулировка coref уходила пользователю как ответ). Якорь не
           найден (лента виртуализована и съела своё сообщение?) — разовый
           откат на baseline-режим.
        2. Baseline: новый ответ = блоков стало БОЛЬШЕ baseline (непрерывный
           чат: прошлые ответы уже в DOM — ждём именно новый) ИЛИ текст
           последнего блока СМЕНИЛСЯ относительно досылочного: сайты с
           виртуализацией ленты (deepseek держит в DOM только последний
           обмен) не наращивают счётчик — по одному числу блоков ответ не
           отличить от старого.

        Текст непустой и стабилен STABLE_POLLS замеров подряд.
        None по таймауту — честный фолбэк."""
        from app.features import browser_actions as ba
        sels = self.adapter["answer"]
        # Пробник баннера ошибки по контейнеру error_scope (см. ADAPTERS):
        # qwen рендерит «Oops!…» без content-классов — блоки ответа его не
        # видят, без пробника битый чат = вечный таймаут без самолечения.
        err_js = (_ERROR_SCOPE_JS % json.dumps(self.adapter["error_scope"])
                  if self.adapter.get("error_scope") else None)
        # Проба баннера-ошибки по странице (deepseek length limit): div с
        # хэш-классом вне блоков ответа — строим JS-регекс из _CHAT_ERROR_RES
        banner_js = _PAGE_BANNER_ERROR_JS.replace(
            "__RX__", "/length limit reached.{0,40}start a new chat/i")
        deadline = time.time() + timeout
        base_norm = " ".join((baseline_text or "").split()).strip().lower()
        anchored = bool(marker and self.adapter.get("user"))
        excl = self.adapter.get("answer_exclude")
        prev = None
        stable = 0
        err_seen = 0
        while time.time() < deadline:
            n, cur, cnt, done = 0, "", None, False
            try:
                if anchored:
                    cnt, cur, done = ba.answer_blocks_after(
                        host, tab_id, self.adapter["user"], sels,
                        marker, markdown=True, exclude=excl,
                        done_selector=self.adapter.get("done_selector"))
                    if cnt is None:
                        anchored = False  # якорь не нашёлся — baseline-путь
                if not anchored:
                    n = ba.count_blocks(host, tab_id, sels)
                    cur = ba.last_block_text(host, tab_id, sels, markdown=True,
                                             exclude=excl)
                    done = True  # baseline-путь: маркера завершения нет
            except Exception:
                n, cur, cnt, done = 0, "", None, False
            scope_txt = ""
            if err_js:
                try:
                    scope_txt = ba.eval_js(host, tab_id, err_js)
                except Exception:
                    scope_txt = ""
            banner_txt = ""
            try:
                banner_txt = ba.eval_js(host, tab_id, banner_js)
            except Exception:
                banner_txt = ""
            cur_norm = " ".join(cur.split()).strip().lower()
            scope_norm = " ".join(scope_txt.split()).strip().lower()
            banner_norm = " ".join(banner_txt.split()).strip().lower()
            # Баннер ошибки сайта (битый чат): два подряд замера — не путаем
            # с мимолётным состоянием стриминга
            err_hit = next((t for t in (cur_norm, scope_norm, banner_norm)
                            if t and any(rx.search(t)
                                         for rx in _CHAT_ERROR_RES)), None)
            if err_hit is not None:
                err_seen += 1
                if err_seen >= 2:
                    raise _ChatBroken(f"сайт вернул ошибку: {err_hit[:100]}")
            else:
                err_seen = 0
            is_new = bool(cnt) if anchored else \
                (n > baseline or (bool(cur_norm) and cur_norm != base_norm))
            # done — маркер завершения генерации (кнопки действий и т.п.):
            # без него плейсхолдер сбойной генерации залипал как «ответ»
            if cur_norm and cur_norm not in _THINKING_NOISE and is_new and done:
                if cur == prev:
                    stable += 1
                    if stable >= STABLE_POLLS:
                        return cur
                else:
                    stable = 0
                    prev = cur
            time.sleep(POLL_SEC)
        logger.warning(f"[WebChat] {self.site}: ответ не дождались за "
                       f"{int(timeout)}с")
        return None
