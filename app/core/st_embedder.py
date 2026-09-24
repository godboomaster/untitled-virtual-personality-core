"""SentenceTransformer-эмбеддер, устойчивый к отсутствию интернета.

Без сети загрузка модели идёт на huggingface.co за HEAD-проверками (ретраи
с backoff на каждый файл), а при параллельных загрузках глобальный httpx-
клиент hub'а падает с RuntimeError — вместе с созданием бота (500 на
/api/chat). Если hub недоступен и модель в кэше — включаем offline-режим
HF: загрузка идёт из кэша без единого HTTP-запроса. Любая другая неудача
при живом кэше — один повтор в offline. Режим липкий (на весь процесс):
вернувшаяся сеть его не выключает.
"""

import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger(__name__)

ST_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

_hub_reachable_cache = None


def _model_cached(model_name: str) -> bool:
    # Модель есть в кэше HF (хотя бы один снапшот). Короткое имя без
    # префикса namespace sentence-transformers резолвит в свой — проверяем
    # оба варианта.
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        names = [model_name] if "/" in model_name \
            else [model_name, f"sentence-transformers/{model_name}"]
        for name in names:
            folder = "models--" + name.replace("/", "--")
            snaps = Path(HF_HUB_CACHE) / folder / "snapshots"
            if snaps.is_dir() and any(snaps.iterdir()):
                return True
        return False
    except Exception:
        return False


def _hub_reachable(timeout: float = 2.0) -> bool:
    # Один probe на процесс: отвечает ли huggingface.co по сети.
    global _hub_reachable_cache
    if _hub_reachable_cache is None:
        try:
            socket.create_connection(("huggingface.co", 443), timeout=timeout).close()
            _hub_reachable_cache = True
        except OSError:
            _hub_reachable_cache = False
    return _hub_reachable_cache


def force_hf_offline():
    # Offline-режим huggingface_hub на лету: env (для поздних импортов) +
    # константа (is_offline_mode читает её динамически — HTTP не выполняется,
    # cached_files сразу уходит в кэш).
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        from huggingface_hub import constants
        constants.HF_HUB_OFFLINE = True
    except Exception:
        pass


def create_st_embedder(model_name: str = ST_MODEL_NAME):
    """SentenceTransformerEmbeddingFunction с offline-фолбэком по кэшу HF.

    Офлайн + кэш — загрузка из кэша без сети. Без кэша — понятная ошибка:
    модель надо скачать один раз при живом интернете.
    """
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    cached = _model_cached(model_name)
    if cached and not _hub_reachable():
        logger.info(f"[Embedder] huggingface.co недоступен — {model_name} "
                    "грузится из кэша (offline)")
        force_hf_offline()
    try:
        return SentenceTransformerEmbeddingFunction(model_name=model_name)
    except Exception as e:
        if not cached:
            raise RuntimeError(
                f"[Embedder] Модель {model_name} не в кэше HF и не скачалась "
                f"({e}) — нужен интернет хотя бы раз") from e
        logger.warning(f"[Embedder] Загрузка {model_name} не удалась ({e}) — "
                       "повтор offline по кэшу")
        force_hf_offline()
        return SentenceTransformerEmbeddingFunction(model_name=model_name)
