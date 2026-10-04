"""Тест чистой установки: персоны пользователя вне git и провайдеры без значений
по умолчанию.

  A. persona_dirs: папка пользователя (data/personas) первой, потом app/personas;
  B. правки встроенной персоны из веба (форма, сырой YAML, цвет) пишутся копией
     в папку пользователя, встроенный файл не меняется; builtin/customized;
  C. удаление: своя копия — сброс к встроенной, встроенную — 409, своя
     персона — удаляется; создание и копия — в папке пользователя; смена id
     встроенной — новый YAML у пользователя, встроенная остаётся;
     id «personas» занят;
  D. провайдеры: моделей по умолчанию нет; без ключей основного нет; основной
     по умолчанию — первый с ключом И моделью; роутер пропускает провайдера
     без модели, не делая запроса; Ollama без модели недоступна;
  E. в git из персон — только встроенная тестовая (connor), без блока llm.

Всё на временных папках (VPC_DATA_DIR, app/personas подменяется) — настоящие
data/ и app/personas не трогаются. LLM и сеть не зовутся.

Запуск: python -m scripts.test_user_personas
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    if cond:
        ok += 1
        print(f"  [OK] {name}")
    else:
        failures += 1
        print(f"  [FAIL] {name}")


def section(title):
    print(f"\n── {title} ──")


BUILTIN = """id: vpcut_builtin
name: Встроенная
description: тестовая встроенная персона
system_prompt: |
  Ты — тестовая персона.
settings:
  temperature: 0.7
features:
  todo: true
"""


def personas_case(tmp: Path):
    from app.api import runtime
    from app.api import settings_api as sa
    from app.core import addons

    core = tmp / "core_personas"
    core.mkdir()
    (core / "vpcut_builtin.yaml").write_text(BUILTIN, encoding="utf-8")
    (core / "vpcut_plain.yaml").write_text(
        BUILTIN.replace("vpcut_builtin", "vpcut_plain"), encoding="utf-8")
    builtin_bytes = (core / "vpcut_builtin.yaml").read_bytes()
    user = (tmp / "data" / "personas").resolve()

    with mock.patch.object(addons, "CORE_PERSONAS_DIR", core.resolve()), \
            mock.patch.object(sa, "CORE_PERSONAS_DIR", core.resolve()), \
            mock.patch.object(sa, "_data_roots", lambda: [(tmp / "data").resolve()]):
        section("A. Порядок папок персон")
        dirs = addons.persona_dirs()
        check("папка пользователя — data/personas под VPC_DATA_DIR",
              addons.user_personas_dir() == user)
        check("persona_dirs: сначала папка пользователя, потом app/personas",
              dirs[:2] == [user, core.resolve()])
        check("встроенная персона видна до первой правки",
              "vpcut_builtin" in runtime.list_personas())
        info = runtime.get_persona_info("vpcut_builtin")
        check("builtin=True, customized=False", info["builtin"] and not info["customized"])

        section("B. Правки встроенной — копией у пользователя")
        r = sa.update_persona_config("vpcut_builtin", {"temperature": 0.3}, None, None)
        check("update_persona_config: записано", r is not None)
        check("копия появилась в папке пользователя",
              (user / "vpcut_builtin.yaml").is_file())
        check("встроенный файл не изменился",
              (core / "vpcut_builtin.yaml").read_bytes() == builtin_bytes)
        cfg = sa.get_persona_config("vpcut_builtin")
        check("чтение — из копии (temperature 0.3)",
              (cfg or {}).get("settings", {}).get("temperature") == 0.3)
        info = runtime.get_persona_info("vpcut_builtin")
        check("builtin=True, customized=True", info["builtin"] and info["customized"])

        r = sa.set_persona_color("vpcut_plain", "#112233")
        check("цвет встроенной: копия у пользователя, встроенный не тронут",
              r.get("ok") and (user / "vpcut_plain.yaml").is_file()
              and "color" not in (core / "vpcut_plain.yaml").read_text(encoding="utf-8"))
        raw = BUILTIN.replace("Ты — тестовая персона.", "Ты — правленая персона.")
        r = sa.save_persona_yaml("vpcut_builtin", raw)
        check("сырой YAML: в копию",
              r and r.get("ok")
              and "правленая" in (user / "vpcut_builtin.yaml").read_text(encoding="utf-8")
              and (core / "vpcut_builtin.yaml").read_bytes() == builtin_bytes)

        # YAML вне папок персон (подменённый путь, как в тестах других модулей)
        # правится на месте и не утекает копией в папку пользователя
        other = tmp / "elsewhere" / "vpcut_other.yaml"
        other.parent.mkdir()
        other.write_text(BUILTIN.replace("vpcut_builtin", "vpcut_other"), encoding="utf-8")
        with mock.patch.object(sa, "_persona_yaml_path", lambda name: other):
            sa.update_persona_config("vpcut_other", {"temperature": 0.5}, None, None)
        check("YAML вне папок персон правится на месте, копии у пользователя нет",
              "0.5" in other.read_text(encoding="utf-8")
              and not (user / "vpcut_other.yaml").exists())

        section("C. Удаление, создание, копия, смена id")
        r = sa.delete_persona("vpcut_builtin")
        check("удаление своей копии — сброс к встроенной (reset)",
              r == {"ok": True, "reset": True}
              and not (user / "vpcut_builtin.yaml").exists()
              and "vpcut_builtin" in runtime.list_personas())
        r = sa.delete_persona("vpcut_builtin")
        check("встроенную без копии удалить нельзя — 409",
              not r["ok"] and r["status"] == 409
              and (core / "vpcut_builtin.yaml").read_bytes() == builtin_bytes)

        r = sa.create_persona(BUILTIN.replace("vpcut_builtin", "vpcut_mine"))
        check("создание — в папку пользователя",
              r.get("ok") and (user / "vpcut_mine.yaml").is_file()
              and not (core / "vpcut_mine.yaml").exists())
        r = sa.delete_persona("vpcut_mine")
        check("своя персона удаляется целиком",
              r == {"ok": True, "reset": False} and "vpcut_mine" not in runtime.list_personas())

        r = sa.duplicate_persona("vpcut_builtin")
        check("копия встроенной — в папке пользователя",
              r and r.get("ok") and (user / f"{r['persona']}.yaml").is_file()
              and not (core / f"{r['persona']}.yaml").exists())

        r = sa.rename_persona("vpcut_builtin", "vpcut_renamed")
        check("смена id встроенной: новый YAML у пользователя",
              r.get("ok") and (user / "vpcut_renamed.yaml").is_file())
        check("встроенная осталась под старым id, файл не тронут",
              (core / "vpcut_builtin.yaml").read_bytes() == builtin_bytes
              and "vpcut_builtin" in runtime.list_personas())

        r = sa.create_persona(BUILTIN.replace("vpcut_builtin", "personas"))
        check("id «personas» занят служебной папкой данных", not r.get("ok"))


def providers_case():
    section("D. Провайдеры без значений по умолчанию")
    from app.core import config
    from app.core import router as router_mod

    # По исходнику: в живом процессе модели могли прийти из .env пользователя
    import re
    src = Path(config.__file__).read_text(encoding="utf-8")
    defaults = re.findall(r'os\.getenv\("([A-Z]+_MODEL)", "([^"]*)"\)', src)
    check(f"моделей по умолчанию в коде нет ({len(defaults)}: провайдеры и Ollama)",
          len(defaults) >= 10 and all(not v for _, v in defaults))

    def cfg(model, keys=("k",)):
        return {"api_keys": list(keys), "base_url": "http://127.0.0.1:9", "model": model}

    check("основной по умолчанию — первый с ключом и моделью",
          config.first_ready_provider({"zai": cfg(""), "groq": cfg("m")}) == "groq")
    check("нет провайдера с моделью — первый с ключом",
          config.first_ready_provider({"zai": cfg(""), "groq": cfg("")}) == "zai")
    check("нет ключей — основного нет", config.first_ready_provider({}) is None)

    from app.api import settings_api as sa
    empty = {p: dict(c, api_keys=[], model="") for p, c in config.PROVIDER_CONFIGS.items()}
    with mock.patch.dict(sa.PROVIDER_CONFIGS, empty), \
            mock.patch.dict(os.environ, {"ACTIVE_PROVIDER": ""}):
        lst = sa.list_providers()
        cloud = [p for p in lst["providers"] if not p["local"]]
        check("чистая установка: ни один ключ не задан",
              cloud and not any(p["key_set"] for p in cloud))
        check("чистая установка: основного провайдера нет", lst["active"] is None)
        check("чистая установка: модели не заданы", not any(p["model"] for p in cloud))

    # Роутер: провайдер без модели пропускается без запроса
    r = router_mod.ModelRouter.__new__(router_mod.ModelRouter)
    r.model_overrides = {}
    called = []
    with mock.patch.object(router_mod, "OpenAI", lambda *a, **k: called.append(1)):
        out = router_mod.ModelRouter._call_with_keys(
            r, "zai", cfg(""), [{"role": "user", "content": "hi"}], 0.7, 10, 0.9, 5.0)
    check("роутер: без модели — None и ни одного запроса", out is None and not called)

    from app.core.local_router import LocalLLMRouter
    lr = LocalLLMRouter.__new__(LocalLLMRouter)
    lr.model = ""
    lr._client = None  # до сети дойти не должно
    check("Ollama без модели недоступна (без запроса)", lr._check_available() is False)


def git_case():
    section("E. В git — только встроенная тестовая персона")
    import yaml
    r = subprocess.run(["git", "ls-files", "app/personas"], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode == 0:
        yamls = [p for p in r.stdout.split() if p.endswith(".yaml")]
        where = "в git"
    else:  # копия без .git — смотрим на сами файлы
        yamls = sorted(f"app/personas/{p.name}" for p in (ROOT / "app/personas").glob("*.yaml"))
        where = "в папке (не git-клон)"
    check(f"app/personas {where}: только connor.yaml ({yamls})",
          yamls == ["app/personas/connor.yaml"])
    data = yaml.safe_load((ROOT / "app/personas/connor.yaml").read_text(encoding="utf-8"))
    check("тестовый Коннор без блока llm (провайдеры, запасные, модели)", "llm" not in data)
    check("тестовый Коннор: подтверждение действий включено",
          data["features"]["computer_control"].get("confirm") is True)
    env_config = (ROOT / ".env.config").read_text(encoding="utf-8")
    check(".env.config: ни провайдера, ни моделей",
          not any(line.split("=")[0].endswith(("_MODEL", "ACTIVE_PROVIDER"))
                  for line in env_config.splitlines() if "=" in line and not line.startswith("#")))
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    filled = [line.split("=")[0] for line in example.splitlines()
              if "=" in line and not line.startswith("#")
              and line.split("=")[0].endswith(("_API_KEY", "_MODEL", "_TOKEN", "_USER_ID",
                                                "ACTIVE_PROVIDER", "WEBCHAT_SITES"))
              and line.split("=", 1)[1].split("#")[0].strip()]
    check(f".env.example: ключи, модели, токены и ID пустые {filled or ''}", not filled)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="user_personas_"))
    os.environ["VPC_DATA_DIR"] = str(tmp / "data")
    os.environ.setdefault("OLLAMA_URL", "http://127.0.0.1:9")
    personas_case(tmp)
    providers_case()
    git_case()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
