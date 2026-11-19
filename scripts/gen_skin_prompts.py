"""Тексты промпта генерации скина для приложения — web/src/skins/skinGenPrompts.ts.

Промпт для своей нейросети (раздел «Скины» → «Промпт для своей нейросети»,
web/src/skins/manualPrompt.ts) должен совпадать с тем, что генератор ядра
отправляет модели (app/api/skin_gen_api.py: SYSTEM_PROMPT,
DIRECTION_SYSTEM_PROMPT, FUNCTIONAL_CSS, строка языка, лимиты нормализации
направления). Тексты выгружаются отсюда, а сборка сообщений повторена в TS;
совпадение сборки проверяет scripts/test_skin_library.py. Правка промпта в
ядре — перегенерировать:

  python3 -m scripts.gen_skin_prompts           # записать
  python3 -m scripts.gen_skin_prompts --check   # сверить (1 — разошлось)
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("VPC_DATA_DIR", tempfile.mkdtemp(prefix="skin_prompts_"))
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")

OUT = ROOT / "web" / "src" / "skins" / "skinGenPrompts.ts"

HEAD = (
    "// Сгенерировано scripts/gen_skin_prompts.py из ядра (app/api/skin_gen_api.py) —\n"
    "// не править руками: правка промпта в ядре, потом перегенерировать\n"
)


def export() -> dict:
    from app.api import skin_gen_api as sg
    from app.core.language import user_language_line
    locales = ("ru", "en")
    return {
        "system": sg.SYSTEM_PROMPT,
        "directionSystem": sg.DIRECTION_SYSTEM_PROMPT,
        "functionalCss": sg.FUNCTIONAL_CSS,
        # «Language of visible fallback texts: …» + строка языка пользователя
        "langLine": {loc: sg._lang_line(loc) for loc in locales},
        "langName": {loc: sg._LANG_NAME[loc] for loc in locales},
        "userLanguageLine": {loc: user_language_line(loc) for loc in locales},
        "stylesRemoved": sg.STYLES_REMOVED,
        "longComment": sg.LONG_COMMENT,
        "maxDescription": sg.MAX_DESCRIPTION,
        "maxHtmlForLlm": sg.MAX_HTML_FOR_LLM,
        "directionMaxCount": sg.DIRECTION_MAX_COUNT,
        "directionMaxExclude": sg.DIRECTION_MAX_EXCLUDE,
        "directionMaxNote": sg.DIRECTION_MAX_NOTE,
        # Нормализация направления (check_direction)
        "textLimits": sg._TEXT_LIMITS,
        "maxPalette": sg._MAX_PALETTE,
        "maxAvoid": sg._MAX_AVOID,
        "maxStack": sg._MAX_STACK,
        "defaultMono": sg._DEFAULT_MONO,
        "roleAliases": sg._ROLE_ALIASES,
    }


def render() -> str:
    body = json.dumps(export(), ensure_ascii=False, indent=2)
    return f"{HEAD}export const SKIN_GEN_PROMPTS = {body};\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        same = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print(f"{OUT.name} {'совпадает с ядром' if same else 'РАЗОШЁЛСЯ с ядром — перегенерировать'}")
        return 0 if same else 1
    OUT.write_text(text, encoding="utf-8")
    print(f"записано {OUT.relative_to(ROOT)} ({len(text)} байт)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
