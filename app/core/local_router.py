"""
Локальный LLM роутер через Ollama.

Используется для лёгких бинарных классификаций:
- need_search: SEARCH / SKIP
- self_memory: SKIP / NOTE
- proactive: МОЛЧУ / мысль

Преимущества: быстро, дёшево (бесплатно), приватно.

Движок каждой задачи выбирает пользователь (настройки досье, «Движок
локальных задач»): «ollama» (дефолт) или «webchat» — тогда вызовы этой
задачи уходят в веб-чат через WebChatLLM (канал side: отдельный чат и
квота, чтобы не замусоривать контекст основной беседы); конкретный сайт
тоже выбирается пользователем. Выбор хранится в data/local_backends.json,
задача без записи наследует LOCAL_LLM_BACKEND из env (дефолт ollama).
Веб-чат не ответил — мягкий откат на Ollama, если она доступна. OCR
остаётся только за Ollama: веб-чату не отдать картинку.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx

from app.core.config import OLLAMA_MODEL

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_TIMEOUT = 15.0

# Все задачи локального движка (полный список потребителей Ollama).
# Значение — ollama_only: задача технически не может уйти в веб-чат.
LOCAL_TASKS: dict[str, bool] = {
    "need_search": False,          # нужен ли веб-поиск (features/need_search)
    "query_rewrite": False,        # рерайтер/улучшатель поисковых запросов
    "book_search": False,          # книжный RAG: перевод, distill, сплит, кореферентность
    "self_memory": False,          # заметки в личный дневник
    "state_engine": False,         # тики состояния + оценка инициативы
    "world_engine": False,         # мир: NPC из диалога, события, стимулы
    "offline_summary": False,      # сжатие офлайн-дневника
    "proactive_prefilter": False,  # префильтр проактивных инициатив
    "intent_router": False,        # арбитр намерений (TODO/инвентарь)
    "todo_cleanup": False,         # очистка текста задач
    "rule_extract": False,         # извлечение правил из реплик
    "inventory_enrich": False,     # описания предметов инвентаря
    "learning_intent": False,      # детект «хочу учиться»
    "learning": False,             # генерация уроков и словаря курса
    "help_detect": False,          # детект просьб о помощи
    "dossier": False,              # анализ досье (fallback без основного роутера)
    "relationship": False,         # разбор диалога → общие моменты/темы отношений
    "dialogue_harvest": False,     # общий урожай диалога: NPC + mood + моменты/позиции
    "ocr": True,                   # текст с картинок — только Ollama (vision)
}

# Выбор пользователя: {task: {"backend": "ollama"|"webchat", "site": ...}}
_TASKS_FILE = Path("data/local_backends.json")


def _load_task_config() -> dict:
    try:
        cfg = json.loads(_TASKS_FILE.read_text(encoding="utf-8"))
        tasks = cfg.get("tasks") if isinstance(cfg, dict) else None
        return tasks if isinstance(tasks, dict) else {}
    except Exception:
        return {}


def _save_task_config(tasks: dict):
    try:
        _TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _TASKS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(_TASKS_FILE)
    except Exception as e:
        logger.warning(f"[LocalLLM] Конфиг движков задач не записан: {e}")


class LocalLLMRouter:
    """
    Простой роутер к локальной модели через Ollama API.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url or os.getenv("OLLAMA_URL", DEFAULT_OLLAMA_URL).rstrip("/")
        self.model = model or OLLAMA_MODEL
        self.timeout = timeout
        # Движки задач (Ollama/веб-чат по каждой) — выбор пользователя
        self._task_cfg = _load_task_config()

        self._client = httpx.Client(timeout=timeout)
        self._last_check = 0.0  # для периодической пере-проверки в is_available()
        self._available = self._check_available()
        self._webchats: dict = {}  # сайт -> ленивый WebChatLLM (канал side)

        if self._available:
            logger.info(f"[LocalLLM] Подключен к Ollama: {self.base_url}, модель: {self.model}")
        else:
            logger.warning(
                f"[LocalLLM] Ollama недоступен по {self.base_url}. "
                f"Бинарные классификаторы будут fallback на основной роутер."
            )
        if any(v.get("backend") == "webchat" for v in self._task_cfg.values()
               if isinstance(v, dict)):
            logger.info("[LocalLLM] Часть задач локального движка идёт через веб-чат (side)")

    # ── движки задач: ollama или веб-чат (выбор пользователя) ──

    def _default_backend(self) -> str:
        """Дефолтный движок для задач без явной записи: LOCAL_LLM_BACKEND."""
        b = (os.getenv("LOCAL_LLM_BACKEND") or "ollama").strip().lower()
        return b if b in ("ollama", "webchat") else "ollama"

    def _resolve_task(self, task: Optional[str]) -> tuple[str, Optional[str]]:
        """(backend, site|None) задачи: запись пользователя или дефолт env."""
        entry = self._task_cfg.get(task) if task else None
        if isinstance(entry, dict):
            backend = entry.get("backend")
            backend = backend if backend in ("ollama", "webchat") else None
            if backend:
                site = entry.get("site") or None
                return backend, (str(site) if site else None)
        return self._default_backend(), None

    def task_snapshot(self) -> list[dict]:
        """Снимок для UI: текущий (resolved) движок каждой известной задачи."""
        out = []
        for task, ollama_only in LOCAL_TASKS.items():
            backend, site = self._resolve_task(task)
            out.append({"id": task, "backend": backend,
                        "site": site if backend == "webchat" else None,
                        "ollama_only": ollama_only})
        return out

    def set_task_backend(self, task: str, backend: str,
                         site: Optional[str] = None) -> tuple[bool, str]:
        """Выбрать движок задачи: «ollama»/«webchat» + сайт веб-чата
        (None/пусто — первый включённый). Персист в data/local_backends.json.
        (ok, detail) — detail заполняется при отказе."""
        if task not in LOCAL_TASKS:
            return False, f"Неизвестная задача «{task}»"
        backend = (backend or "").strip().lower()
        if backend not in ("ollama", "webchat"):
            return False, "Движок должен быть «ollama» или «webchat»"
        site = (site or "").strip().lower() or None
        if backend == "webchat":
            if LOCAL_TASKS[task]:
                return False, "Эта задача технически не может уйти в веб-чат"
            from app.core.router import _parse_webchat_sites
            sites = _parse_webchat_sites()
            if not sites:
                return False, "Сначала включите веб-чат (выберите хотя бы один сайт)"
            if site is not None and site not in sites:
                return False, f"Сайт «{site}» не включён"
            if site is None:
                site = sites[0]
        else:
            site = None
        entry = {"backend": backend}
        if site:
            entry["site"] = site
        tasks = dict(self._task_cfg)
        old = tasks.get(task)
        if old == entry:
            return True, ""
        tasks[task] = entry
        _save_task_config(tasks)
        self._task_cfg = tasks
        logger.info(f"[LocalLLM] Задача «{task}»: движок {backend}"
                    + (f", сайт {site}" if site else ""))
        return True, ""

    def reset_webchat_tasks(self):
        """Веб-чаты выключили — все задачи, что шли в веб-чат, возвращаем на
        Ollama (иначе вызовы молча ходили бы в никуда; get_response и так
        откатывается, но конфиг не должен врать)."""
        changed = {t: {"backend": "ollama"} for t, v in self._task_cfg.items()
                   if isinstance(v, dict) and v.get("backend") == "webchat"}
        if not changed:
            return
        tasks = dict(self._task_cfg)
        tasks.update(changed)
        _save_task_config(tasks)
        self._task_cfg = tasks
        self._webchats.clear()
        logger.info(f"[LocalLLM] Веб-чаты выключены — задачи на Ollama: "
                    f"{', '.join(sorted(changed))}")

    def _get_webchat(self, site: Optional[str] = None):
        """WebChatLLM для локальных задач: сайт задачи (или первый включённый
        из WEBCHAT_SITES), канал «side» — отдельный чат и квота. Контекст
        «default»: локальный роутер — синглтон без привязки к персоне, его
        сайт#side общий для всех персон (там только короткие классификации —
        персональный контент инициатив/LTM у персон теперь в своих чатах,
        см. ModelRouter(context=...)). None — веб-чаты не включены."""
        try:
            from app.core.router import _parse_webchat_sites
            from app.features.web_llm import WebChatLLM
            sites = _parse_webchat_sites()
            if not sites:
                return None
            target = site if site in sites else sites[0]
            chat = self._webchats.get(target)
            if chat is None:
                chat = WebChatLLM(target, channel="side")
                self._webchats[target] = chat
            return chat
        except Exception as e:
            logger.debug(f"[LocalLLM] Веб-чат для локальных задач недоступен: {e}")
            return None

    def _check_available(self) -> bool:
        """Проверяет доступность Ollama."""
        try:
            resp = self._client.get(f"{self.base_url}/api/tags", timeout=5.0)
            if resp.status_code != 200:
                return False
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            # Ollama хранит имя с тегом: llama3 -> llama3:latest
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

    def is_available(self, task: Optional[str] = None) -> bool:
        """Доступен ли движок задачи (какой бы ни был выбран).

        webchat — включён сайт задачи (или любой, если сайт не задан);
        ollama — как раньше (с пере-проверкой не чаще раза в 30 сек).
        Доступность ≠ успех вызова: веб-чат мог не ответить, Ollama — упасть;
        тогда get_response честно вернёт None (или откатится на второй движок).
        """
        backend, site = self._resolve_task(task)
        if backend == "webchat":
            try:
                from app.core.router import _parse_webchat_sites
                sites = _parse_webchat_sites()
                return bool(sites) and (site is None or site in sites)
            except Exception:
                return False
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
    ) -> Optional[str]:
        """
        Отправляет запрос к локальной модели.
        Возвращает текст ответа или None при ошибке.

        task — идентификатор задачи из LOCAL_TASKS: движок (Ollama/веб-чат
        и сайт) берётся из выбора пользователя для этой задачи. webchat —
        сначала веб-чат (канал side), его неудача мягко откатывает на
        Ollama; ollama — только Ollama, как раньше.
        """
        backend, site = self._resolve_task(task)
        if backend == "webchat":
            chat = self._get_webchat(site)
            if chat is not None:
                try:
                    # Веб-чат медленный (стриминг + опрос DOM) — минимум как у роутера
                    answer = chat.get_response(
                        messages, temperature=temperature, max_tokens=max_tokens,
                        top_p=top_p, timeout=max(timeout or 0.0, 150.0))
                    if answer:
                        return answer.strip()
                    logger.warning(
                        f"[LocalLLM] Веб-чат ({task or 'default'}) не ответил — "
                        f"пробуем Ollama, если доступна")
                except Exception as e:
                    logger.warning(f"[LocalLLM] Ошибка веб-чата: {e}")
            else:
                logger.warning(
                    f"[LocalLLM] Движок задачи «{task or 'default'}» — webchat, "
                    f"но сайты не включены — пробуем Ollama")

        if not self._available:
            return None

        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                # Рассуждающие модели (gemma4 и т.п.) иначе тратят весь
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
        )

        if not response:
            return None

        response_upper = response.strip().upper()

        # Ищем точное совпадение
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


    def ocr_image(self, image_bytes: bytes, question: str = "") -> Optional[str]:
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

            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt, "images": [img_b64]}],
                "stream": False,
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


def get_local_router() -> LocalLLMRouter:
    global _local_router
    if _local_router is None:
        _local_router = LocalLLMRouter()
    return _local_router
