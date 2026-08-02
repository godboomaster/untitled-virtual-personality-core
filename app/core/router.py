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


class ModelRouter:
    def __init__(self, provider: str = None):
        self.available = get_available_providers()
        self.active_provider = provider or os.getenv("ACTIVE_PROVIDER")
        self._last_key_index: dict[str, int] = {}

        if not self.available:
            # Нет ни одного облачного ключа — пробуем жить полностью на
            # локальной модели (Ollama). Все вызовы get_response уйдут в
            # локальный fallback в конце цепочки.
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

    def _call_with_keys(self, provider: str, cfg: dict, messages: list,
                        temperature: float, max_tokens: int, top_p: float,
                        timeout: float) -> str | None:
        
        # Пробует все ключи провайдера по очереди. Возвращает ответ или None.
        keys = cfg["api_keys"]
        last_idx = self._last_key_index.get(provider, 0)

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
                    f"[LLM Request] {provider}/{cfg['model']} "
                    f"| key={idx + 1}/{len(keys)} "
                    f"| max_tokens={max_tokens} | messages={len(messages)}"
                )
                response = client.chat.completions.create(
                    model=cfg["model"], messages=messages,
                    temperature=cfg.get("temperature", temperature),
                    max_tokens=max_tokens, top_p=cfg.get("top_p", top_p)
                )
                answer = response.choices[0].message.content
                self._last_provider = provider
                self._last_key_index[provider] = idx
                logger.debug(f"[Response] {provider}/{cfg['model']} key={idx + 1} | len={len(answer) if answer else 0}")
                return answer
            except Exception as e:
                logger.warning(
                    f"{provider.upper()} key={idx + 1}/{len(keys)} ({cfg['model']}) ошибка: {e}"
                )

        return None

    def get_response(self, messages, temperature: float = 0.7,
                     max_tokens: int = 2000, top_p: float = 0.9,
                     exclude_provider: str = None, timeout: float = 60.0) -> str | None:
        """Возвращает ответ модели или None, если все провайдеры недоступны.

        Вызывающий код ОБЯЗАН проверять результат на None/пустоту — строка-заглушка
        больше не возвращается, чтобы ошибку нельзя было принять за ответ модели.
        """
        provider_order = self._get_provider_order()

        if exclude_provider and len(provider_order) > 1:
            provider_order = [p for p in provider_order if p != exclude_provider]

        for provider in provider_order:
            cfg = self.available[provider]
            answer = self._call_with_keys(
                provider, cfg, messages, temperature, max_tokens, top_p, timeout
            )
            if answer:
                return answer
            logger.error(f"Провайдер {provider.upper()} не ответил ни одним ключом, переключаемся...")

        # Последний fallback — локальная модель (Ollama), если запущена.
        # Импорт ленивый: синглтон делает сетевой запрос к Ollama при создании.
        try:
            from app.core.local_router import get_local_router
            local = get_local_router()
            if local.is_available():
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
                    self._last_provider = "local"
                    self._last_local_model = local.model
                    logger.warning(f"Все облачные провайдеры недоступны — ответ локальной модели {local.model}")
                    return answer
        except Exception as e:
            logger.warning(f"Локальный fallback не сработал: {e}")

        return None

    def _get_provider_order(self) -> list:
        if self.active_provider == "local":
            return []  # офлайн-режим без облачных ключей: сразу локальный fallback
        available_keys = list(self.available.keys())

        if self.active_provider in available_keys:
            order = [self.active_provider]
            order += [p for p in available_keys if p != self.active_provider]
            return order

        logger.warning(f"Активный провайдер '{self.active_provider}' не имеет ключей, fallback на {available_keys}")
        return available_keys

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
        return False

    def _probe_vision(self, provider: str, cfg: dict) -> bool:
        """
        Автопроба vision: шлём провайдеру крошечную картинку с цифрой «42» и
        проверяем, что модель её реально увидела (ответила «42»). Текстовая модель
        не сможет угадать — ложных срабатываний почти нет. Вердикт кешируется.
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
            verdict = bool(answer and "42" in answer)
        except Exception as e:
            logger.info(f"[Vision probe] {provider}: ошибка ({e})")
            verdict = False
        logger.info(f"[Vision probe] {provider}/{cfg['model']}: {'ПОДДЕРЖИВАЕТ' if verdict else 'НЕ поддерживает'} изображения")
        self._vision_verdict[provider] = verdict
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

        for provider in self._get_provider_order():
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

    def get_provider_model_info(self) -> str:
        provider = self._last_provider or self.active_provider
        if provider == "local":
            return f"local/{getattr(self, '_last_local_model', '?')}"
        if provider in self.available:
            cfg = self.available[provider]
            idx = self._last_key_index.get(provider, 0) + 1
            total = len(cfg["api_keys"])
            key_suffix = f"/key{idx}" if total > 1 else ""
            return f"{provider}/{cfg['model']}{key_suffix}"
        return f"{provider}/?"
