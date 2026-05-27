import os
import logging
from openai import OpenAI
from app.core.config import PROVIDER_CONFIGS, get_available_providers

logger = logging.getLogger(__name__)


class ModelRouter:
    def __init__(self, provider: str = None):
        self.available = get_available_providers()
        self.active_provider = provider or os.getenv("ACTIVE_PROVIDER")
        self._last_provider = self.active_provider
        self._last_key_index: dict[str, int] = {}

        if not self.available:
            logger.critical(
                "Нет доступных провайдеров! "
                "Задайте API-ключ хотя бы для одного провайдера в .env или .env.config "
                "(например, ZAI_API_KEY=..., OPENAI_API_KEY=...)."
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
                    temperature=temperature, max_tokens=max_tokens, top_p=top_p
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
                     exclude_provider: str = None, timeout: float = 60.0) -> str:
        provider_order = self._get_provider_order()

        if exclude_provider and len(provider_order) > 1:
            provider_order = [p for p in provider_order if p != exclude_provider]

        for provider in provider_order:
            cfg = self.available[provider]
            answer = self._call_with_keys(
                provider, cfg, messages, temperature, max_tokens, top_p, timeout
            )
            if answer is not None:
                return answer
            logger.error(f"Провайдер {provider.upper()} не ответил ни одним ключом, переключаемся...")

        return "Ошибка: все провайдеры недоступны."

    def _get_provider_order(self) -> list:
        available_keys = list(self.available.keys())

        if self.active_provider in available_keys:
            order = [self.active_provider]
            order += [p for p in available_keys if p != self.active_provider]
            return order

        logger.warning(f"Активный провайдер '{self.active_provider}' не имеет ключей, fallback на {available_keys}")
        return available_keys

    def get_provider_model_info(self) -> str:
        provider = self._last_provider or self.active_provider
        if provider in self.available:
            cfg = self.available[provider]
            idx = self._last_key_index.get(provider, 0) + 1
            total = len(cfg["api_keys"])
            key_suffix = f"/key{idx}" if total > 1 else ""
            return f"{provider}/{cfg['model']}{key_suffix}"
        return f"{provider}/?"
