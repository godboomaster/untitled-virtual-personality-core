import os
import logging
import socket
import threading
import time
from openai import OpenAI
from app.core.config import PROVIDER_CONFIGS, get_available_providers

logger = logging.getLogger(__name__)


# Крошечная тестовая картинка (240x100 PNG, белый фон, цифра «42») для автопробы
# vision-возможностей провайдеров — генерируется один раз, лежит константой.
_VISION_PROBE_IMAGE_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAeAAAADICAIAAAC/PqUtAAADmElEQVR42u3cMWoiYRyH4TVsaWFjE7D3FuliJSJKxBQpU6Ww9AzeIIUarI2VBKxsPIIgBNKmCYKoIFazF/i7yyySHZfnKX/FEGbC61cMk0uS5AcA2XPlFgAINAACDSDQAAg0gEADINAACDSAQAMg0AACDYBAAyDQAAINgEADCDQAAg0g0AAINAACDSDQAAg0gEADINAACDSAQAMg0AACDYBAAwg0AAINgEADCDQAAg0g0AAINAACDSDQAAg0gEADINAAAg2AQAMg0AACDYBAAwg0AAINgEADCDQAAg0g0AAINAACDSDQAKT10y34Hm9vb+HeaDTC/XA4pLr+YDAI9+FwGO673S7ce71euN/e3nqI4AQNgEADCDQAAg0g0AAINIBAA5AVuSRJ3IUzOvV+caVSCfflchnu2+023L++vsK92WyG+3w+D/f39/dwr9fr4b5arTxccIIGQKABBBoAgQYQaAAEGkCgAcgK34M+s263G+6dTifcHx8fU11/vV6H+9PTU/wLfBX/BpdKpVTXB5ygARBoAIEGQKABBBoAgQYQaAAyxHvQf2mxWIT75+dnuN/d3YV72vegy+Vyqv2U8Xgc7tVq1cMFJ2gABBpAoAEQaACBBkCgARBogKzLJUniLvzG8XgM95ubm3CfTCbhfn19He6FQiHcN5vNWf7+j4+PcK/VauE+n8/DvVgs+mcAJ2gABBpAoAEQaACBBkCgAQQagKzwPeg/eH19Dffdbhfu9/f3qa6/3+/D/eHhIdxHo1Gq67RarXDv9/vh7n1ncIIGQKABBBoAgQYQaAAEGgCBBrgAvgf9j6X9HvSp59VsNlPt7XbbzQcnaAAEGkCgARBoAIEGQKABEGiAS+V70Bfm5eUl3GezWbiv1+twf35+Dvd8Ph/u0+nUzQcnaAAEGkCgARBoAIEGQKABBBqArPA9aAAnaAAEGkCgARBoAIEGQKABEGgAgQZAoAEEGgCBBkCgAQQaAIEGEGgABBpAoAEQaAAEGkCgARBoAIEGQKABEGgAgQZAoAEEGgCBBhBoAAQaAIEGEGgABBpAoAEQaAAEGkCgARBoAIEGQKABBNotABBoAAQaQKABEGgAgQZAoAEQaACBBkCgAQQaAIEGQKABBBoAgQYQaAAEGkCgARBoAAQaQKABEGgAgQZAoAEQaACBBkCgAQQaAIEGEGgABBoAgQYQaAAEGkCgARBoAAQaQKABEGiA/9Ev/L1/S1mQHbAAAAAASUVORK5CYII="
)


def _parse_webchat_sites() -> list[str]:
    """Сайты веб-чата из env: WEBCHAT_SITES=qwen,deepseek (порядок = порядок
    перебора); legacy WEBCHAT_SITE (один сайт) добавляется, если его нет."""
    raw = f"{os.getenv('WEBCHAT_SITES') or ''},{os.getenv('WEBCHAT_SITE') or ''}"
    try:
        from app.features.web_llm import ADAPTERS
        known = set(ADAPTERS)
    except Exception:
        known = {"deepseek", "qwen", "claude", "zai", "chatgpt", "kimi",
                 "google"}
    out: list[str] = []
    for tok in raw.split(","):
        site = tok.strip().lower()
        if site and site in known and site not in out:
            out.append(site)
    return out


# ── Детект офлайна ──
# Без интернета облачная цепочка и веб-чаты заведомо мертвы, а до local
# цепочка идёт минуты таймаутов — probe сырых IP раз в 30 с, офлайн →
# локальная модель (Ollama) пробуется первой.
_NET_CHECK_TTL_SEC = 30.0
_NET_PROBE_TIMEOUT_SEC = 1.5
_NET_PROBE_HOSTS = (("1.1.1.1", 443), ("8.8.8.8", 53))  # IP-литералы, без DNS
_net_lock = threading.Lock()
_net_ok: bool | None = None
_net_checked = 0.0

# Сколько пользовательский (main) вызов веб-чата ждёт лок занятого чата,
# прежде чем уйти в burst (разовый свежий чат): секунды. Дольше ждать —
# значит задерживать ответ пользователю из-за чужой генерации.
BURST_LOCK_WAIT_SEC = 3.0


def internet_available() -> bool:
    """Есть ли интернет: TCP-probe пары надёжных IP, кэш на 30 с (процесс).
    Ложное «офлайн» безопасно: если local не ответила, обычная цепочка всё
    равно идёт дальше — меняется только её приоритет."""
    global _net_ok, _net_checked
    with _net_lock:
        if (_net_ok is not None
                and time.monotonic() - _net_checked < _NET_CHECK_TTL_SEC):
            return _net_ok
    ok = False
    for host, port in _NET_PROBE_HOSTS:
        try:
            socket.create_connection((host, port),
                                     timeout=_NET_PROBE_TIMEOUT_SEC).close()
            ok = True
            break
        except OSError:
            continue
    with _net_lock:
        _net_ok, _net_checked = ok, time.monotonic()
    return ok


class ModelRouter:
    def __init__(self, provider: str = None, context: str = "default"):
        # context — изоляция состояния веб-чатов персоны: у каждой свой
        # постоянный чат на сайте (data/{context}/computer_control), иначе
        # все персоны писали бы в один разговор и видели чужой контекст
        self.context = context or "default"
        self.available = get_available_providers()
        self.active_provider = provider or os.getenv("ACTIVE_PROVIDER")
        self._last_key_index: dict[str, int] = {}
        # Семафоры параллельности API-провайдеров (max_concurrent в конфиге,
        # напр. kimi=1 — аккаунтный лимит Moonshot): занят → мгновенный
        # фолбэк по цепочке, без 403 «concurrent request limit» (кейс 19.09:
        # фоновая задача и ответ пользователю столкнулись на одном ключе)
        self._provider_sems: dict[str, threading.BoundedSemaphore] = {}
        # Персональный override из YAML персоны (секция llm): закреплённый
        # основной провайдер (глобальная смена active его не трогает),
        # приоритет fallback-цепочки и свои модели по провайдерам.
        self.pinned_provider: str | None = None
        # Провайдеры по назначению (llm.answer_provider/cc_provider/
        # vision_provider в YAML персоны): None — обычная цепочка
        self.answer_provider: str | None = None  # текст ответа пользователю
        self.cc_provider: str | None = None      # решения режима управления
        self.vision_provider: str | None = None  # vision-фолбэк (картинки)
        self.fallback_order: list[str] | None = None
        self.model_overrides: dict[str, str] = {}
        # Веб-чаты как провайдеры без ключей (WEBCHAT_SITES=qwen,deepseek —
        # порядок перебора; legacy WEBCHAT_SITE — один сайт). Пусто — выключены.
        # Персона может включить/переставить их секцией llm (primary/fallback:
        # токены webchat:<сайт>; llm.webchat — сайт по умолчанию).
        self.webchat_sites: list[str] = _parse_webchat_sites()
        self._webchats: dict = {}  # site -> ленивый web_llm.WebChatLLM
        # Лимиты веб-чатов персоны (llm.webchat_limits): {сайт: per_hour|None}
        # None — лимит снят; сайта нет в dict — без лимита (дефолт)
        self.webchat_limits: dict = {}
        # Режим браузера для сайта (llm.webchat_mode): headless|hidden|headed
        # (web_extended: headless → пул H, hidden/headed → пул V)
        self.webchat_modes: dict = {}

        if not self.available:
            # Нет ни одного облачного ключа. Явно включённые веб-чаты
            # (WEBCHAT_SITES) — основной провайдер; иначе пробуем жить
            # полностью на локальной модели (Ollama).
            if self.webchat_sites:
                self.active_provider = "webchat"
                self._last_provider = "webchat"
                self._vision_verdict: dict[str, bool] = {}
                logger.warning(
                    f"Облачные провайдеры не настроены — бот работает через "
                    f"веб-чат {','.join(self.webchat_sites)} "
                    f"(аккаунт пользователя в Chrome)"
                )
                return
            try:
                from app.core.local_router import get_local_router
                local = get_local_router()
            except Exception:
                local = None
            if local and local.is_available():
                self.active_provider = "local"
                self._last_provider = "local"
                self._last_local_model = local.model
                self._vision_verdict: dict[str, bool] = {}
                logger.warning(
                    f"Облачные провайдеры не настроены — бот работает ПОЛНОСТЬЮ "
                    f"на локальной модели {local.model}"
                )
                return
            logger.critical(
                "Нет доступных провайдеров и локальная модель недоступна! "
                "Задайте API-ключ хотя бы для одного провайдера в .env или .env.config "
                "(например, ZAI_API_KEY=..., OPENAI_API_KEY=...) "
                "или запустите Ollama с локальной моделью."
            )
            raise RuntimeError("Нет настроенных провайдеров. Заполните API-ключи в конфиге.")

        if not self.active_provider:
            self.active_provider = next(iter(self.available))
            logger.warning(f"ACTIVE_PROVIDER не задан, используется первый доступный: {self.active_provider}")
        elif self.active_provider not in self.available:
            fallback = next(iter(self.available))
            logger.warning(
                f"Провайдер '{self.active_provider}' недоступен (нет API-ключа). "
                f"Доступные: {list(self.available.keys())}. "
                f"Используем fallback: {fallback}"
            )
            self.active_provider = fallback

        self._last_provider = self.active_provider
        # Кеш вердиктов автопробы vision: provider -> bool
        self._vision_verdict: dict[str, bool] = {}

        # Логируем количество ключей
        key_info = {p: len(cfg["api_keys"]) for p, cfg in self.available.items()}
        logger.info(f"ModelRouter: active={self.active_provider} | keys={key_info}")

    @property
    def webchat_site(self) -> str | None:
        """Первый (основной) сайт веб-чата — совместимость со старым кодом."""
        return self.webchat_sites[0] if self.webchat_sites else None

    @webchat_site.setter
    def webchat_site(self, site: str | None):
        self.webchat_sites = [site] if site else []
        self._webchats = {}

    def model_for(self, provider: str) -> str:
        """Модель провайдера с учётом персонального override (пусто, если неизвестен)."""
        if provider in self.model_overrides:
            return self.model_overrides[provider]
        return (self.available.get(provider) or {}).get("model", "")

    def _provider_sem(self, provider: str, cfg: dict):
        """Семафор параллельности провайдера (max_concurrent в конфиге);
        None — без лимита. Ленивое создание, потокобезопасность не нужна:
        худший случай гонки — два семафора, оба с лимитом (безопасно)."""
        limit = cfg.get("max_concurrent")
        if not limit:
            return None
        sem = self._provider_sems.get(provider)
        if sem is None:
            sem = threading.BoundedSemaphore(int(limit))
            self._provider_sems[provider] = sem
        return sem

    def _call_with_keys(self, provider: str, cfg: dict, messages: list,
                        temperature: float, max_tokens: int, top_p: float,
                        timeout: float) -> str | None:
        # Провайдер с лимитом параллельности занят (фон/другой чат) — не ждём
        # и не ловим 403 concurrent: мгновенный фолбэк по цепочке
        sem = self._provider_sem(provider, cfg)
        if sem is not None and not sem.acquire(blocking=False):
            logger.info(f"{provider.upper()}: занят параллельным запросом — "
                        "пропуск (фолбэк по цепочке)")
            return None
        try:
            return self._call_with_keys_locked(provider, cfg, messages,
                                               temperature, max_tokens, top_p,
                                               timeout)
        finally:
            if sem is not None:
                sem.release()

    def _call_with_keys_locked(self, provider: str, cfg: dict, messages: list,
                               temperature: float, max_tokens: int, top_p: float,
                               timeout: float) -> str | None:
        
        # Пробует все ключи провайдера по очереди. Возвращает ответ или None.
        keys = cfg["api_keys"]
        last_idx = self._last_key_index.get(provider, 0)
        model = self.model_overrides.get(provider) or cfg["model"]

        # Начинаем с последнего успешного ключа, потом остальные
        indices = [last_idx] + [i for i in range(len(keys)) if i != last_idx]

        for idx in indices:
            api_key = keys[idx]
            try:
                client = OpenAI(
                    api_key=api_key,
                    base_url=cfg["base_url"],
                    timeout=timeout
                )
                logger.debug(
                    f"[LLM Request] {provider}/{model} "
                    f"| key={idx + 1}/{len(keys)} "
                    f"| max_tokens={max_tokens} | messages={len(messages)}"
                )
                response = client.chat.completions.create(
                    model=model, messages=messages,
                    temperature=cfg.get("temperature", temperature),
                    max_tokens=max_tokens, top_p=cfg.get("top_p", top_p)
                )
                answer = response.choices[0].message.content
                self._last_provider = provider
                self._last_key_index[provider] = idx
                logger.debug(f"[Response] {provider}/{model} key={idx + 1} | len={len(answer) if answer else 0}")
                return answer
            except Exception as e:
                logger.warning(
                    f"{provider.upper()} key={idx + 1}/{len(keys)} ({model}) ошибка: {e}"
                )

        return None

    def _try_local(self, messages, temperature: float, max_tokens: int,
                   top_p: float, timeout: float, on_token=None) -> str | None:
        """Попытка ответа локальной моделью (Ollama). None — недоступна/не ответила.
        on_token задан — ответ дополнительно отдаётся одним куском (стрим)."""
        try:
            from app.core.local_router import get_local_router
            local = get_local_router()
            if not local.is_available():
                return None
            # Локальная модель на CPU медленная: даём ей минимум 180 сек,
            # иначе длинные ответы (max_tokens=4000) обрываются по таймауту.
            answer = local.get_response(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                timeout=max(timeout, 180.0),
            )
            if answer:
                if on_token is not None:
                    on_token(answer)
                self._last_provider = "local"
                self._last_local_model = local.model
                return answer
        except Exception as e:
            logger.warning(f"Локальный вызов не сработал: {e}")
        return None

    def get_response(self, messages, temperature: float = 0.7,
                     max_tokens: int = 2000, top_p: float = 0.9,
                     exclude_provider: str = None, timeout: float = 60.0,
                     webchat_channel: str = "main",
                     force_provider: str = None) -> str | None:
        """Возвращает ответ модели или None, если все провайдеры недоступны.

        Вызывающий код ОБЯЗАН проверять результат на None/пустоту — строка-заглушка
        больше не возвращается, чтобы ошибку нельзя было принять за ответ модели.

        force_provider — провайдер по назначению (llm.answer_provider/
        cc_provider): одна попытка ВНЕ цепочки, неудача — обычная цепочка.
        """
        provider_order = self._get_full_order()

        if exclude_provider and len(provider_order) > 1:
            if exclude_provider == "webchat":
                # голый 'webchat' исключает все веб-чаты разом
                provider_order = [p for p in provider_order
                                  if not (isinstance(p, str) and p.startswith("webchat"))]
            else:
                provider_order = [p for p in provider_order if p != exclude_provider]

        # Провайдер по назначению (текст ответа / решения управления): сначала
        # он, цепочка ниже — fallback. Не дублируем, если он же исключён.
        if force_provider and force_provider != exclude_provider \
                and internet_available():
            answer = self._call_forced(force_provider, messages, temperature,
                                       max_tokens, top_p, timeout, webchat_channel)
            if answer:
                return answer
            logger.info(f"[Router] назначенный провайдер {force_provider} "
                        "не ответил — обычная цепочка")

        # Нет интернета — веб-чаты и облако заведомо мертвы: сразу локальная
        # модель (Ollama), без минут таймаутов по мёртвым провайдерам. Не
        # ответила/не установлена — идём по обычной цепочке (вдруг probe солгал).
        tried_local = False
        if exclude_provider != "local" and not internet_available():
            tried_local = True
            answer = self._try_local(messages, temperature, max_tokens, top_p, timeout)
            if answer:
                logger.warning("Нет интернета — ответ локальной модели "
                               f"{getattr(self, '_last_local_model', '?')} сразу "
                               "(облачная цепочка пропущена)")
                return answer
            logger.error("Нет интернета и локальная модель недоступна — "
                         "пробуем обычную цепочку")

        # Основной провайдер — веб-чат (аккаунт пользователя в Chrome):
        # пробуем его первым, цепочка ниже — fallback. 'webchat' — все сайты
        # по порядку, 'webchat:<сайт>' — конкретный. exclude_provider
        # (побочные задачи: LTM и т.п.) пропускает и эту ветку.
        tried_webchats: set[str] = set()
        if (self.active_provider == "webchat" or
                self.active_provider.startswith("webchat:")) and \
                exclude_provider not in ("webchat", self.active_provider):
            sites = self.webchat_sites if self.active_provider == "webchat" \
                else [self.active_provider.split(":", 1)[1]]
            if isinstance(exclude_provider, str) and \
                    exclude_provider.startswith("webchat:"):
                ex_site = exclude_provider.split(":", 1)[1]
                sites = [s for s in sites if s != ex_site]
            tried_webchats.update(sites)
            if sites:
                answer = self._try_webchat(messages, temperature, max_tokens, top_p,
                                           timeout, sites, webchat_channel)
                if answer:
                    return answer
                logger.error("Веб-чат (основной провайдер) не ответил, идём по цепочке...")

        # Основной провайдер — локальная модель (глобально или закреплена за
        # персоной): пробуем её первой, облачная цепочка ниже — fallback.
        if self.active_provider == "local" and exclude_provider != "local":
            tried_local = True
            answer = self._try_local(messages, temperature, max_tokens, top_p, timeout)
            if answer:
                return answer
            logger.error("Локальная модель (основной провайдер) не ответила, переключаемся на облачных...")

        for provider in provider_order:
            if provider == "local":
                # Локальная — на позиции из fallback-списка персоны
                # (по умолчанию _get_full_order ставит её последней)
                if tried_local:
                    continue
                tried_local = True
                answer = self._try_local(messages, temperature, max_tokens, top_p, timeout)
                if answer:
                    logger.warning(f"Облачные выше по цепочке не ответили — ответ локальной модели {getattr(self, '_last_local_model', '?')}")
                    return answer
                continue
            if provider == "webchat" or provider.startswith("webchat:"):
                # Веб-чат — на своей позиции из fallback-списка (по умолчанию
                # после облачных, перед локальной)
                sites = self.webchat_sites if provider == "webchat" \
                    else [provider.split(":", 1)[1]]
                sites = [s for s in sites if s not in tried_webchats]
                if not sites:
                    continue
                tried_webchats.update(sites)
                answer = self._try_webchat(messages, temperature, max_tokens, top_p,
                                           timeout, sites, webchat_channel)
                if answer:
                    return answer
                continue
            cfg = self.available[provider]
            answer = self._call_with_keys(
                provider, cfg, messages, temperature, max_tokens, top_p, timeout
            )
            if answer:
                return answer
            logger.error(f"Провайдер {provider.upper()} не ответил ни одним ключом, переключаемся...")

        return None

    def get_response_stream(self, messages, on_token, temperature: float = 0.7,
                            max_tokens: int = 2000, top_p: float = 0.9,
                            exclude_provider: str = None, timeout: float = 60.0,
                            webchat_channel: str = "main",
                            force_provider: str = None) -> str | None:
        """Стриминговый вариант get_response: токены уходят в on_token(delta) по мере
        генерации, возвращается полный текст. Fallback на другой ключ/провайдер —
        только до первого токена; обрыв посередине — возвращаем накопленное.
        Локальный fallback (Ollama) не стримится — отдаётся одним куском.
        force_provider — провайдер по назначению: одна попытка вне цепочки
        (веб-чат/локальный отдают одним куском), неудача — обычная цепочка."""
        provider_order = self._get_full_order()

        if exclude_provider and len(provider_order) > 1:
            if exclude_provider == "webchat":
                provider_order = [p for p in provider_order
                                  if not (isinstance(p, str) and p.startswith("webchat"))]
            else:
                provider_order = [p for p in provider_order if p != exclude_provider]

        # Провайдер по назначению: сначала он, цепочка — fallback
        if force_provider and force_provider != exclude_provider \
                and internet_available():
            answer = self._call_forced(force_provider, messages, temperature,
                                       max_tokens, top_p, timeout,
                                       webchat_channel, on_token=on_token)
            if answer:
                return answer
            logger.info(f"[Router] назначенный провайдер {force_provider} "
                        "не ответил — обычная цепочка")

        # Нет интернета — сразу локальная модель (Ollama), облачная цепочка
        # заведомо мертва. Не ответила — идём по обычной цепочке (вдруг probe
        # солгал). Локальная не стримится — ответ одним куском через on_token.
        tried_local = False
        if exclude_provider != "local" and not internet_available():
            tried_local = True
            answer = self._try_local(messages, temperature, max_tokens, top_p, timeout, on_token)
            if answer:
                logger.warning("Нет интернета — ответ локальной модели "
                               f"{getattr(self, '_last_local_model', '?')} сразу "
                               "(облачная цепочка пропущена)")
                return answer
            logger.error("Нет интернета и локальная модель недоступна — "
                         "пробуем обычную цепочку")

        # Основной провайдер — локальная модель: первая попытка, облачные — fallback
        if self.active_provider == "local" and exclude_provider != "local":
            tried_local = True
            answer = self._try_local(messages, temperature, max_tokens, top_p, timeout, on_token)
            if answer:
                return answer
            logger.error("Локальная модель (основной провайдер) не ответила, переключаемся на облачных...")

        # Основной провайдер — веб-чат: не стримится, ответ одним куском
        # через on_token (как локальная модель)
        tried_webchats: set[str] = set()
        if (self.active_provider == "webchat" or
                self.active_provider.startswith("webchat:")) and \
                exclude_provider not in ("webchat", self.active_provider):
            sites = self.webchat_sites if self.active_provider == "webchat" \
                else [self.active_provider.split(":", 1)[1]]
            if isinstance(exclude_provider, str) and \
                    exclude_provider.startswith("webchat:"):
                ex_site = exclude_provider.split(":", 1)[1]
                sites = [s for s in sites if s != ex_site]
            tried_webchats.update(sites)
            if sites:
                answer = self._try_webchat(messages, temperature, max_tokens, top_p,
                                           timeout, sites, webchat_channel)
                if answer:
                    on_token(answer)
                    return answer
                logger.error("Веб-чат (основной провайдер) не ответил, идём по цепочке...")

        for provider in provider_order:
            if provider == "local":
                # Локальная — на позиции из fallback-списка персоны
                # (не стримится: _try_local отдаёт ответ одним куском)
                if tried_local:
                    continue
                tried_local = True
                answer = self._try_local(messages, temperature, max_tokens, top_p, timeout, on_token)
                if answer:
                    logger.warning(f"Облачные выше по цепочке не ответили — ответ локальной модели {getattr(self, '_last_local_model', '?')}")
                    return answer
                continue
            if provider == "webchat" or provider.startswith("webchat:"):
                # Веб-чат — на своей позиции из fallback-списка
                # (не стримится: ответ отдаётся одним куском)
                sites = self.webchat_sites if provider == "webchat" \
                    else [provider.split(":", 1)[1]]
                sites = [s for s in sites if s not in tried_webchats]
                if not sites:
                    continue
                tried_webchats.update(sites)
                answer = self._try_webchat(messages, temperature, max_tokens, top_p,
                                           timeout, sites, webchat_channel)
                if answer:
                    on_token(answer)
                    return answer
                continue
            cfg = self.available[provider]
            answer = self._stream_with_keys(
                provider, cfg, messages, on_token, temperature, max_tokens, top_p, timeout
            )
            if answer:
                return answer
            logger.error(f"Провайдер {provider.upper()} не ответил ни одним ключом, переключаемся...")

        return None

    def _stream_with_keys(self, provider: str, cfg: dict, messages: list, on_token,
                          temperature: float, max_tokens: int, top_p: float,
                          timeout: float) -> str | None:
        """Стримит ответ первого ответившего ключа провайдера. None — все ключи упали."""
        # Лимит параллельности провайдера (max_concurrent): занят — мгновенный
        # фолбэк, как в _call_with_keys (без 403 concurrent и ожидания)
        sem = self._provider_sem(provider, cfg)
        if sem is not None and not sem.acquire(blocking=False):
            logger.info(f"{provider.upper()}: занят параллельным запросом — "
                        "пропуск (фолбэк по цепочке)")
            return None
        try:
            return self._stream_with_keys_locked(provider, cfg, messages,
                                                 on_token, temperature,
                                                 max_tokens, top_p, timeout)
        finally:
            if sem is not None:
                sem.release()

    def _stream_with_keys_locked(self, provider: str, cfg: dict, messages: list,
                                 on_token, temperature: float, max_tokens: int,
                                 top_p: float, timeout: float) -> str | None:
        """Стримит ответ первого ответившего ключа провайдера. None — все ключи упали."""
        keys = cfg["api_keys"]
        last_idx = self._last_key_index.get(provider, 0)
        model = self.model_overrides.get(provider) or cfg["model"]
        indices = [last_idx] + [i for i in range(len(keys)) if i != last_idx]

        for idx in indices:
            parts: list = []
            try:
                client = OpenAI(
                    api_key=keys[idx],
                    base_url=cfg["base_url"],
                    timeout=timeout
                )
                stream = client.chat.completions.create(
                    model=model, messages=messages,
                    temperature=cfg.get("temperature", temperature),
                    max_tokens=max_tokens, top_p=cfg.get("top_p", top_p),
                    stream=True,
                )
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta.content
                    if delta:
                        parts.append(delta)
                        on_token(delta)
                answer = "".join(parts)
                if not answer:
                    continue  # пустой ответ — пробуем следующий ключ
                self._last_provider = provider
                self._last_key_index[provider] = idx
                return answer
            except Exception as e:
                logger.warning(
                    f"{provider.upper()} key={idx + 1}/{len(keys)} ({model}) stream ошибка: {e}"
                )
                if parts:
                    # Обрыв посередине стрима — лучше недописанный ответ, чем None
                    return "".join(parts)

        return None

    # ── лимиты веб-чатов (per-персона) ──

    @staticmethod
    def _norm_webchat_limits(raw) -> dict:
        """{сайт: {"enabled": bool, "per_hour": int}} → {сайт: per_hour|None}.
        None — лимит снят персоной; мусорные записи отбрасываются (дефолт)."""
        from app.features.web_llm import ADAPTERS as _WC
        out = {}
        for site, cfg in (raw or {}).items():
            if site not in _WC or not isinstance(cfg, dict):
                continue
            if not cfg.get("enabled", True):
                out[site] = None
                continue
            try:
                ph = int(cfg.get("per_hour") or 0)
            except (TypeError, ValueError):
                continue
            if ph > 0:
                out[site] = min(ph, 500)
        return out

    def _webchat_quota_for(self, site: str):
        """Лимит вызовов/час для сайта из llm.webchat_limits; без записи —
        без лимита (дефолт web_llm.QUOTA_PER_HOUR = None)."""
        from app.features.web_llm import QUOTA_PER_HOUR
        return self.webchat_limits.get(site, QUOTA_PER_HOUR)

    def _apply_webchat_limits(self):
        """Передать лимиты персоны уже созданным webchat-инстансам (кэш)."""
        for key, chat in self._webchats.items():
            site = key.split("#", 1)[0]
            try:
                chat.set_quota(self._webchat_quota_for(site))
            except Exception:
                pass

    def _webchat_pool_for(self, site: str) -> str | None:
        """Пул браузера для сайта из llm.webchat_mode: headless → 'h',
        hidden/headed → 'v'; без записи — None (дефолт WebChatLLM)."""
        mode = str((self.webchat_modes or {}).get(site) or "").lower()
        if mode == "headless":
            return "h"
        if mode in ("hidden", "headed"):
            return "v"
        return None

    def _try_webchat(self, messages, temperature: float, max_tokens: int,
                     top_p: float, timeout: float,
                     sites: list | None = None, channel: str = "main") -> str | None:
        """Попытка ответа через веб-чат (аккаунт пользователя в Chrome).
        sites — конкретные сайты в порядке перебора; None — все включённые.
        channel — «main» (ответы) или «side» (побочные задачи): разные чаты.
        None — выключен/недоступен/таймаут: цепочка идёт дальше.

        Канал main — пользовательский путь: лок инстанса ждём недолго
        (BURST_LOCK_WAIT_SEC). Занят (другая генерация, в т.ч. второй чат
        пользователя) — НЕ ждём: уходим в burst-инстанс (канал «burst»,
        разовый свежий чат с тем же полным контекстом — _join_messages и
        так шлёт его целиком, память сайта не нужна). Burst не кэшируется:
        свой свежий лок, чужую очередь не ждёт никогда."""
        try:
            from app.features.web_llm import WebChatLLM
        except Exception:
            return None
        for site in (sites if sites is not None else self.webchat_sites):
            try:
                key = site if channel == "main" else f"{site}#{channel}"
                chat = self._webchats.get(key)
                if chat is None:
                    chat = WebChatLLM(site, context=self.context, channel=channel,
                                      quota_per_hour=self._webchat_quota_for(site),
                                      browser_pool=self._webchat_pool_for(site))
                    self._webchats[key] = chat
                # Веб-чат медленный (стриминг + опрос DOM): минимум 150 сек
                eff_timeout = max(timeout, 150.0)
                answer = chat.get_response(
                    messages, temperature=temperature, max_tokens=max_tokens,
                    top_p=top_p, timeout=eff_timeout,
                    lock_timeout=(BURST_LOCK_WAIT_SEC
                                  if channel == "main" else None))
                if not answer and channel == "main" \
                        and getattr(chat, "last_call_lock_miss", False):
                    logger.info(f"[WebChat] {site}: основной чат занят — "
                                "отвечаю из свежего чата (burst)")
                    burst = WebChatLLM(
                        site, context=self.context, channel="burst",
                        quota_per_hour=self._webchat_quota_for(site),
                        browser_pool=self._webchat_pool_for(site))
                    answer = burst.get_response(
                        messages, temperature=temperature,
                        max_tokens=max_tokens, top_p=top_p, timeout=eff_timeout)
                if answer:
                    self._last_provider = f"webchat:{site}"
                    return answer
            except Exception as e:
                logger.warning(f"[WebChat] {site}: вызов не сработал: {e}")
        return None

    def _call_forced(self, provider: str, messages, temperature: float,
                     max_tokens: int, top_p: float, timeout: float,
                     webchat_channel: str = "main", on_token=None) -> str | None:
        """Одна попытка по назначенному провайдеру (llm.answer_provider/
        cc_provider) ВНЕ цепочки. None — провайдер недоступен/не ответил
        (caller идёт по обычной цепочке — fallback). on_token задан —
        стрим-вариант (веб-чат/локальный отдают одним куском, как в
        get_response_stream)."""
        try:
            if provider == "local":
                return self._try_local(messages, temperature, max_tokens, top_p,
                                       timeout, on_token)
            if provider == "webchat" or str(provider).startswith("webchat:"):
                sites = self.webchat_sites if provider == "webchat" \
                    else [provider.split(":", 1)[1]]
                answer = self._try_webchat(messages, temperature, max_tokens,
                                           top_p, timeout, sites, webchat_channel)
                if answer and on_token is not None:
                    on_token(answer)
                return answer
            if provider in self.available:
                cfg = self.available[provider]
                if on_token is not None:
                    return self._stream_with_keys(provider, cfg, messages, on_token,
                                                  temperature, max_tokens, top_p,
                                                  timeout)
                return self._call_with_keys(provider, cfg, messages, temperature,
                                            max_tokens, top_p, timeout)
        except Exception as e:
            logger.warning(f"[Router] назначенный провайдер {provider}: {e}")
        return None

    def _call_forced_vision(self, provider: str, text_prompt: str,
                            image_bytes: bytes, timeout: float,
                            image_mime: str, extra_image) -> str | None:
        """Назначенный vision-провайдер (llm.vision_provider): webchat:<сайт>
        с adapter['images'] или облачный с vision != false (auto — с автопробой,
        как в основной цепочке). None — недоступен: caller идёт по цепочке."""
        import base64
        try:
            if provider == "local":
                return None  # vision в локальном роутере нет
            if provider == "webchat" or str(provider).startswith("webchat:"):
                sites = self.webchat_sites if provider == "webchat" \
                    else [provider.split(":", 1)[1]]
                return self._try_webchat_image(text_prompt, image_bytes, timeout,
                                               sites=sites, image_mime=image_mime,
                                               extra_image=extra_image)
            if provider in self.available:
                cfg = self.available[provider]
                mode = str(cfg.get("vision", "auto")).lower()
                if mode == "false":
                    return None
                if mode == "auto":
                    verdict = self._vision_verdict.get(provider)
                    if verdict is None:
                        verdict = self._probe_vision(provider, cfg)
                    if not verdict:
                        return None
                img_b64 = base64.b64encode(image_bytes).decode()
                content = [
                    {"type": "text", "text": text_prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{image_mime};base64,{img_b64}"}},
                ]
                if extra_image:
                    content.append(
                        {"type": "image_url",
                         "image_url": {"url": f"data:{image_mime};base64,"
                                       + base64.b64encode(extra_image).decode()}})
                return self._call_with_keys(
                    provider, cfg, [{"role": "user", "content": content}],
                    temperature=0.2, max_tokens=1000, top_p=0.9,
                    timeout=timeout)
        except Exception as e:
            logger.warning(f"[Router] назначенный vision-провайдер {provider}: {e}")
        return None

    def set_persona_llm(self, primary: str | None, fallback: list[str] | None = None,
                        models: dict | None = None, webchat: str | None = None,
                        webchat_limits: dict | None = None,
                        webchat_modes: dict | None = None,
                        answer_provider: str | None = None,
                        cc_provider: str | None = None,
                        vision_provider: str | None = None):
        """Персональный override провайдеров (YAML персоны, секция llm).

        primary — основной провайдер персоны ('local', 'webchat' (все сайты),
        'webchat:<сайт>' или id из PROVIDER_CONFIGS); None — снять закрепление,
        вернуться к глобальному ACTIVE_PROVIDER. fallback — приоритет цепочки
        после основного; 'local', 'webchat' и 'webchat:<сайт>' учитываются на
        своих позициях (голый 'webchat' разворачивается в текущие сайты).
        models — свои модели по провайдерам; webchat — сайт веб-чата
        (deepseek|qwen|claude), None — оставить как есть (env WEBCHAT_SITES).
        webchat_limits — {сайт: {"enabled": bool, "per_hour": int}}: лимит
        вызовов в час на сайт; enabled:false — снять; None — как есть.
        webchat_modes — {сайт: headless|hidden|headed}: в каком пуле Chrome
        держать вкладку сайта (web_extended); None — дефолты."""
        from app.features.web_llm import ADAPTERS as _WC_ADAPTERS

        if webchat_modes is not None:
            modes = {}
            for site, mode in (webchat_modes or {}).items():
                m = str(mode).strip().lower()
                if site in _WC_ADAPTERS and m in ("headless", "hidden", "headed"):
                    modes[site] = m
                else:
                    logger.warning(f"[Router] webchat_mode: пропуск {site!r}={mode!r} "
                                   f"(нет такого сайта/режима)")
            self.webchat_modes = modes

        if webchat_limits is not None:
            self.webchat_limits = self._norm_webchat_limits(webchat_limits)
            self._apply_webchat_limits()

        if webchat is not None:
            site = str(webchat).strip().lower()
            if site in _WC_ADAPTERS:
                self.webchat_sites = [site]
                logger.info(f"Веб-чат провайдер персоны: {site}")
            else:
                logger.warning(f"Неизвестный webchat-сайт '{webchat}' — "
                               f"есть: {', '.join(_WC_ADAPTERS)}")

        def _norm_token(p):
            # 'webchat:<сайт>' — валидный токен при известном адаптере; персона
            # может включить себе сайт, которого нет в глобальном списке
            if isinstance(p, str) and p.startswith("webchat:"):
                return p if p.split(":", 1)[1] in _WC_ADAPTERS else None
            return p if p in PROVIDER_CONFIGS or p == "local" else None

        # Провайдеры по назначению (кейс 18.09): answer — текст ответа
        # пользователю, cc — внутренние решения режима управления (разбор
        # команды, резолв элементов страницы), vision — картинки.
        # None/неизвестный токен — обычная цепочка.
        for attr, val in (("answer_provider", answer_provider),
                          ("cc_provider", cc_provider),
                          ("vision_provider", vision_provider)):
            tok = _norm_token(val) if val else None
            if val and not tok:
                logger.warning(f"[Router] llm.{attr}: неизвестный провайдер "
                               f"{val!r} — обычная цепочка")
            setattr(self, attr, tok)

        if fallback:
            norm: list[str] = []
            for p in fallback:
                if p == "webchat":
                    # Голый 'webchat' — все включённые сайты на этой позиции
                    norm.extend(f"webchat:{s}" for s in self.webchat_sites)
                    continue
                tok = _norm_token(p)
                if tok and tok not in norm:
                    norm.append(tok)
            self.fallback_order = norm
        else:
            self.fallback_order = None

        self.model_overrides = {
            p: str(m).strip()
            for p, m in (models or {}).items()
            if p in PROVIDER_CONFIGS and isinstance(m, str) and m.strip()
        }

        if primary:
            if primary == "webchat" and not self.webchat_sites:
                # primary=webchat без сайтов — дефолт qwen (бесплатный веб-чат)
                self.webchat_sites = _parse_webchat_sites() or ["qwen"]
            if isinstance(primary, str) and primary.startswith("webchat:"):
                site = primary.split(":", 1)[1]
                if site in _WC_ADAPTERS:
                    if site not in self.webchat_sites:
                        self.webchat_sites = self.webchat_sites + [site]
                    self.active_provider = primary
                    self.pinned_provider = primary
                    logger.info(f"Персональный основной провайдер: {primary} (fallback: {self.fallback_order})")
                else:
                    logger.warning(f"Неизвестный основной провайдер персоны '{primary}' — игнорируем")
            elif primary in ("local", "webchat") or primary in PROVIDER_CONFIGS:
                self.active_provider = primary
                self.pinned_provider = primary
                logger.info(f"Персональный основной провайдер: {primary} (fallback: {self.fallback_order})")
            else:
                logger.warning(f"Неизвестный основной провайдер персоны '{primary}' — игнорируем")
        else:
            self.pinned_provider = None
            # Снятие закрепления — возвращаем глобального активного
            global_active = os.getenv("ACTIVE_PROVIDER")
            self.active_provider = (
                global_active
                if global_active in self.available or global_active == "local"
                else next(iter(self.available), self.active_provider)
            )

    def is_local_primary(self) -> bool:
        """Первым отвечает локальная модель (Ollama). Слабым моделям большой
        контекст вредит — по этому флагу контекст основного ответа собирается
        в урезанном виде (см. BotInstance.process_message)."""
        return self.active_provider == "local"

    def _get_provider_order(self) -> list:
        # active == "local" здесь не обрывает цепочку: локальная пробуется
        # первой в get_response/_stream, облачные из этого списка — fallback.
        available_keys = list(self.available.keys())

        order = []
        if self.active_provider in available_keys:
            order.append(self.active_provider)
        # Персональный приоритет fallback (из YAML персоны), затем остальные
        for p in (self.fallback_order or []):
            if p in available_keys and p not in order:
                order.append(p)
        order += [p for p in available_keys if p not in order]

        if not order and self.active_provider != "local":
            logger.warning("Ни один облачный провайдер не имеет ключей — уйдём в локальный fallback")
        return order

    def _webchat_tokens(self) -> list[str]:
        """Токены webchat:<сайт> для цепочки: все включённые сайты плюс сайты
        из персонального primary/fallback (персона может включить себе сайт,
        которого нет в глобальном списке)."""
        sites = list(self.webchat_sites)
        extra = list(self.fallback_order or [])
        if self.pinned_provider:
            extra.append(self.pinned_provider)
        for tok in extra:
            if isinstance(tok, str) and tok.startswith("webchat:"):
                site = tok.split(":", 1)[1]
                if site not in sites:
                    sites.append(site)
        return [f"webchat:{s}" for s in sites]

    def _get_full_order(self) -> list:
        """Полный порядок перебора: облачная цепочка (_get_provider_order)
        плюс спец-провайдеры 'webchat:<сайт>' и 'local'. Персональный
        fallback-список задаёт позиции явно (в т.ч. когда основной — local:
        он пробуется первой отдельной веткой и в цепочку не дублируется);
        без списка — веб-чаты после облачных, local последним. Основной
        'local'/'webchat*' сюда не попадает: он пробуется первым отдельной
        веткой в get_response."""
        clouds = self._get_provider_order()
        wc_tokens = self._webchat_tokens()
        active_is_local = self.active_provider == "local"
        fb = self.fallback_order or []
        order: list = []
        if self.active_provider in self.available:
            order.append(self.active_provider)
        for p in fb:
            if p in self.available and p not in order:
                order.append(p)
            elif p == "local":
                if not active_is_local and p not in order:
                    order.append(p)
            elif (isinstance(p, str) and p.startswith("webchat:")
                  and p in wc_tokens and p not in order):
                order.append(p)
        tail = clouds + wc_tokens + ([] if active_is_local else ["local"])
        for p in tail:
            if p not in order:
                order.append(p)
        return order

    def supports_vision(self) -> bool:
        """Может ли роутер обработать изображение (vision-провайдер или режим auto)."""
        for name, cfg in self.available.items():
            mode = cfg.get("vision", "auto")
            if mode is True or str(mode).lower() == "true":
                return True
            if self._vision_verdict.get(name):
                return True
            if str(mode).lower() == "auto":
                return True  # auto = потенциально да, проверим пробой
        # Веб-чаты с подтверждённым приёмом картинок (adapter["images"]) —
        # vision-источник даже при мёртвых облачных ключах
        try:
            from app.features.web_llm import ADAPTERS
            sites = [t.split(":", 1)[1] for t in self._webchat_tokens()]
            if any(ADAPTERS.get(s, {}).get("images") for s in sites):
                return True
        except Exception:
            pass
        return False

    def _probe_vision(self, provider: str, cfg: dict) -> bool:
        """
        Автопроба vision: шлём провайдеру крошечную картинку с цифрой «42» и
        проверяем, что модель её реально увидела (ответила «42»). Текстовая модель
        не сможет угадать — ложных срабатываний почти нет. Вердикт кешируется —
        кроме ответа-ошибки (429/таймаут при пробе ≠ «модель слепая», транзиент
        не должен выключать vision до конца процесса).
        """
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "What number is written in this image? Reply with just the number."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_VISION_PROBE_IMAGE_B64}"}},
            ],
        }]
        try:
            # max_tokens с запасом: reasoning-модели (k3 и т.п.) тратят бюджет
            # на скрытые размышления, при малом лимите ответ пустой
            answer = self._call_with_keys(
                provider, cfg, messages,
                temperature=0.0, max_tokens=300, top_p=1.0, timeout=30.0,
            )
            if answer is None:
                # все ключи упали (429/403/таймаут) — транзиент, не вердикт
                # о слепоте модели: не кешируем, иначе одна неудачная проба
                # выключала vision до конца процесса
                logger.info(f"[Vision probe] {provider}: вызов не прошёл — "
                            "вердикт не кешируем")
                return False
            verdict = "42" in answer
            self._vision_verdict[provider] = verdict
        except Exception as e:
            logger.info(f"[Vision probe] {provider}: ошибка ({e}) — "
                        "вердикт не кешируем")
            verdict = False
        logger.info(f"[Vision probe] {provider}/{self.model_for(provider)}: {'ПОДДЕРЖИВАЕТ' if verdict else 'НЕ поддерживает'} изображения")
        return verdict

    def get_response_with_image(self, text_prompt: str, image_bytes: bytes,
                                max_tokens: int = 1000, timeout: float = 90.0,
                                image_mime: str = "image/jpeg",
                                extra_image: bytes | None = None,
                                force_provider: str = None
                                ) -> str | None:
        """
        Отправляет изображение vision-модели (OpenAI-совместимый формат image_url).
        extra_image — второй кадр в том же сообщении (чистый скриншот страницы
        без разметки — модель видит, что закрыто рамками/бейджами). Для
        веб-чатов вставка второго кадра — best effort: не прикрепился —
        отвечаем по одному.

        Для каждого провайдера режим определяется флагом vision в конфиге:
        - "true"  — используем без проверки;
        - "false" — пропускаем;
        - "auto"  — при первом изображении делаем автопробу (крошечная тестовая
                    картинка) и кешируем вердикт на время жизни процесса.

        Цепочка — та же, что у текстового get_response: основной провайдер
        (закреплённый персоной или глобальный; веб-чат — отдельной первой
        веткой), дальше _get_full_order() — fallback-список персоны на своих
        позициях, хвост: облака → веб-чаты → local. Изображение умеют:
        облачные с флагом vision ("true" сразу, "auto" — после автопробы
        с кешем вердикта, "false" — пропуск) и веб-чаты с adapter["images"]
        (картинка вставляется в композер paste-событием); local пропускаем
        (vision в локальном роутере нет).

        Возвращает ответ или None, если vision-провайдеры недоступны/ошиблись.
        """
        import base64
        img_b64 = base64.b64encode(image_bytes).decode()
        content = [
            {"type": "text", "text": text_prompt},
            {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{img_b64}"}},
        ]
        if extra_image:
            ex_b64 = base64.b64encode(extra_image).decode()
            content.append(
                {"type": "image_url",
                 "image_url": {"url": f"data:{image_mime};base64,{ex_b64}"}})
        messages = [{"role": "user", "content": content}]

        # Назначенный vision-провайдер (llm.vision_provider): одна попытка
        # вне цепочки, неудача — обычная цепочка
        if force_provider:
            answer = self._call_forced_vision(force_provider, text_prompt,
                                              image_bytes, timeout, image_mime,
                                              extra_image)
            if answer:
                return answer
            logger.info(f"[Router] назначенный vision-провайдер "
                        f"{force_provider} не ответил — обычная цепочка")

        tried_webchats: set[str] = set()
        # Основной провайдер — веб-чат: пробуем его первым, как в get_response
        # (в _get_full_order он не входит — там он «отдельной веткой»)
        if (self.active_provider == "webchat"
                or str(self.active_provider).startswith("webchat:")):
            sites = self.webchat_sites if self.active_provider == "webchat" \
                else [self.active_provider.split(":", 1)[1]]
            tried_webchats.update(sites)
            answer = self._try_webchat_image(text_prompt, image_bytes, timeout,
                                             sites=sites,
                                             image_mime=image_mime,
                                             extra_image=extra_image)
            if answer:
                return answer

        for provider in self._get_full_order():
            if provider == "local":
                continue  # vision в локальном роутере нет
            if isinstance(provider, str) and provider.startswith("webchat"):
                # Веб-чат — на своей позиции из fallback-списка персоны
                sites = self.webchat_sites if provider == "webchat" \
                    else [provider.split(":", 1)[1]]
                sites = [s for s in sites if s not in tried_webchats]
                if not sites:
                    continue
                tried_webchats.update(sites)
                answer = self._try_webchat_image(text_prompt, image_bytes,
                                                 timeout, sites=sites,
                                                 image_mime=image_mime,
                                                 extra_image=extra_image)
                if answer:
                    return answer
                continue
            cfg = self.available[provider]
            mode = str(cfg.get("vision", "auto")).lower()
            if mode == "false":
                continue
            if mode == "auto":
                verdict = self._vision_verdict.get(provider)
                if verdict is None:
                    verdict = self._probe_vision(provider, cfg)
                if not verdict:
                    continue
            # mode == "true" или подтверждённый auto
            answer = self._call_with_keys(
                provider, cfg, messages,
                temperature=0.2, max_tokens=max_tokens, top_p=0.9,
                timeout=timeout,
            )
            if answer:
                return answer
            logger.error(f"Vision-провайдер {provider.upper()} не ответил, пробуем следующий...")

        return None

    def _try_webchat_image(self, text_prompt: str, image_bytes: bytes,
                           timeout: float,
                           sites: list | None = None,
                           image_mime: str = "image/jpeg",
                           extra_image: bytes | None = None) -> str | None:
        """Vision через веб-чат: картинка вставляется в композер синтетическим
        paste. Только сайты с adapter['images'] (приём проверен вручную).
        sites — конкретные сайты в порядке перебора; None — все включённые.
        Отдельный канал 'vision', чтобы скриншоты не мусорили в основном чате.
        None — ни один сайт не ответил."""
        try:
            from app.features.web_llm import ADAPTERS, WebChatLLM
        except Exception:
            return None
        if sites is None:
            sites = [t.split(":", 1)[1] for t in self._webchat_tokens()]
        for site in sites:
            if not ADAPTERS.get(site, {}).get("images"):
                continue
            try:
                key = f"{site}#vision"
                chat = self._webchats.get(key)
                if chat is None:
                    chat = WebChatLLM(site, context=self.context,
                                      channel="vision",
                                      quota_per_hour=self._webchat_quota_for(site),
                                      browser_pool=self._webchat_pool_for(site))
                    self._webchats[key] = chat
                answer = chat.get_response_with_image(
                    text_prompt, image_bytes, timeout=max(timeout, 150.0),
                    image_mime=image_mime, extra_image=extra_image)
                if answer:
                    self._last_provider = f"webchat:{site}"
                    return answer
            except Exception as e:
                logger.warning(f"[WebChat] {site}: vision-вызов не сработал: {e}")
        return None

    def get_provider_model_info(self) -> str:
        provider = self._last_provider or self.active_provider
        if provider == "local":
            return f"local/{getattr(self, '_last_local_model', '?')}"
        if isinstance(provider, str) and provider.startswith("webchat"):
            # webchat:qwen → webchat/qwen
            return provider.replace(":", "/")
        if provider in self.available:
            cfg = self.available[provider]
            idx = self._last_key_index.get(provider, 0) + 1
            total = len(cfg["api_keys"])
            key_suffix = f"/key{idx}" if total > 1 else ""
            return f"{provider}/{self.model_for(provider)}{key_suffix}"
        return f"{provider}/?"
