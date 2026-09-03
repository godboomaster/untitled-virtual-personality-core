import os
import logging
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
        known = {"deepseek", "qwen", "claude", "zai", "chatgpt"}
    out: list[str] = []
    for tok in raw.split(","):
        site = tok.strip().lower()
        if site and site in known and site not in out:
            out.append(site)
    return out


class ModelRouter:
    def __init__(self, provider: str = None, context: str = "default"):
        # context — изоляция состояния веб-чатов персоны: у каждой свой
        # постоянный чат на сайте (data/{context}/computer_control), иначе
        # все персоны писали бы в один разговор и видели чужой контекст
        self.context = context or "default"
        self.available = get_available_providers()
        self.active_provider = provider or os.getenv("ACTIVE_PROVIDER")
        self._last_key_index: dict[str, int] = {}
        # Персональный override из YAML персоны (секция llm): закреплённый
        # основной провайдер (глобальная смена active его не трогает),
        # приоритет fallback-цепочки и свои модели по провайдерам.
        self.pinned_provider: str | None = None
        self.fallback_order: list[str] | None = None
        self.model_overrides: dict[str, str] = {}
        # Веб-чаты как провайдеры без ключей (WEBCHAT_SITES=qwen,deepseek —
        # порядок перебора; legacy WEBCHAT_SITE — один сайт). Пусто — выключены.
        # Персона может включить/переставить их секцией llm (primary/fallback:
        # токены webchat:<сайт>; llm.webchat — сайт по умолчанию).
        self.webchat_sites: list[str] = _parse_webchat_sites()
        self._webchats: dict = {}  # site -> ленивый web_llm.WebChatLLM
        # Лимиты веб-чатов персоны (llm.webchat_limits): {сайт: per_hour|None}
        # None — лимит снят; сайта нет в dict — дефолт web_llm.QUOTA_PER_HOUR
        self.webchat_limits: dict = {}

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

    def _call_with_keys(self, provider: str, cfg: dict, messages: list,
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
                     webchat_channel: str = "main") -> str | None:
        """Возвращает ответ модели или None, если все провайдеры недоступны.

        Вызывающий код ОБЯЗАН проверять результат на None/пустоту — строка-заглушка
        больше не возвращается, чтобы ошибку нельзя было принять за ответ модели.
        """
        provider_order = self._get_full_order()

        if exclude_provider and len(provider_order) > 1:
            if exclude_provider == "webchat":
                # голый 'webchat' исключает все веб-чаты разом
                provider_order = [p for p in provider_order
                                  if not (isinstance(p, str) and p.startswith("webchat"))]
            else:
                provider_order = [p for p in provider_order if p != exclude_provider]

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
        tried_local = False
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
                            webchat_channel: str = "main") -> str | None:
        """Стриминговый вариант get_response: токены уходят в on_token(delta) по мере
        генерации, возвращается полный текст. Fallback на другой ключ/провайдер —
        только до первого токена; обрыв посередине — возвращаем накопленное.
        Локальный fallback (Ollama) не стримится — отдаётся одним куском."""
        provider_order = self._get_full_order()

        if exclude_provider and len(provider_order) > 1:
            if exclude_provider == "webchat":
                provider_order = [p for p in provider_order
                                  if not (isinstance(p, str) and p.startswith("webchat"))]
            else:
                provider_order = [p for p in provider_order if p != exclude_provider]

        # Основной провайдер — локальная модель: первая попытка, облачные — fallback
        tried_local = False
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
        """Лимит вызовов/час для сайта: персональный override (None — снят)
        или дефолт web_llm.QUOTA_PER_HOUR."""
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

    def _try_webchat(self, messages, temperature: float, max_tokens: int,
                     top_p: float, timeout: float,
                     sites: list | None = None, channel: str = "main") -> str | None:
        """Попытка ответа через веб-чат (аккаунт пользователя в Chrome).
        sites — конкретные сайты в порядке перебора; None — все включённые.
        channel — «main» (ответы) или «side» (побочные задачи): разные чаты.
        None — выключен/недоступен/таймаут: цепочка идёт дальше."""
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
                                      quota_per_hour=self._webchat_quota_for(site))
                    self._webchats[key] = chat
                # Веб-чат медленный (стриминг + опрос DOM): минимум 150 сек
                answer = chat.get_response(
                    messages, temperature=temperature, max_tokens=max_tokens,
                    top_p=top_p, timeout=max(timeout, 150.0))
                if answer:
                    self._last_provider = f"webchat:{site}"
                    return answer
            except Exception as e:
                logger.warning(f"[WebChat] {site}: вызов не сработал: {e}")
        return None

    def set_persona_llm(self, primary: str | None, fallback: list[str] | None = None,
                        models: dict | None = None, webchat: str | None = None,
                        webchat_limits: dict | None = None):
        """Персональный override провайдеров (YAML персоны, секция llm).

        primary — основной провайдер персоны ('local', 'webchat' (все сайты),
        'webchat:<сайт>' или id из PROVIDER_CONFIGS); None — снять закрепление,
        вернуться к глобальному ACTIVE_PROVIDER. fallback — приоритет цепочки
        после основного; 'local', 'webchat' и 'webchat:<сайт>' учитываются на
        своих позициях (голый 'webchat' разворачивается в текущие сайты).
        models — свои модели по провайдерам; webchat — сайт веб-чата
        (deepseek|qwen|claude), None — оставить как есть (env WEBCHAT_SITES).
        webchat_limits — {сайт: {"enabled": bool, "per_hour": int}}: лимит
        вызовов в час на сайт; enabled:false — снять; None — как есть."""
        from app.features.web_llm import ADAPTERS as _WC_ADAPTERS

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
                                image_mime: str = "image/jpeg") -> str | None:
        """
        Отправляет изображение vision-модели (OpenAI-совместимый формат image_url).

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
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": text_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{img_b64}"}},
            ],
        }]

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
                                             image_mime=image_mime)
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
                                                 image_mime=image_mime)
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
                           image_mime: str = "image/jpeg") -> str | None:
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
                                      quota_per_hour=self._webchat_quota_for(site))
                    self._webchats[key] = chat
                answer = chat.get_response_with_image(
                    text_prompt, image_bytes, timeout=max(timeout, 150.0),
                    image_mime=image_mime)
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
