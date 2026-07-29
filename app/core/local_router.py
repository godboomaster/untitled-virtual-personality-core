"""
Локальный LLM роутер через Ollama.

Используется для лёгких бинарных классификаций:
- need_search: SEARCH / SKIP
- self_memory: SKIP / NOTE
- proactive: МОЛЧУ / мысль

Преимущества: быстро, дёшево (бесплатно), приватно.
"""

import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma3:4b"
DEFAULT_TIMEOUT = 15.0


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
        self.model = model or os.getenv("OLLAMA_MODEL", DEFAULT_MODEL)
        self.timeout = timeout

        self._client = httpx.Client(timeout=timeout)
        self._last_check = 0.0  # для периодической пере-проверки в is_available()
        self._available = self._check_available()

        if self._available:
            logger.info(f"[LocalLLM] Подключен к Ollama: {self.base_url}, модель: {self.model}")
        else:
            logger.warning(
                f"[LocalLLM] Ollama недоступен по {self.base_url}. "
                f"Бинарные классификаторы будут fallback на основной роутер."
            )

    def _check_available(self) -> bool:
        """Проверяет доступность Ollama."""
        try:
            resp = self._client.get(f"{self.base_url}/api/tags", timeout=5.0)
            if resp.status_code != 200:
                return False
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            # Ollama хранит имя с тегом: gemma3 -> gemma3:latest
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

    def is_available(self) -> bool:
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
    ) -> Optional[str]:
        """
        Отправляет запрос к локальной модели.
        Возвращает текст ответа или None при ошибке.
        """
        if not self._available:
            return None

        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "top_p": top_p,
                    "num_predict": max_tokens,
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
        Требует мультимодальную модель (gemma3). Возвращает None при ошибке.
        """
        if not self.is_available():
            return None

        try:
            import base64
            img_b64 = base64.b64encode(image_bytes).decode()

            prompt = (
                "Пользователь прислал изображение. Вытащи с него весь видимый текст (OCR) "
                "и коротко опиши, что изображено (1-2 предложения).\n"
                "Формат ответа:\nТЕКСТ: <текст с изображения или «нет текста»>\nОПИСАНИЕ: <...>"
            )
            if question:
                prompt += f"\nДополнительно ответь на вопрос пользователя об изображении: {question}"

            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt, "images": [img_b64]}],
                "stream": False,
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
