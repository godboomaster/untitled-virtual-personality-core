import os
from pathlib import Path

from app.core.envfile import load_env_file

_project_root = Path(__file__).parent.parent.parent
# .env грузим первым: уже заданные переменные не перезаписываются,
# поэтому пользовательские значения из .env имеют приоритет над дефолтами .env.config.
# load_env_file, а не load_dotenv: «KEY=   # комментарий» — пустое значение
load_env_file(_project_root / ".env")
load_env_file(_project_root / ".env.config")

# ─── Локальная модель (Ollama) ────────────────────────────
# Единая модель для ВСЕХ локальных вызовов: чат-фолбэк (local_router),
# перевод/классификация/дистилляция/кореференция (book_search,
# intent_router). Меняется в одном месте — OLLAMA_MODEL в .env / .env.config
# или через настройки веба. Reasoning-модели (gemma4) требуют "think": False
# в запросах — это уже учтено во всех вызывающих сторонах. По умолчанию не
# задана — без неё Ollama считается недоступной.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "")

# ─── Провайдеры ──────────────────────────────────────────
# Все используют OpenAI-совместимый API. Модели по умолчанию нет: её выбирает
# пользователь («Настройки» в вебе или <ПРОВАЙДЕР>_MODEL в .env); провайдер
# без модели роутер пропускает.
# Если API_KEY не задан — провайдер пропускается.
# Порядок ключей в словаре = fallback-очередь (если ACTIVE_PROVIDER не указан).

def _collect_api_keys(prefix: str) -> list[str]:
    """
    Собирает все API-ключи для провайдера.

    Форматы в .env:
        GROQ_API_KEY=sk-aaa          # основной (или GROQ_API_KEY_1)
        GROQ_API_KEY_2=sk-bbb        # дополнительный
        GROQ_API_KEY_3=sk-ccc
    """
    keys = []
    # GROQ_API_KEY (без суффикса) — основной ключ
    main = os.getenv(f"{prefix}_API_KEY")
    if main:
        keys.append(main)
    # GROQ_API_KEY_1, GROQ_API_KEY_2, ... — ищем все подряд номера
    i = 1
    empty_count = 0
    while empty_count < 5:  # допускаем до 5 пропусков
        k = os.getenv(f"{prefix}_API_KEY_{i}")
        if k and k not in keys:
            keys.append(k)
            empty_count = 0
        else:
            empty_count += 1
        i += 1
    return keys


PROVIDER_CONFIGS = {
    "zai": {
        "api_keys": _collect_api_keys("ZAI"),
        "base_url": os.getenv("ZAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
        "model": os.getenv("ZAI_MODEL", ""),
        "vision": os.getenv("ZAI_VISION", "auto"),
    },
    "openai": {
        "api_keys": _collect_api_keys("OPENAI"),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "model": os.getenv("OPENAI_MODEL", ""),
        "vision": os.getenv("OPENAI_VISION", "auto"),
    },
    "anthropic": {
        "api_keys": _collect_api_keys("ANTHROPIC"),
        "base_url": os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1/"),
        "model": os.getenv("ANTHROPIC_MODEL", ""),
        "vision": os.getenv("ANTHROPIC_VISION", "auto"),
    },
    "groq": {
        "api_keys": _collect_api_keys("GROQ"),
        "base_url": os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        "model": os.getenv("GROQ_MODEL", ""),
        "vision": os.getenv("GROQ_VISION", "auto"),
    },
    "deepseek": {
        "api_keys": _collect_api_keys("DEEPSEEK"),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model": os.getenv("DEEPSEEK_MODEL", ""),
        "vision": os.getenv("DEEPSEEK_VISION", "auto"),
    },
    "kimi": {
        "api_keys": _collect_api_keys("KIMI"),
        "base_url": os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1"),
        "model": os.getenv("KIMI_MODEL", ""),
        "vision": os.getenv("KIMI_VISION", "auto"),
        # Провайдер Kimi принимает только temperature=1 и top_p=0.95 — иначе
        # 400 «invalid temperature/top_p: only ... is allowed for this model».
        # Фиксируем на уровне провайдера, игнорируя значения вызывающего кода;
        # переопределяются через KIMI_TEMPERATURE / KIMI_TOP_P в .env.config.
        "temperature": float(os.getenv("KIMI_TEMPERATURE", "1.0")),
        "top_p": float(os.getenv("KIMI_TOP_P", "0.95")),
        # Лимит аккаунта: 1 параллельный запрос — иначе 403 «concurrent
        # request limit» (фон и диалог могут дёрнуть его одновременно).
        # Роутер пропускает занятость мгновенно, уходя по цепочке дальше.
        "max_concurrent": 1,
    },
    "google": {
        "api_keys": _collect_api_keys("GOOGLE"),
        "base_url": os.getenv("GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        "model": os.getenv("GOOGLE_MODEL", ""),
        "vision": os.getenv("GOOGLE_VISION", "auto"),
    },
    "mimo": {
        "api_keys": _collect_api_keys("MIMO"),
        "base_url": os.getenv("MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1"),
        "model": os.getenv("MIMO_MODEL", ""),
        "vision": os.getenv("MIMO_VISION", "auto"),
    },
    "hf": {
        "api_keys": _collect_api_keys("HF"),
        "base_url": os.getenv("HF_BASE_URL", "https://router.huggingface.co/v1"),
        "model": os.getenv("HF_MODEL", ""),
        "vision": os.getenv("HF_VISION", "auto"),
    },
}

# Режимы флага vision: "auto" (по умолчанию — роутер сам пробует модель крошечной
# тестовой картинкой при первом изображении и кеширует вердикт), "true"/"false"
# (явное ручное переопределение, проба не делается).


def get_available_providers() -> dict:
    # Возвращает только провайдеры хотя бы с одним API-ключом.
    return {k: v for k, v in PROVIDER_CONFIGS.items() if v["api_keys"]}


def first_ready_provider(available: dict) -> str | None:
    # Основной по умолчанию (ACTIVE_PROVIDER не задан или без ключа): первый с
    # ключом и моделью — провайдер без модели не ответит; нет таких — первый с ключом
    ready = [p for p, cfg in available.items() if cfg.get("model")]
    return (ready or list(available) or [None])[0]


class Config:
    DATA_DIR = os.getenv("DATA_DIR") or "./data"
    EMBEDDING_MODEL = "all-MiniLM-L6-v2"
    STM_SIZE = int(os.getenv("STM_SIZE", "500"))
    LTM_EXTRACTION_ENABLED = os.getenv("LTM_EXTRACTION_ENABLED", "true").lower() == "true"
    # Провайдер для побочных LLM-задач LTM (экстракция фактов и т.п.).
    # Пусто (дефолт) — основной роутер бота по fallback-цепочке персоны
    # МИНУС основной провайдер (exclude_provider); задан — отдельный роутер
    # с этим провайдером основным.
    LTM_MODEL_PROVIDER = os.getenv("LTM_MODEL_PROVIDER", "")


def get_db_paths(context: str) -> dict:
    """
    Возвращает пути к базам данных для заданного контекста.

    Контекст — обычно id персоны (data/<persona>/stm, ltm, files) или
    "api_{persona}" для веб/API-режима; "tg" — общий контекст Telegram
    (обратная совместимость).
    """
    base = os.path.join(Config.DATA_DIR, context)
    return {
        "stm": os.path.join(base, "stm"),
        "ltm": os.path.join(base, "ltm"),
        "files": os.path.join(base, "files"),
    }
