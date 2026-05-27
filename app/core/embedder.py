"""
Использует HuggingFace Inference API для вычисления эмбеддингов.
Размер вектора: 384 (совместим с all-MiniLM-L6-v2).
"""

import os
import httpx
from app.core.config import PROVIDER_CONFIGS


class Embedder:
    #Вычисляет эмбеддинги через HF Inference API.

    MODEL = "sentence-transformers/all-MiniLM-L6-v2"
    API_URL = f"https://api-inference.huggingface.co/pipeline/feature-extraction/{MODEL}"

    def __init__(self):
        self._api_key = PROVIDER_CONFIGS["hf"]["api_keys"][0]

    def encode(self, text: str | list[str]) -> list[float] | list[list[float]]:
        #Возвращает эмбеддинги для текста. Совместим с SentenceTransformer.encode().
        if isinstance(text, str):
            return self._encode_single(text)
        return [self._encode_single(t) for t in text]

    def _encode_single(self, text: str) -> list[float]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {"inputs": text}

        response = httpx.post(
            self.API_URL,
            headers=headers,
            json=payload,
            timeout=30.0,
        )
        response.raise_for_status()
        result = response.json()

        # feature-extraction возвращает [[...]] — берём первый элемент
        if isinstance(result, list) and len(result) > 0:
            vector = result[0]
            if isinstance(vector, list):
                return vector
        raise ValueError(f"Unexpected embedding response: {result}")
