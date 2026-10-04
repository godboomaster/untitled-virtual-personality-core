"""Жизненный цикл персоны: правка YAML на живую, создание и смена id поверх
оставшейся памяти.

  A. save_persona_yaml (редактор YAML в вебе) применяет к живому боту то же,
     что форма настроек: computer_control целиком (allowed_users, confirm,
     выключение — режим управления больше не допускается), proactive,
     rhythm, life, system_prompt, заморозка; restart_required — только для
     того, что действует после перезапуска;
  B. create_persona поверх памяти, оставшейся под id (удалённая персона):
     без выбора — конфликт memory_exists, keep — подхватить, fresh — старая
     память в архив рядом (ничего не удаляется), бот и очередь фоновых
     сообщений прежней персоны сняты; копия персоны такой id пропускает;
     общие папки данных (skins, tg) — не память персоны;
  C. rename_persona в id с оставшейся памятью — тот же выбор (keep — только
     если своя память не ляжет поверх);
  D. эндпоинт POST /api/personas: 409 с memory_exists, memory=fresh, 422.

Всё на временных папках (VPC_DATA_DIR, _PERSONAS_DIR, _data_roots) —
настоящие data/ и app/personas не трогаются. LLM/браузер не зовутся.

Запуск: python -m scripts.test_persona_lifecycle
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


class _Loop:
    """Заготовка фонового менеджера (proactive/rhythm/living): старт/стоп/конфиг."""

    def __init__(self, **cfg):
        self._running = True
        self.config = SimpleNamespace(**cfg)
        self.updates = []
        self.living = None
        self._ignore_streak = {}
        self.ruined = []

    def start(self, *a, **kw):
        self._running = True

    def stop(self):
        self._running = False

    def update_config(self, cfg):
        self.updates.append(cfg)

    def ruin_mood(self, chat_id):
        self.ruined.append(chat_id)


LIVE_YAML = """id: lc_live
name: Live
system_prompt: ping
features:
  computer_control:
    enabled: true
    confirm: true
    allowed_users: ["111", "222"]
  scenarios: false
  task_agent: false
  proactive:
    enabled: true
    silence_threshold_minutes: 30
  rhythm:
    enabled: true
  life: true
"""


def _with_cc(cc_lines: str, base: str = LIVE_YAML) -> str:
    old = ("  computer_control:\n    enabled: true\n    confirm: true\n"
           "    allowed_users: [\"111\", \"222\"]\n")
    assert old in base
    return base.replace(old, cc_lines)


def test_yaml_live(tmp: Path):
    section("A. save_persona_yaml — живой бот как после формы настроек")
    import yaml as _yaml
    from app.api import settings_api as sa
    from app.api.runtime import registry
    from app.bot_instance import BotInstance
    from app.features import flavor_text
    from app.features.computer_control import ComputerControlManager

    path = sa._PERSONAS_DIR / "lc_live.yaml"
    path.write_text(LIVE_YAML, encoding="utf-8")
    data = _yaml.safe_load(LIVE_YAML)
    feats = data["features"]

    bot = BotInstance.__new__(BotInstance)
    bot.persona_name = "lc_live"
    bot.context = "api_lc_live"
    bot.owner = "999"
    bot.web_single_user = False  # проверяем сам allowlist, а не «веб = владелец»
    bot.features = dict(feats)
    bot.persona = SimpleNamespace(persona_data=data, system_prompt="ping", settings={})
    bot.stm_size = 100
    bot.router = SimpleNamespace(set_persona_llm=lambda *a, **k: None)
    bot.intellect = SimpleNamespace(tier=None)
    bot.computer_control = ComputerControlManager(
        context=bot.context, config=feats["computer_control"], base_dir=tmp / "cc")
    bot._cc_allowed_users = {"111", "222"}
    bot._control_mode = {"chatA"}
    bot._control_mode_ts = {}
    bot.scenario_manager = None
    bot.task_agent = None
    bot.reminder_manager = bot.todo_manager = bot.inventory_manager = None
    bot.learning_manager = None
    bot.proactive = _Loop(enabled=True, silence_threshold_minutes=30)
    bot.rhythm = _Loop()
    bot.living = _Loop()
    registry._bots["lc_live"] = bot
    cc_manager = bot.computer_control

    def save(text):
        return sa.save_persona_yaml("lc_live", text)

    try:
        with mock.patch.object(flavor_text, "ensure_flavor_bank", lambda *a, **k: None):
            # 1. allowed_users и confirm — сразу, без рестарта
            y1 = _with_cc("  computer_control:\n    enabled: true\n    confirm: false\n"
                          "    allowed_users: [\"111\"]\n")
            y1 = y1.replace("silence_threshold_minutes: 30", "silence_threshold_minutes: 45")
            r = save(y1)
            check("allowed_users: убранный человек сразу без доступа",
                  bot._cc_allowed_users == {"111"} and bot._cc_allowed("222") is False)
            check("allowed_users: оставшийся по-прежнему допущен", bot._cc_allowed("111") is True)
            check("confirm применён к живому менеджеру (тот же объект)",
                  bot.computer_control is cc_manager and cc_manager.confirm is False)
            check("proactive: порог молчания применён к живому конфигу",
                  bot.proactive.config.silence_threshold_minutes == 45)
            check("restart_required=false (всё применено на живую)",
                  r["ok"] and r["restart_required"] is False)

            # 2. computer_control выключен — режим управления больше не допускается
            y2 = _with_cc("  computer_control:\n    enabled: false\n"
                          "    allowed_users: [\"111\"]\n").replace(
                "silence_threshold_minutes: 30", "silence_threshold_minutes: 45")
            r = save(y2)
            check("выключение computer_control: менеджер снят", bot.computer_control is None)
            check("режим управления снят во всех чатах", not bot._control_mode)
            reply = bot._control_mode_switch("chatA", True)
            check("включить режим управления заново нельзя",
                  "chatA" not in bot._control_mode and not bot.control_mode_on("chatA")
                  and isinstance(reply, str))
            check("выключение — без рестарта", r["restart_required"] is False)

            # 3. proactive / rhythm / life выключены — циклы остановлены
            y3 = (y2.replace("  proactive:\n    enabled: true", "  proactive:\n    enabled: false")
                    .replace("  rhythm:\n    enabled: true", "  rhythm:\n    enabled: false")
                    .replace("  life: true", "  life: false"))
            living = bot.living
            r = save(y3)
            check("proactive: enabled=false — цикл остановлен",
                  bot.proactive.config.enabled is False and bot.proactive._running is False)
            check("rhythm: конфиг передан, цикл остановлен",
                  bot.rhythm.updates and bot.rhythm.updates[-1].get("enabled") is False
                  and bot.rhythm._running is False)
            check("life: жизнь остановлена и снята",
                  living._running is False and bot.living is None)
            check("proactive/rhythm/life — без рестарта", r["restart_required"] is False)

            # 4. system_prompt на живую; intellect — только после перезапуска
            r = save(y3.replace("system_prompt: ping", "system_prompt: pong"))
            check("system_prompt применён к живой персоне",
                  bot.persona.system_prompt == "pong" and r["restart_required"] is False)
            y4 = y3.replace("system_prompt: ping", "system_prompt: pong")
            r = save(y4 + "intellect:\n  tier: normal\n")
            check("intellect (читается при создании бота) — restart_required",
                  r["restart_required"] is True)

            # 5. Заморозка через YAML рушит настроение, как тумблер в форме — один раз
            y5 = y4 + "intellect:\n  tier: normal\n"
            y5 = y5.replace("  life: false\n", "  life: false\n  muted: true\n")
            save(y5)
            first = list(bot.proactive.ruined)
            save(y5.replace("system_prompt: pong", "system_prompt: pong2"))
            check("свежая заморозка через YAML — настроение испорчено",
                  "web_user" in first)
            check("повторное сохранение замороженной — без повторного удара",
                  bot.proactive.ruined == first)

            # 6. Прочие фичи по-прежнему требуют рестарта
            r = save(y5.replace("  life: false\n", "  life: false\n  web_search: true\n"))
            check("web_search — restart_required", r["restart_required"] is True)

            # 7. Файл на диске — ровно то, что сохранили
            check("YAML записан как есть",
                  path.read_text(encoding="utf-8")
                  == y5.replace("  life: false\n", "  life: false\n  web_search: true\n"))
    finally:
        registry._bots.pop("lc_live", None)


def _mk(path: Path, marker: str):
    path.mkdir(parents=True, exist_ok=True)
    (path / "marker.txt").write_text(marker, encoding="utf-8")


def _archives(root: Path, name: str) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.name.startswith(f"{name}.archived-"))


def test_create_over_leftover(roots: list[Path]):
    section("B. create_persona поверх оставшейся памяти")
    from app.api import settings_api as sa
    from app.api.inbox import inbox_pop, inbox_push
    from app.api.runtime import registry

    r1, r2 = roots
    pd = sa._PERSONAS_DIR

    def yaml_for(pid):
        return f"id: {pid}\nname: {pid}\nsystem_prompt: hi\n"

    r = sa.create_persona(yaml_for("fresh_one"))
    check("нет памяти под id — создаётся сразу",
          r["ok"] and r["persona"] == "fresh_one" and r["archived"] == [])

    # Память удалённой персоны: веб-папка в одном корне, Telegram — в другом
    _mk(r1 / "api_ghost" / "stm", "old-web")
    _mk(r2 / "ghost", "old-tg")
    r = sa.create_persona(yaml_for("ghost"))
    check("без выбора — конфликт memory_exists, YAML не создан",
          r["ok"] is False and r.get("memory_exists") is True and r.get("conflict") is True
          and r.get("persona") == "ghost" and not (pd / "ghost.yaml").exists())
    check("detail называет оставшиеся папки", "api_ghost" in r["detail"])
    r = sa.create_persona(yaml_for("ghost"), memory="bogus")
    check("неизвестный выбор — как без выбора (конфликт)",
          r["ok"] is False and r.get("memory_exists") is True)

    r = sa.create_persona(yaml_for("ghost"), memory="keep")
    check("keep: создана, память на месте (подхвачена)",
          r["ok"] and (pd / "ghost.yaml").is_file()
          and (r1 / "api_ghost" / "stm" / "marker.txt").read_text(encoding="utf-8") == "old-web"
          and (r2 / "ghost" / "marker.txt").is_file() and r["archived"] == [])

    # Удаление персоны: YAML уходит, память остаётся. Живой бот и очередь
    # фоновых сообщений прежней персоны ещё в процессе
    (pd / "ghost.yaml").unlink()
    stopped = []
    stale = SimpleNamespace(proactive=None, reminder_manager=None, learning_manager=None,
                            living=SimpleNamespace(stop=lambda: stopped.append(1)), rhythm=None)
    registry._bots["ghost"] = stale
    inbox_push("ghost", "web_user", "напоминание прежней персоны")

    r = sa.create_persona(yaml_for("ghost"))
    check("после удаления — снова конфликт (молча не подхватывается)",
          r["ok"] is False and r.get("memory_exists") is True)

    r = sa.create_persona(yaml_for("ghost"), memory="fresh")
    a1, a2 = _archives(r1, "api_ghost"), _archives(r2, "ghost")
    check("fresh: создана, под id чисто",
          r["ok"] and (pd / "ghost.yaml").is_file()
          and not (r1 / "api_ghost").exists() and not (r2 / "ghost").exists())
    check("fresh: старая память в архиве рядом, ничего не удалено",
          len(a1) == 1 and len(a2) == 1
          and (a1[0] / "stm" / "marker.txt").read_text(encoding="utf-8") == "old-web"
          and (a2[0] / "marker.txt").read_text(encoding="utf-8") == "old-tg")
    check("fresh: архивные пути в ответе", sorted(r["archived"]) == sorted(map(str, a1 + a2)))
    check("fresh: имя архива — <папка>.archived-ГГГГММДД-ЧЧММСС",
          len(a1[0].name.split(".archived-")[1]) == len("20261004-120000"))
    check("fresh: бот прежней персоны выгружен и остановлен",
          "ghost" not in registry._bots and stopped == [1])
    check("fresh: её недоставленные фоновые сообщения сняты",
          inbox_pop("ghost", "web_user") == [])

    # Повторный архив того же id в ту же секунду — не затирает первый
    (pd / "ghost.yaml").unlink()
    _mk(r1 / "api_ghost", "second")
    with mock.patch("app.core.timeutil.now") as now:
        stamp = a1[0].name.split(".archived-")[1]
        from datetime import datetime
        now.return_value = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
        r = sa.create_persona(yaml_for("ghost"), memory="fresh")
    a1b = _archives(r1, "api_ghost")
    check("второй архив в ту же секунду — отдельная папка",
          r["ok"] and len(a1b) == 2
          and (a1[0] / "stm" / "marker.txt").read_text(encoding="utf-8") == "old-web")

    # Копия персоны: id с оставшейся памятью пропускается (не подхватывает её)
    (pd / "dup_src.yaml").write_text(yaml_for("dup_src"), encoding="utf-8")
    _mk(r1 / "api_dup_src_copy", "old-copy")
    r = sa.duplicate_persona("dup_src")
    check("копия: id с оставшейся памятью пропущен",
          r["ok"] and r["persona"] == "dup_src_copy2"
          and (r1 / "api_dup_src_copy" / "marker.txt").is_file())

    # Общие папки корня данных — не память персоны
    _mk(r1 / "skins", "skins-lib")
    r = sa.create_persona(yaml_for("skins"))
    check("id = общая папка (skins): не конфликт, папка не тронута",
          r["ok"] and (r1 / "skins" / "marker.txt").read_text(encoding="utf-8") == "skins-lib")


def test_rename_over_leftover(roots: list[Path]):
    section("C. rename_persona в id с оставшейся памятью")
    from app.api import settings_api as sa

    r1, _r2 = roots
    pd = sa._PERSONAS_DIR
    calendar = SimpleNamespace(list_entries=lambda: [], update_entry=lambda *a, **k: None)

    with mock.patch("app.features.calendar_manager.get_calendar", lambda: calendar):
        # У персоны своя память, под новым id — чужая: подхватить нельзя
        (pd / "ren_src.yaml").write_text("id: ren_src\nname: S\nsystem_prompt: hi\n", encoding="utf-8")
        _mk(r1 / "api_ren_src", "own")
        _mk(r1 / "api_ren_dst", "old")
        r = sa.rename_persona("ren_src", "ren_dst")
        check("без выбора — 409 memory_exists, can_keep=false (своя память ляжет поверх)",
              r["ok"] is False and r["status"] == 409 and r.get("memory_exists") is True
              and r.get("can_keep") is False and r.get("persona") == "ren_dst")
        check("ничего не перенесено",
              (pd / "ren_src.yaml").is_file()
              and (r1 / "api_ren_src" / "marker.txt").read_text(encoding="utf-8") == "own"
              and (r1 / "api_ren_dst" / "marker.txt").read_text(encoding="utf-8") == "old")
        r = sa.rename_persona("ren_src", "ren_dst", memory="keep")
        check("keep при пересечении — отказ 409", r["ok"] is False and r["status"] == 409)

        r = sa.rename_persona("ren_src", "ren_dst", memory="fresh")
        arch = _archives(r1, "api_ren_dst")
        check("fresh: id сменён, своя память переехала",
              r["ok"] and (pd / "ren_dst.yaml").is_file() and not (pd / "ren_src.yaml").exists()
              and (r1 / "api_ren_dst" / "marker.txt").read_text(encoding="utf-8") == "own")
        check("fresh: чужая память — в архиве, не удалена",
              len(arch) == 1 and (arch[0] / "marker.txt").read_text(encoding="utf-8") == "old"
              and r["archived"] == [str(arch[0])])

        # Своей памяти нет — подхватить оставшуюся можно
        (pd / "ren_b.yaml").write_text("id: ren_b\nname: B\nsystem_prompt: hi\n", encoding="utf-8")
        _mk(r1 / "api_ren_c", "left")
        r = sa.rename_persona("ren_b", "ren_c")
        check("своей памяти нет — 409 с can_keep=true",
              r["ok"] is False and r.get("memory_exists") is True and r.get("can_keep") is True)
        r = sa.rename_persona("ren_b", "ren_c", memory="keep")
        check("keep: id сменён, оставшаяся память подхвачена",
              r["ok"] and (pd / "ren_c.yaml").is_file()
              and (r1 / "api_ren_c" / "marker.txt").read_text(encoding="utf-8") == "left"
              and r["archived"] == [] and not _archives(r1, "api_ren_c"))

        # Сбой переноса после архива — архив возвращается на место
        (pd / "ren_x.yaml").write_text("id: ren_x\nname: X\nsystem_prompt: hi\n", encoding="utf-8")
        _mk(r1 / "api_ren_x", "own-x")
        _mk(r1 / "api_ren_y", "old-y")
        # (logger.exception заглушён: ожидаемый сбой не печатает трейсбек)
        with mock.patch.object(sa, "atomic_write_text", side_effect=OSError("disk full")), \
                mock.patch.object(sa.logger, "exception"):
            r = sa.rename_persona("ren_x", "ren_y", memory="fresh")
        check("сбой: 500, всё как было (своя память и чужая — на местах, архива нет)",
              r["ok"] is False and r["status"] == 500
              and (r1 / "api_ren_x" / "marker.txt").read_text(encoding="utf-8") == "own-x"
              and (r1 / "api_ren_y" / "marker.txt").read_text(encoding="utf-8") == "old-y"
              and not _archives(r1, "api_ren_y") and (pd / "ren_x.yaml").is_file())


def test_endpoint(roots: list[Path]):
    section("D. POST /api/personas — 409 memory_exists и выбор")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod

    r1, _r2 = roots
    _mk(r1 / "api_ep_ghost", "old")
    body = "id: ep_ghost\nname: E\nsystem_prompt: hi\n"
    orig_token = server_mod._api_token
    server_mod._api_token = ""
    try:
        client = TestClient(server_mod.app, base_url="http://127.0.0.1")
        r = client.post("/api/personas", json={"yaml": body})
        j = r.json()
        check("без выбора — 409 с detail-строкой и memory_exists",
              r.status_code == 409 and isinstance(j.get("detail"), str)
              and j.get("memory_exists") is True and j.get("persona") == "ep_ghost")
        r = client.post("/api/personas", json={"yaml": body, "memory": "bogus"})
        check("неизвестный memory — 422", r.status_code == 422)
        r = client.post("/api/personas", json={"yaml": body, "memory": "fresh"})
        check("memory=fresh — 200, архив в ответе",
              r.status_code == 200 and len(r.json().get("archived") or []) == 1
              and not (r1 / "api_ep_ghost").exists())
        r = client.post("/api/personas", json={"yaml": body})
        check("персона уже есть — обычный 409 без memory_exists",
              r.status_code == 409 and not r.json().get("memory_exists"))
    finally:
        server_mod._api_token = orig_token


def main():
    tmp = Path(tempfile.mkdtemp(prefix="persona_lifecycle_"))
    old_env = os.environ.get("VPC_DATA_DIR")
    os.environ["VPC_DATA_DIR"] = str(tmp / "vpc")
    from app.api import settings_api as sa

    personas_dir = tmp / "personas"
    personas_dir.mkdir()
    roots = [tmp / "root1", tmp / "root2"]
    for r in roots:
        r.mkdir()
    patches = [
        mock.patch.object(sa, "_PERSONAS_DIR", personas_dir),
        mock.patch.object(sa, "persona_dirs", lambda: [personas_dir]),
        mock.patch.object(sa, "_data_roots", lambda: list(roots)),
        mock.patch("app.api.runtime.persona_dirs", lambda: [personas_dir]),
    ]
    for p in patches:
        p.start()
    try:
        test_yaml_live(tmp)
        test_create_over_leftover(roots)
        test_rename_over_leftover(roots)
        test_endpoint(roots)
    finally:
        for p in reversed(patches):
            p.stop()
        if old_env is None:
            os.environ.pop("VPC_DATA_DIR", None)
        else:
            os.environ["VPC_DATA_DIR"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
