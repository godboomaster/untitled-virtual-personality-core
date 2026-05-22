import os
from dotenv import load_dotenv
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent
load_dotenv(_project_root / ".env.config")
load_dotenv(_project_root / ".env")

# ─── Провайдеры ──────────────────────────────────────────
# Все используют OpenAI-совместимый API.
# Если API_KEY не задан — провайдер пропускается.
# Порядок ключей в словаре = fallback-очередь (если ACTIVE_PROVIDER не указан).

PROVIDER_CONFIGS = {
    "zai": {
        "api_key": os.getenv("ZAI_API_KEY"),
        "base_url": os.getenv("ZAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
        "model": os.getenv("ZAI_MODEL", "glm-5-turbo"),
    },
    "openai": {
        "api_key": os.getenv("OPENAI_API_KEY"),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    },
    "anthropic": {
        "api_key": os.getenv("ANTHROPIC_API_KEY"),
        "base_url": os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1/"),
        "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
    },
    "groq": {
        "api_key": os.getenv("GROQ_API_KEY"),
        "base_url": os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
    },
    "deepseek": {
        "api_key": os.getenv("DEEPSEEK_API_KEY"),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
    },
    "kimi": {
        "api_key": os.getenv("KIMI_API_KEY"),
        "base_url": os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1"),
        "model": os.getenv("KIMI_MODEL", "moonshot-v1-8k"),
    },
    "google": {
        "api_key": os.getenv("GOOGLE_API_KEY"),
        "base_url": os.getenv("GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        "model": os.getenv("GOOGLE_MODEL", "gemini-2.0-flash"),
    },
    "mimo": {
        "api_key": os.getenv("MIMO_API_KEY"),
        "base_url": os.getenv("MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1"),
        "model": os.getenv("MIMO_MODEL", "mimo-v2.5-pro"),
    },
    "hf": {
        "api_key": os.getenv("HF_API_KEY"),
        "base_url": os.getenv("HF_BASE_URL", "https://router.huggingface.co/v1"),
        "model": os.getenv("HF_MODEL", "Qwen/Qwen2.5-7B-Instruct"),
    },
}


def get_available_providers() -> dict:
    """Возвращает только провайдеры с заданным API_KEY."""
    return {k: v for k, v in PROVIDER_CONFIGS.items() if v["api_key"]}


class Config:
    DATA_DIR = os.getenv("DATA_DIR", "./data")
    EMBEDDING_MODEL = "all-MiniLM-L6-v2"
    STM_SIZE = int(os.getenv("STM_SIZE", "50"))
    LTM_EXTRACTION_ENABLED = os.getenv("LTM_EXTRACTION_ENABLED", "true").lower() == "true"
    LTM_MODEL_PROVIDER = os.getenv("LTM_MODEL_PROVIDER", "hf")


def get_db_paths(context: str) -> dict:
    """
    Возвращает пути к базам данных для заданного контекста.

    Контексты:
        "connor"  -> data/connor/stm, ltm, files
        "arrodes" -> data/arrodes/stm, ltm, files
        "verso"   -> data/verso/stm, ltm, files
        "gradio"  -> data/gradio/stm, ltm, files
        "tg"      -> data/tg/stm, ltm, files (обратная совместимость)
    """
    base = os.path.join(Config.DATA_DIR, context)
    return {
        "stm": os.path.join(base, "stm"),
        "ltm": os.path.join(base, "ltm"),
        "files": os.path.join(base, "files"),
    }
