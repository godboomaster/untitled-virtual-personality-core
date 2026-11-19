"""Базовый кит телефона без компьютера — web/src/offline/baseKit.ts, и
встроенные персоны — web/src/offline/bundledPersonas.ts.

Приложение, скачанное без ядра, отвечает по нему: правила, заметки
напоминаний, гейт поиска, правило вопросов и погодные коды — те же, что ядро
кладёт в каждый кит (app/api/offline_api.build_base_kit), но с правилами
для телефона без компьютера. Встроенные персоны — YAML из app/personas как
есть: на ядре они те же, и переписка с ними при переносе ложится туда.
Правка в ядре — перегенерировать:

  python3 -m scripts.gen_offline_base_kit           # записать
  python3 -m scripts.gen_offline_base_kit --check   # сверить (1 — разошлось)
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("VPC_DATA_DIR", tempfile.mkdtemp(prefix="base_kit_"))
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")

OUT = ROOT / "web" / "src" / "offline" / "baseKit.ts"
OUT_PERSONAS = ROOT / "web" / "src" / "offline" / "bundledPersonas.ts"
BUNDLED = ["connor"]  # app/personas/<id>.yaml

HEAD = (
    "// Сгенерировано scripts/gen_offline_base_kit.py из ядра (offline_api.build_base_kit,\n"
    "// app/personas) — не править руками: правка в ядре, потом перегенерировать\n"
)


def render() -> str:
    from app.api.offline_api import build_base_kit
    body = json.dumps(build_base_kit(), ensure_ascii=False, indent=2, sort_keys=True)
    return f"{HEAD}export const BASE_KIT = {body};\n"


def render_personas() -> str:
    items = {pid: (ROOT / "app" / "personas" / f"{pid}.yaml").read_text(encoding="utf-8") for pid in BUNDLED}
    body = json.dumps(items, ensure_ascii=False, indent=2)
    return f"{HEAD}export const BUNDLED_PERSONAS: Record<string, string> = {body};\n"


def main() -> int:
    outs = [(OUT, render()), (OUT_PERSONAS, render_personas())]
    if "--check" in sys.argv:
        bad = 0
        for path, text in outs:
            same = path.exists() and path.read_text(encoding="utf-8") == text
            bad += not same
            print(f"{path.name} {'совпадает с ядром' if same else 'РАЗОШЁЛСЯ с ядром — перегенерировать'}")
        return 1 if bad else 0
    for path, text in outs:
        path.write_text(text, encoding="utf-8")
        print(f"записано {path.relative_to(ROOT)} ({len(text)} байт)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
