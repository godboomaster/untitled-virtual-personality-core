"""Превью последней реплики для страницы всех чатов (home_api._last_message):

  - читается из STM (Chroma) персоны напрямую, без бота: последняя по времени
    реплика ИМЕННО этого чата (чужие чаты той же персоны не подмешиваются);
  - role user → "user", остальное → "bot"; длинный текст обрезается, пробелы
    схлопываются; метка в мс переводится в секунды;
  - нет STM / нет реплик чата — None; GET /api/home отдаёт поле last_message.

Данные — во временной папке (VPC_DATA_DIR), настоящий data/ не трогается.
Запуск: python3 -m scripts.test_chat_overview
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def _stm(base: Path, persona: str, rows):
    """rows: (chat_id, role, text, ts_ms) — как пишет STM._save_to_db."""
    import chromadb
    client = chromadb.PersistentClient(path=str(base / f"api_{persona}" / "stm"))
    col = client.get_or_create_collection("short_term_memory")
    col.add(
        ids=[f"stm_{c}_{ts}" for c, _r, _t, ts in rows],
        documents=[t for _c, _r, t, _ts in rows],
        metadatas=[{"role": r, "timestamp": ts, "chat_id": c} for c, r, _t, ts in rows],
        embeddings=[[0.1, 0.2, 0.3] for _ in rows],
    )


def main():
    tmp = Path(tempfile.mkdtemp(prefix="chat_overview_"))
    os.environ["VPC_DATA_DIR"] = str(tmp)
    from app.api import home_api

    print("\n── превью последней реплики ──")
    long_text = "очень   длинный\nответ " * 40
    _stm(tmp, "p1", [
        ("web_user", "user", "привет", 1_700_000_000_000),
        ("web_user", "assistant", long_text, 1_700_000_005_000),
        ("tg_42", "user", "сообщение из другого чата", 1_700_000_009_000),
    ])
    lm = home_api._last_message("p1", "web_user")
    check("последняя реплика своего чата (не новее из чужого)",
          lm is not None and lm["role"] == "bot" and lm["text"].startswith("очень длинный ответ"))
    check("пробелы схлопнуты, текст обрезан с многоточием",
          lm is not None and "  " not in lm["text"] and "\n" not in lm["text"]
          and len(lm["text"]) <= home_api._PREVIEW_MAX and lm["text"].endswith("…"))
    check("метка в секундах", lm is not None and abs(lm["ts"] - 1_700_000_005) < 1e-6)

    _stm(tmp, "p1", [("web_user", "user", "а ты где?", 1_700_000_007_000)])
    lm = home_api._last_message("p1", "web_user")
    check("реплика пользователя → role user", lm == {"role": "user", "text": "а ты где?",
                                                     "ts": 1_700_000_007.0})
    check("чужой чат читается своим id",
          (home_api._last_message("p1", "tg_42") or {}).get("text") == "сообщение из другого чата")
    check("нет реплик чата — None", home_api._last_message("p1", "nobody") is None)
    check("нет STM у персоны — None", home_api._last_message("p2", "web_user") is None)

    print("\n── сводка /api/home ──")
    ov = home_api.home_overview({"p1": {}, "p2": {}}, chat_id="web_user")
    check("last_message в сводке персоны",
          (ov["personas"]["p1"].get("last_message") or {}).get("text") == "а ты где?")
    check("у персоны без переписки last_message = None",
          "last_message" in ov["personas"]["p2"] and ov["personas"]["p2"]["last_message"] is None)

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
