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

        logger.info(f"ModelRouter: active={self.active_provider} | available={list(self.available.keys())}")

    def get_response(self, messages, temperature: float = 0.7,
                     max_tokens: int = 2000, top_p: float = 0.9,
                     exclude_provider: str = None, timeout: float = 60.0) -> str:
        provider_order = self._get_provider_order()

        # Пропускаем провайдер, занятый другой задачей
        if exclude_provider and len(provider_order) > 1:
            provider_order = [p for p in provider_order if p != exclude_provider]

        for provider in provider_order:
            try:
                cfg = self.available[provider]
                client = OpenAI(
                    api_key=cfg["api_key"],
                    base_url=cfg["base_url"],
                    timeout=timeout
                )
                logger.debug(f"[LLM Request] {provider}/{cfg['model']} | max_tokens={max_tokens} | messages={len(messages)} | timeout={timeout}")
                response = client.chat.completions.create(
                    model=cfg["model"], messages=messages,
                    temperature=temperature, max_tokens=max_tokens, top_p=top_p
                )
                answer = response.choices[0].message.content
                self._last_provider = provider
                logger.debug(f"[Response] {provider}/{cfg['model']} | len={len(answer) if answer else 0}")
                return answer
            except Exception as e:
                logger.error(f"{provider.upper()} ({cfg['model']}) не сработал: {e}")

        return "Ошибка: все провайдеры недоступны."

    def _get_provider_order(self) -> list:
        # Строит порядок fallback: активный первый, остальные за ним.
        available_keys = list(self.available.keys())

        if self.active_provider in available_keys:
            order = [self.active_provider]
            order += [p for p in available_keys if p != self.active_provider]
            return order

        # Активный провайдер недоступен (нет ключа) — берём по порядку из словаря
        logger.warning(f"Активный провайдер '{self.active_provider}' не имеет API_KEY, fallback на {available_keys}")
        return available_keys

    def get_provider_model_info(self) -> str:
        # Возвращает строку 'provider/model' для логирования.
        # Показывает реального провайдера (last_provider), а не active.
        provider = self._last_provider or self.active_provider
        if provider in self.available:
            cfg = self.available[provider]
            return f"{provider}/{cfg['model']}"
        return f"{provider}/?"
