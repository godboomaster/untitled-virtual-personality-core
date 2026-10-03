"""Окно часов самоинициативы из веба (PUT /api/personas/<id>/initiative):
поле initiative_hours раньше отбрасывалось схемой InitiativeUpdate, а
снять окно (null) было нельзя — обработчик выкидывал все None.

Проверяется на временном YAML персоны (рабочие app/personas/*.yaml не
трогаются): окно сохраняется, null снимает его, неприсланное поле не
меняется, битое значение — 400, остальные поля — как раньше.

Запуск: python3 -m scripts.test_initiative_hours
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")
for _k in ("API_CORS_ORIGINS", "API_HOST", "API_ALLOWED_HOSTS", "API_TOKEN"):
    os.environ.pop(_k, None)

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def main():
    from fastapi.testclient import TestClient
    import app.api.server as server_mod
    from app.api import settings_api

    tmp = Path(tempfile.mkdtemp(prefix="init_hours_"))
    yml = tmp / "probe.yaml"
    yml.write_text(yaml.safe_dump({
        "name": "Probe", "system_prompt": "test",
        "features": {"proactive": {"enabled": False, "silence_threshold_minutes": 180}},
    }, allow_unicode=True), encoding="utf-8")
    orig_path = settings_api._persona_yaml_path
    settings_api._persona_yaml_path = lambda persona: yml if persona == "probe" else None
    try:
        c = TestClient(server_mod.app, base_url="http://127.0.0.1")
        url = "/api/personas/probe/initiative"

        def hours():
            data = yaml.safe_load(yml.read_text(encoding="utf-8"))
            return data["features"]["proactive"].get("initiative_hours", "<нет>")

        r = c.put(url, json={"initiative_hours": "09:00-22:00"})
        check("окно сохраняется: 200", r.status_code == 200)
        check("окно в YAML", hours() == "09:00-22:00")

        r = c.put(url, json={"initiative_probability": 0.5})
        check("правка другого поля окно не трогает", r.status_code == 200 and hours() == "09:00-22:00")

        r = c.put(url, json={"initiative_hours": "9:00-25:00"})
        check("битое окно — 400", r.status_code == 400)
        check("битое окно не записано", hours() == "09:00-22:00")

        r = c.put(url, json={"initiative_hours": None})
        check("null снимает окно: 200", r.status_code == 200)
        check("окна нет (круглые сутки)", hours() is None)

        r = c.put(url, json={"silence_threshold_minutes": None})
        check("null у обычного поля — пустой патч, как раньше (400)", r.status_code == 400)

        proactive = yaml.safe_load(yml.read_text(encoding="utf-8"))["features"]["proactive"]
        check("остальные поля целы", proactive.get("silence_threshold_minutes") == 180
              and proactive.get("initiative_probability") == 0.5)
    finally:
        settings_api._persona_yaml_path = orig_path
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
