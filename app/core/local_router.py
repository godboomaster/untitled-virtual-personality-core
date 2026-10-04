"""
Локальный LLM роутер: лёгкие служебные вызовы (классификации, извлечение,
сжатие) — полный список задач в LOCAL_TASKS.

Движок задачи выбирается на персону (досье → настройки, «Движок локальных
задач»; YAML персоны, llm.local_tasks): «ollama» или «webchat» — тогда вызов
уходит в веб-чат через WebChatLLM (канал side: отдельный чат и квота, чтобы
не замусоривать контекст основной беседы). Без явного выбора — по роду
задачи: задачи на пути ответа (ответ ждут прямо в разговоре) идут в Ollama,
фоновые (BACKGROUND_TASKS) — в веб-чат: первый веб-чат fallback-цепочки
персоны после основного, запасной — основной веб-чат персоны (тоже канал
side). Веб-чаты не ответили — мягкий откат на Ollama, если она доступна. OCR
остаётся только за Ollama: веб-чату не отдать картинку.

Роутер один на процесс (Ollama и вкладки side общие), персона задаёт только
выбор движков: get_local_router(context) — вид роутера, привязанный к
персоне (bind_persona). Без привязки — дефолт LOCAL_LLM_BACKEND из env
(ollama) для всех задач.
"""

import logging
import os
import threading
import time
from typing import Optional

import httpx

from app.core.config import OLLAMA_MODEL
from app.core.language import detect_language, user_language_line

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_TIMEOUT = 15.0

# Все задачи локального движка (полный список потребителей Ollama).
# Значение — ollama_only: задача технически не может уйти в веб-чат.
# Аддоны добавляют свои задачи через register_task.
LOCAL_TASKS: dict[str, bool] = {
    "query_rewrite": False,        # рерайтер/улучшатель поисковых запросов
    "self_memory": False,          # заметки в личный дневник
    "state_engine": False,         # тики состояния + оценка инициативы
    "world_engine": False,         # мир: офлайн-события, фильтр стимулов (NPC из диалога — dialogue_harvest)
    "offline_summary": False,      # сжатие офлайн-дневника
    "proactive_prefilter": False,  # префильтр проактивных инициатив
    "intent_router": False,        # арбитр намерений (TODO/инвентарь)
    "todo_cleanup": False,         # очистка текста задач
    "rule_extract": False,         # извлечение правил из реплик
    "inventory_enrich": False,     # описания предметов инвентаря
    "learning_intent": False,      # детект «хочу учиться»
    "learning": False,             # уроки на пути ответа: «продолжим?», старт курса
    "learning_lesson": False,      # плановый урок в фоне: тема и словарь
    "help_detect": False,          # детект просьб о помощи
    "dossier": False,              # анализ досье (fallback без основного роутера)
    "dialogue_harvest": False,     # общий урожай диалога: NPC + mood + моменты/позиции
    "room_placement": False,       # комната в вебе: где стоит новый предмет инвентаря
    "ocr": True,                   # текст с картинок — только Ollama (vision)
}

# Фоновые задачи: их ответа никто не ждёт в разговоре — по умолчанию веб-чат
# (канал side). Остальные идут на пути ответа пользователю — по умолчанию
# Ollama: тёплая модель отвечает за доли секунды, веб-чат side — от ~10 с
# (шаг опроса POLL_SEC × STABLE_POLLS). offline_summary формально на пути
# ответа (контекст первой реплики после отсутствия ≥12 ч), но редкий — в фоне
# по выбору: лишние секунды раз в полдня дешевле, чем держать Gemma.
BACKGROUND_TASKS: set[str] = {
    "self_memory", "state_engine", "world_engine", "offline_summary",
    "proactive_prefilter", "dossier", "dialogue_harvest",
    "room_placement",
    "learning_lesson",
}


def register_task(name: str, ollama_only: bool = False,
                  background: bool = False) -> None:
    # Задача аддона: видна в настройках движков, её движок можно выбрать
    LOCAL_TASKS[name] = bool(ollama_only)
    if background:
        BACKGROUND_TASKS.add(name)
    else:
        BACKGROUND_TASKS.discard(name)


# Бюджет ожидания очереди фона веб-чата (канал side) для локальных задач:
# занято дольше — следующий сайт/откат на Ollama (см. get_response). Столько
# же, сколько основной роутер ждёт перед уходом в burst (BURST_LOCK_WAIT_SEC).
LOCAL_WEBCHAT_QUEUE_WAIT_SEC = 3.0

# Веб-чат фоновых задач персоны (llm.local_tasks.bg_site): «fallback» —
# первый веб-чат fallback-цепочки после основного (дефолт), «primary» —
# основной веб-чат персоны, «rotate» — сайты по очереди из
# llm.local_tasks.rotate ({сайт: сколько вопросов подряд}; 01.10 — duck.ai и
# Google AI Mode, чтобы нагрузка на каждый сайт была меньше), иначе имя
# сайта. Запасной — основной веб-чат (или первый fallback, если выбран сам
# основной); у «rotate» — следующий сайт очереди.
BG_SITE_MODES = ("fallback", "primary", "rotate")
DEFAULT_BG_SITE = "fallback"
ROTATE_MAX = 20  # вопросов подряд одному сайту — не больше


def normalize_local_tasks_cfg(cfg) -> tuple[dict, str]:
    """llm.local_tasks из YAML → ({задача: {"backend", "site"?}}, bg_site).
    Мусорные записи отбрасываются: неизвестный движок, сайт вне ADAPTERS.
    Задача не из LOCAL_TASKS (снятая, как relationship, или аддон ещё не
    зарегистрирован) остаётся в записи, но не читается: резолв и снимок
    идут по LOCAL_TASKS."""
    from app.features.web_llm import ADAPTERS
    cfg = cfg if isinstance(cfg, dict) else {}
    bg = str(cfg.get("bg_site") or DEFAULT_BG_SITE).strip().lower()
    if bg not in BG_SITE_MODES and bg not in ADAPTERS:
        bg = DEFAULT_BG_SITE
    tasks: dict = {}
    raw = cfg.get("tasks") if isinstance(cfg.get("tasks"), dict) else {}
    for task, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        backend = str(entry.get("backend") or "").strip().lower()
        if backend not in ("ollama", "webchat"):
            continue
        site = str(entry.get("site") or "").strip().lower() or None
        norm = {"backend": backend}
        if backend == "webchat" and site in ADAPTERS:
            norm["site"] = site
        tasks[str(task)] = norm
    return tasks, bg


def normalize_rotate_cfg(cfg) -> list[tuple[str, int]]:
    """llm.local_tasks.rotate → [(сайт, сколько вопросов подряд)] в порядке
    YAML. Мусор отбрасывается: сайт вне ADAPTERS, счётчик вне 1..ROTATE_MAX."""
    from app.features.web_llm import ADAPTERS
    raw = cfg.get("rotate") if isinstance(cfg, dict) else None
    out: list[tuple[str, int]] = []
    for site, n in (raw.items() if isinstance(raw, dict) else ()):
        site = str(site or "").strip().lower()
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if site in ADAPTERS and 1 <= n <= ROTATE_MAX \
                and site not in (s for s, _n in out):
            out.append((site, n))
    return out


def _global_webchat_sites() -> list[str]:
    try:
        from app.core.router import _parse_webchat_sites
        return list(_parse_webchat_sites())
    except Exception:
        return []


def _router_webchat_sites(router) -> tuple[Optional[str], list[str]]:
    """(основной веб-чат персоны | None, веб-чаты её fallback-цепочки по
    порядку, без основного) — из ModelRouter персоны."""
    if router is None:
        return None, []

    def _site(tok) -> Optional[str]:
        if not isinstance(tok, str):
            return None
        if tok == "webchat":  # голый webchat — все сайты, первым идёт первый
            sites = list(getattr(router, "webchat_sites", None) or [])
            # Сайты, исключённые персоной (llm.exclude), пропускаем — роутер
            # их всё равно не тронет (см. ModelRouter._filter_excluded_sites);
            # 'webchat:<сайт>' ниже не фильтруется — это уже закреплённый
            # основной провайдер, исключение его не касается.
            excluded = getattr(router, "excluded", None) or set()
            sites = [s for s in sites if f"webchat:{s}" not in excluded]
            return sites[0] if sites else None
        if tok.startswith("webchat:"):
            return tok.split(":", 1)[1] or None
        return None

    primary = _site(getattr(router, "active_provider", None))
    try:
        order = router._get_full_order()
    except Exception:
        order = []
    chain: list[str] = []
    for tok in order:
        site = _site(tok)
        if site and site != primary and site not in chain:
            chain.append(site)
    return primary, chain


class LocalLLMRouter:
    # Роутер служебных задач: Ollama или веб-чат (канал side) — по выбору на задачу.

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url or os.getenv("OLLAMA_URL", DEFAULT_OLLAMA_URL).rstrip("/")
        self.model = model or OLLAMA_MODEL
        self.timeout = timeout
        # Привязки персон (bind_persona): context -> {"router": ModelRouter,
        # "tasks": {задача: {"backend", "site"?}}, "bg_site": str}
        self._personas: dict = {}

        self._client = httpx.Client(timeout=timeout)
        self._last_check = 0.0  # для периодической пере-проверки в is_available()
        self._available = self._check_available()
        self._webchats: dict = {}  # сайт -> ленивый WebChatLLM (канал side)
        self._rot_lock = threading.Lock()  # счётчики очереди «rotate»

        if self._available:
            logger.info(f"[LocalLLM] Подключен к Ollama: {self.base_url}, модель: {self.model}")
        else:
            logger.warning(
                f"[LocalLLM] Ollama недоступен по {self.base_url}. "
                f"Бинарные классификаторы будут fallback на основной роутер."
            )

    # ── движки задач: выбор на персону ──

    def _default_backend(self) -> str:
        # Движок задач без привязки к персоне: LOCAL_LLM_BACKEND (дефолт ollama).
        b = (os.getenv("LOCAL_LLM_BACKEND") or "ollama").strip().lower()
        return b if b in ("ollama", "webchat") else "ollama"

    def bind_persona(self, context: str, router=None, cfg=None) -> None:
        """Привязать персону: её ModelRouter (цепочка провайдеров — откуда
        брать веб-чаты фоновых задач) и llm.local_tasks из YAML:
        {"bg_site": "fallback"|"primary"|<сайт>,
         "tasks": {задача: {"backend": "ollama"|"webchat", "site": <сайт>}}}.
        Повторный вызов заменяет привязку (живое применение настроек)."""
        tasks, bg = normalize_local_tasks_cfg(cfg)
        self._personas[str(context)] = {"router": router, "tasks": tasks,
                                        "bg_site": bg,
                                        "rotate": normalize_rotate_cfg(cfg),
                                        "rot_i": 0}

    def for_persona(self, context: Optional[str]):
        # Вид роутера для персоны; без context — сам общий роутер
        return PersonaLocalRouter(self, context) if context else self

    def _webchat_order(self, binding: dict, site: Optional[str],
                       advance: bool = False) -> list[str]:
        """Сайты веб-чата задачи по порядку попыток: выбранный (сайт задачи,
        иначе bg_site персоны), затем запасной — основной веб-чат персоны
        (или первый fallback, если выбран сам основной; основной не веб-чат —
        следующий веб-чат цепочки). «rotate» — очередной сайт очереди, за
        ним следующий; advance — сдвинуть очередь (только настоящий вызов,
        не снимок для настроек)."""
        if binding.get("router") is None:
            # Роутера персоны нет — глобальный порядок веб-чатов
            sites = _global_webchat_sites()
            primary, chain = (sites[0] if sites else None), sites[1:]
        else:
            primary, chain = _router_webchat_sites(binding.get("router"))
        fallback = chain[0] if chain else None
        mode = site or binding.get("bg_site") or DEFAULT_BG_SITE
        rot = binding.get("rotate") or []
        if mode == "rotate" and not rot:
            mode = DEFAULT_BG_SITE  # очередь не задана — как по умолчанию
        if mode == "rotate":
            seq = [s for s, n in rot for _ in range(n)]
            with self._rot_lock:
                i = int(binding.get("rot_i") or 0)
                if advance:
                    binding["rot_i"] = i + 1
            cur = seq[i % len(seq)]
            order = [cur] + [s for s, _n in rot if s != cur] \
                + [primary, fallback]
        elif mode == "primary":
            order = [primary, fallback]
        elif mode == "fallback":
            order = [fallback, primary]
        else:
            order = [mode, primary, fallback]
        order += chain[1:]
        out: list[str] = []
        for s in order:
            if s and s not in out:
                out.append(s)
        return out[:2]

    def _resolve_task(self, task: Optional[str],
                      persona: Optional[str] = None,
                      advance: bool = False) -> tuple[str, list[str]]:
        """(backend, сайты веб-чата по порядку попыток) задачи персоны:
        явный выбор из llm.local_tasks, иначе дефолт по роду задачи.
        Веб-чатов нет — ollama. advance — сдвинуть очередь «rotate»."""
        if task and LOCAL_TASKS.get(task):
            return "ollama", []
        binding = self._personas.get(str(persona)) if persona else None
        if binding is None:
            if self._default_backend() == "webchat":
                sites = _global_webchat_sites()[:1]
                if sites:
                    return "webchat", sites
            return "ollama", []
        entry = binding["tasks"].get(task) if task else None
        if entry:
            backend, site = entry["backend"], entry.get("site")
        else:
            backend = "webchat" if task in BACKGROUND_TASKS else "ollama"
            site = None
        if backend == "webchat":
            sites = self._webchat_order(binding, site, advance=advance)
            if sites:
                return "webchat", sites
        return "ollama", []

    def task_snapshot(self, persona: Optional[str] = None) -> dict:
        """Снимок для UI: bg_site персоны, её основной/первый fallback
        веб-чат, доступные сайты и resolved-движок каждой задачи."""
        binding = self._personas.get(str(persona)) if persona else None
        primary, chain = (_router_webchat_sites(binding.get("router"))
                          if binding else (None, []))
        tasks = []
        for task, ollama_only in LOCAL_TASKS.items():
            backend, sites = self._resolve_task(task, persona)
            entry = (binding["tasks"].get(task) if binding else None) or {}
            tasks.append({"id": task, "backend": backend, "sites": sites,
                          "site": entry.get("site"),
                          "explicit": bool(entry) and not ollama_only,
                          "background": task in BACKGROUND_TASKS,
                          "ollama_only": ollama_only})
        sites = [s for s in [primary] + chain if s]
        return {"bg_site": binding["bg_site"] if binding else DEFAULT_BG_SITE,
                "primary_site": primary,
                "fallback_site": chain[0] if chain else None,
                "sites": sites, "tasks": tasks}

    def reset_webchats(self):
        """Веб-чаты выключили — выбывшие инстансы закрывают свои вкладки
        (идущий вызов — по его завершении, см. WebChatLLM.retire), а не
        остаются жить в пуле; следующий вызов создаст инстанс заново."""
        old = list(self._webchats.values())
        self._webchats.clear()
        for chat in old:
            try:
                chat.retire()
            except Exception:
                pass

    def _get_webchat(self, site: Optional[str] = None):
        """WebChatLLM для локальных задач: сайт (None — первый включённый из
        WEBCHAT_SITES), канал «side» — отдельный чат и квота. Контекст
        «default»: вкладка side на сайт одна на процесс, общая для всех
        персон (там короткие служебные вызовы — персональный контент
        инициатив/LTM у персон идёт в своих чатах, см. ModelRouter(context=...)).
        None — сайта нет."""
        try:
            from app.features.web_llm import ADAPTERS, WebChatLLM
            if site is None:
                sites = _global_webchat_sites()
                if not sites:
                    return None
                site = sites[0]
            if site not in ADAPTERS:
                return None
            chat = self._webchats.get(site)
            if chat is None:
                # setdefault атомарен: два первых параллельных вызова не
                # создают два инстанса (две вкладки на одном side-чате)
                chat = self._webchats.setdefault(
                    site, WebChatLLM(site, channel="side"))
            return chat
        except Exception as e:
            logger.debug(f"[LocalLLM] Веб-чат для локальных задач недоступен: {e}")
            return None

    def _check_available(self) -> bool:
        # Ollama отвечает и нужная модель в ней скачана.
        if not self.model:
            # Модели по умолчанию нет — её выбирает пользователь (OLLAMA_MODEL)
            return False
        try:
            resp = self._client.get(f"{self.base_url}/api/tags", timeout=5.0)
            if resp.status_code != 200:
                return False
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            # Ollama хранит имя с тегом: <model> -> <model>:latest
            if self.model not in models and f"{self.model}:latest" not in models:
                logger.warning(
                    f"[LocalLLM] Модель '{self.model}' не найдена в Ollama. "
                    f"Доступные: {models}. Скачай: ollama pull {self.model}"
                )
                return False
            return True
        except Exception as e:
            logger.debug(f"[LocalLLM] Проверка доступности не удалась: {e}")
            return False

    def is_available(self, task: Optional[str] = None,
                     persona: Optional[str] = None) -> bool:
        """Доступен ли движок задачи персоны (какой бы ни был выбран).

        webchat — у задачи есть сайт веб-чата (_resolve_task отдаёт webchat
        только с сайтами); ollama — ответ /api/tags (после отказа
        пере-проверка не чаще раза в 30 сек). Доступность ≠ успех вызова:
        веб-чат мог не ответить, Ollama — упасть; тогда get_response честно
        вернёт None (или откатится на следующий движок).
        """
        backend, _sites = self._resolve_task(task, persona)
        if backend == "webchat":
            return True
        if self._available:
            return True
        # Ollama могла стартовать после бота — пере-проверяем не чаще раза в 30 сек
        now = time.time()
        if now - self._last_check < 30:
            return False
        self._last_check = now
        self._available = self._check_available()
        if self._available:
            logger.info(f"[LocalLLM] Ollama стал доступен: {self.base_url}, модель: {self.model}")
        return self._available

    def get_response(
        self,
        messages: list,
        temperature: float = 0.0,
        max_tokens: int = 100,
        top_p: float = 0.9,
        timeout: Optional[float] = None,
        task: Optional[str] = None,
        queue_wait: Optional[float] = None,
        persona: Optional[str] = None,
        webchat_timeout: Optional[float] = None,
    ) -> Optional[str]:
        """
        Отправляет запрос движку задачи (Ollama или веб-чат).
        Возвращает текст ответа или None при ошибке.

        task — идентификатор задачи из LOCAL_TASKS, persona — context персоны
        (см. bind_persona): движок и сайты берутся из её выбора для задачи
        (или дефолта по роду задачи). webchat — сайты по очереди (канал
        side), их неудача мягко откатывает на Ollama; ollama — только Ollama.

        queue_wait — сколько ждать очередь фона сайта веб-чата (сек); None —
        LOCAL_WEBCHAT_QUEUE_WAIT_SEC. Канал side фоновый: без бюджета вызов
        стоял бы в очереди сайта до BG_GATE_TIMEOUT_SEC (600 с), хотя
        локальные задачи — короткие служебные вызовы, многие прямо на пути
        ответа (query_rewrite, intent_router, rule_extract, learning_intent,
        help_detect…), и у них есть откат на Ollama. Фоновой задаче без
        отката, которой важнее дождаться, — передать queue_wait явно.

        webchat_timeout — бюджет ответа одного сайта веб-чата (сек); None —
        max(timeout, 150). timeout рассчитан на Ollama и для веб-чата мал,
        поэтому по умолчанию берётся пол 150 с; синхронной задаче на пути
        ответа (offline_summary) — передать свой потолок, иначе при зависших
        сайтах реплика ждала бы минуты.
        """
        backend, sites = self._resolve_task(task, persona, advance=True)
        if backend == "webchat":
            for site in sites:
                chat = self._get_webchat(site)
                if chat is None:
                    continue
                try:
                    # Веб-чат медленный (стриминг + опрос DOM) — минимум как у роутера
                    answer = chat.get_response(
                        messages, temperature=temperature, max_tokens=max_tokens,
                        top_p=top_p,
                        timeout=(webchat_timeout if webchat_timeout is not None
                                 else max(timeout or 0.0, 150.0)),
                        lock_timeout=(LOCAL_WEBCHAT_QUEUE_WAIT_SEC
                                      if queue_wait is None else queue_wait))
                    if answer:
                        return answer.strip()
                    logger.info(f"[LocalLLM] Веб-чат {site} ({task or 'default'}) "
                                f"не ответил — следующий движок")
                except Exception as e:
                    logger.warning(f"[LocalLLM] Ошибка веб-чата {site}: {e}")
            logger.warning(
                f"[LocalLLM] Веб-чаты задачи «{task or 'default'}» "
                f"({', '.join(sites)}) не ответили — пробуем Ollama, если доступна")

        if not self._available:
            return None

        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                # Сколько держать модель в памяти после ответа. Служебные
                # вызовы редкие — при дефолтных 5m модель (~2.6 ГБ) висит в
                # RAM почти постоянно; 2m даёт ей реально выгружаться.
                "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "2m"),
                # Рассуждающие модели иначе тратят весь
                # num_predict на thinking, и content возвращается пустым —
                # классификаторы получают None. Для служебных вызовов
                # рассуждения не нужны. На обычных моделях флаг безвреден.
                "think": False,
                "options": {
                    "temperature": temperature,
                    "top_p": top_p,
                    "num_predict": max_tokens,
                    # Контекст больше дефолтных 4096: системный промпт персоны +
                    # результаты поиска + STM в сумме подходят к лимиту, и на вывод
                    # остаётся несколько токенов — ответ обрывается посреди слова
                    "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "8192")),
                },
            }

            resp = self._client.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=timeout or self.timeout,
            )
            resp.raise_for_status()

            data = resp.json()
            answer = data.get("message", {}).get("content", "")

            logger.debug(
                f"[LocalLLM] {self.model} | len={len(answer)} | "
                f"prompt={data.get('prompt_eval_count', '?')} | "
                f"eval={data.get('eval_count', '?')}"
            )

            return answer.strip() if answer else None

        except Exception as e:
            logger.warning(f"[LocalLLM] Ошибка запроса: {e}")
            return None

    def classify(
        self,
        system_prompt: str,
        user_prompt: str,
        valid_outputs: list[str],
        temperature: float = 0.0,
        max_tokens: int = 50,
        task: Optional[str] = None,
        persona: Optional[str] = None,
    ) -> Optional[str]:
        """
        Упрощённый классификатор.
        Возвращает одно из допустимых значений или None.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        response = self.get_response(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            task=task,
            persona=persona,
        )

        if not response:
            return None

        response_upper = response.strip().upper()

        # Допустимое значение встречается в ответе
        for valid in valid_outputs:
            if valid.upper() in response_upper:
                return valid

        # Fallback: первое слово
        first_word = response_upper.split()[0] if response_upper else ""
        for valid in valid_outputs:
            if valid.upper() == first_word:
                return valid

        logger.debug(f"[LocalLLM] Не распознан ответ: '{response[:80]}...'")
        return None


    def ocr_image(self, image_bytes: bytes, question: str = "",
                  lang: Optional[str] = None) -> Optional[str]:
        """
        Извлекает текст с изображения и описывает его (vision).
        Требует мультимодальную модель — веб-чату картинки не отдать, поэтому
        OCR всегда идёт в Ollama независимо от выбранного движка.
        Возвращает None при ошибке.
        """
        # is_available() в режиме webchat говорит про веб-чат — для OCR
        # нужна живая Ollama, проверяем напрямую (вызов редкий, троттлинг не нужен)
        if not self._available:
            self._available = self._check_available()
        if not self._available:
            return None

        try:
            import base64
            img_b64 = base64.b64encode(image_bytes).decode()

            prompt = (
                "The user sent an image. Extract all visible text from it (OCR) "
                "and briefly describe what is shown (1-2 sentences).\n"
                "Response format:\nTEXT: <text from the image or \"no text\">\nDESCRIPTION: <...>"
            )
            if question:
                prompt += f"\nAdditionally answer the user's question about the image: {question}"
            # lang — язык пользователя; None — по подписи к картинке
            prompt += "\n" + user_language_line(lang or detect_language(question))

            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt, "images": [img_b64]}],
                "stream": False,
                # См. get_response: короткий keep_alive, чтобы модель выгружалась
                "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "2m"),
                # См. get_response: reasoning-модели съедают num_predict на thinking
                "think": False,
                "options": {
                    "temperature": 0.2,
                    "num_predict": 600,
                },
            }

            resp = self._client.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=120.0,  # vision на CPU медленнее текстовых вызовов
            )
            resp.raise_for_status()

            answer = resp.json().get("message", {}).get("content", "")
            return answer.strip() if answer else None

        except Exception as e:
            logger.warning(f"[LocalLLM] Ошибка OCR изображения: {e}")
            return None


# Глобальный singleton (ленивая инициализация)
_local_router: Optional[LocalLLMRouter] = None
# Double-checked locking: первые вызовы get_local_router() идут из разных
# потоков одновременно; без лока каждый создал бы свой LocalLLMRouter (лишний
# httpx.Client и запрос /api/tags), и часть потоков работала бы с
# «потерянным» экземпляром, который перезаписал последний.
_local_router_lock = threading.Lock()


class PersonaLocalRouter:
    """Вид общего локального роутера, привязанный к персоне: те же методы
    задач (is_available/get_response/classify), движок — из её настроек
    (bind_persona). Остальное (model, base_url, ocr_image, _available…)
    читается у общего роутера."""

    def __init__(self, base: LocalLLMRouter, context: str):
        self._base = base
        self.context = str(context)

    def __getattr__(self, name):
        return getattr(self._base, name)

    def is_available(self, task: Optional[str] = None) -> bool:
        return self._base.is_available(task=task, persona=self.context)

    def get_response(self, messages: list, *args, **kwargs) -> Optional[str]:
        kwargs.setdefault("persona", self.context)
        return self._base.get_response(messages, *args, **kwargs)

    def classify(self, *args, **kwargs) -> Optional[str]:
        kwargs.setdefault("persona", self.context)
        return self._base.classify(*args, **kwargs)


def get_local_router(context: Optional[str] = None):
    """Общий локальный роутер; с context — его вид для персоны (движки
    задач из её настроек, см. bind_persona)."""
    global _local_router
    if _local_router is None:
        with _local_router_lock:
            if _local_router is None:
                _local_router = LocalLLMRouter()
    return _local_router.for_persona(context)
