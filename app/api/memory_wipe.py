"""Полное стирание памяти чата за пределами STM/LTM/дневника — вызывается из
/api/chat/clear (app/api/server.py) и его restore.

Хранилища: todo, напоминания, досье чата, обучение, feedback/ignore-streak
инициатив, ритм, живое состояние (per-chat срезы: состояние, офлайн-факты,
отношения, ежедневные выжимки; плюс глобальные для персоны: мир, инвентарь,
кэш контекста персоны), режим управления (что просили и где бот был: аудит
действий чата, страница чата в last_tab.json, память и прогоны агента задач,
pending-подтверждения, прогон/запись сценария, известные секреты чата).
НЕ трогаем: сохранённые сценарии (пользовательские плейбуки — конфиг),
включённость режима управления (настройка, не память), вкладки браузера
(пользователя), book/ (база знаний), files/ (загруженные документы — не
память диалога и не восстановимы из снапшота); адреса веб-чатов чистит
отдельный web_llm.clear_chat_urls.

КРИТИЧНО: бот запущен во время очистки — у менеджеров состояние в памяти и
файл перезаписывается при следующей мутации, а у reminder/learning фоновые
циклы продолжают действовать из памяти. Поэтому всё через живые менеджеры
(под их локами, с их _save), файловая правка — только фолбэк, когда фича
выключена и менеджера нет (паттерн _pop_initiative_history в server.py).
"""

import json
import logging
from pathlib import Path
from typing import get_args

from app.api.schemas import ClearPart
from app.api.security import safe_segment
from app.core.paths import data_dir

logger = logging.getLogger(__name__)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception as e:
        logger.debug(f"[MemoryWipe] запись {path.name}: {e}")


# ════════════ todo (TodoManager — stateless, файловый) ════════════

def _todo_file(context: str, chat_key: str) -> Path:
    return data_dir() / context / "todo" / safe_segment(chat_key) / "todo.txt"


def _collect_todo(bot, context, ck, out):
    path = _todo_file(context, ck)
    if path.is_file():
        try:
            out["todo"] = path.read_text(encoding="utf-8")
        except Exception:
            pass


def _wipe_todo(bot, context, ck):
    mgr = getattr(bot, "todo_manager", None)
    if mgr is not None:
        mgr.clear(ck)
    else:
        path = _todo_file(context, ck)
        if path.is_file():
            path.unlink()


def _restore_todo(bot, context, ck, data):
    path = _todo_file(context, ck)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(data), encoding="utf-8")


# ════════════ reminders (ReminderManager — in-memory list + фон-цикл) ════════════

def _reminders_file(context: str) -> Path:
    return data_dir() / context / "reminders" / "reminders.json"


def _collect_reminders(bot, context, ck, out):
    mgr = getattr(bot, "reminder_manager", None)
    if mgr is not None:
        with mgr._lock:
            mine = [dict(r) for r in mgr._reminders
                    if str(r.get("chat_id")) == ck]
    else:
        mine = [r for r in _read_json(_reminders_file(context), [])
                if str(r.get("chat_id")) == ck]
    if mine:
        out["reminders"] = mine


def _wipe_reminders(bot, context, ck):
    mgr = getattr(bot, "reminder_manager", None)
    if mgr is not None:
        with mgr._lock:
            mgr._reminders = [r for r in mgr._reminders
                              if str(r.get("chat_id")) != ck]
            mgr._save()
        try:
            mgr.clear_pending_remind(ck)  # диалог «напомни через…» тоже чистим
        except Exception:
            pass
        return
    path = _reminders_file(context)
    data = _read_json(path, [])
    if isinstance(data, list):
        _write_json(path, [r for r in data if str(r.get("chat_id")) != ck])


def _restore_reminders(bot, context, ck, data):
    mgr = getattr(bot, "reminder_manager", None)
    if mgr is not None:
        with mgr._lock:
            mgr._reminders.extend(dict(r) for r in data)
            # Восстановленные записи несут свой старый id из бэкапа — он мог
            # с тех пор достаться новой записи (id выдаются заново на пустом
            # множестве после wipe). _ensure_ids() находит такие дубли и
            # переставляет id только у них, остальные записи не трогает;
            # сама она сохраняет файл лишь если что-то поменяла, поэтому
            # финальный _save() всё равно нужен явно.
            mgr._ensure_ids()
            mgr._save()
        return
    path = _reminders_file(context)
    cur = _read_json(path, [])
    if not isinstance(cur, list):
        cur = []
    _write_json(path, cur + list(data))


# ════════════ chat_dossier (ChatDossier — кэш _profiles) ════════════

def _dossier_file(context: str) -> Path:
    return data_dir() / context / "chat_dossier.json"


def _collect_dossier(bot, context, ck, out):
    mgr = getattr(bot, "_chat_dossier", None)
    entry = None
    if mgr is not None:
        # _profiles живого менеджера хранит объекты ChatProfile, а не dict —
        # сериализует сам менеджер через export_profile() (формат файла досье
        # + водяной знак экстракции фактов).
        entry = mgr.export_profile(ck)
    else:
        entry = _read_json(_dossier_file(context), {}).get(ck)
    if isinstance(entry, dict) and entry:
        out["dossier"] = entry


def _wipe_dossier(bot, context, ck):
    mgr = getattr(bot, "_chat_dossier", None)
    if mgr is not None:
        with mgr._lock:
            mgr._profiles.pop(ck, None)
            mgr._facts_seen.pop(ck, None)
            mgr._facts_watermark.pop(ck, None)
            mgr._save()
        return
    path = _dossier_file(context)
    data = _read_json(path, {})
    if isinstance(data, dict) and ck in data:
        data.pop(ck)
        _write_json(path, data)


def _restore_dossier(bot, context, ck, data):
    mgr = getattr(bot, "_chat_dossier", None)
    if mgr is not None:
        # Только через import_profile(): ChatDossier хранит в _profiles объекты
        # ChatProfile, сырой dict сломал бы get_profile_snapshot/
        # get_context_block и сохранение файла досье
        mgr.import_profile(ck, data)
        return
    path = _dossier_file(context)
    cur = _read_json(path, {})
    if not isinstance(cur, dict):
        cur = {}
    # Служебные ключи экспорта (водяной знак экстракции) в файл не пишем —
    # это не поля профиля, знак живёт только в памяти менеджера
    cur[ck] = {k: v for k, v in data.items() if not str(k).startswith("_")}
    _write_json(path, cur)


# ════════════ learning (LearningManager — сессии + фон-цикл уроков) ════════════

def _learning_file(context: str) -> Path:
    return data_dir() / context / "learning" / "learning.json"


def _collect_learning(bot, context, ck, out):
    mgr = getattr(bot, "learning_manager", None)
    if mgr is not None:
        with mgr._lock:
            mine = [dict(s) for s in mgr._sessions
                    if str(s.get("chat_id")) == ck]
    else:
        mine = [s for s in _read_json(_learning_file(context), [])
                if str(s.get("chat_id")) == ck]
    if mine:
        out["learning"] = mine


def _wipe_learning(bot, context, ck):
    mgr = getattr(bot, "learning_manager", None)
    if mgr is not None:
        # Весь чат стирается целиком: сессии + все ожидающие setup «как часто?»
        # (в группе их несколько — по одному на участника) + реестр открытых
        # вопросов уроков. clear_chat() делает это под своим локом одной
        # транзакцией, не трогая здесь приватные поля менеджера
        # (_sessions/_setup_state/_question_msgs).
        mgr.clear_chat(ck)
        return
    path = _learning_file(context)
    data = _read_json(path, [])
    if isinstance(data, list):
        _write_json(path, [s for s in data if str(s.get("chat_id")) != ck])


def _restore_learning(bot, context, ck, data):
    mgr = getattr(bot, "learning_manager", None)
    if mgr is not None:
        with mgr._lock:
            mgr._sessions.extend(dict(s) for s in data)
            mgr._save()
        return
    path = _learning_file(context)
    cur = _read_json(path, [])
    if not isinstance(cur, list):
        cur = []
    _write_json(path, cur + list(data))


# ════════════ proactive: feedback + ignore_streak ════════════

def _collect_proactive(bot, context, ck, out):
    p = getattr(bot, "proactive", None)
    if p is not None:
        fb = p._feedback.get(ck)
        if isinstance(fb, dict) and fb:
            out["proactive_feedback"] = dict(fb)
        streak = p._ignore_streak.get(ck)
        if streak:
            out["ignore_streak"] = streak
        return
    fb = _read_json(data_dir() / context / "proactive_feedback.json", {}).get(ck)
    if isinstance(fb, dict) and fb:
        out["proactive_feedback"] = fb
    streak = _read_json(data_dir() / context / "ignore_streak.json", {}).get(ck)
    if streak:
        out["ignore_streak"] = streak


def _wipe_proactive(bot, context, ck):
    p = getattr(bot, "proactive", None)
    if p is not None:
        p._feedback.pop(ck, None)
        p._save_feedback()
        p._ignore_streak.pop(ck, None)
        p._save_ignore_streak()
        return
    for fname in ("proactive_feedback.json", "ignore_streak.json"):
        path = data_dir() / context / fname
        data = _read_json(path, {})
        if isinstance(data, dict) and ck in data:
            data.pop(ck)
            _write_json(path, data)


def _restore_proactive(bot, context, ck, stores):
    fb, streak = stores.get("proactive_feedback"), stores.get("ignore_streak")
    p = getattr(bot, "proactive", None)
    if p is not None:
        if fb is not None:
            p._feedback[ck] = dict(fb)
            p._save_feedback()
        if streak is not None:
            p._ignore_streak[ck] = streak
            p._save_ignore_streak()
        return
    for fname, val in (("proactive_feedback.json", fb),
                       ("ignore_streak.json", streak)):
        if val is None:
            continue
        path = data_dir() / context / fname
        data = _read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        data[ck] = val
        _write_json(path, data)


# ════════════ rhythm (RhythmManager — отметки дня по чату) ════════════

def _rhythm_file(context: str) -> Path:
    return data_dir() / context / "rhythm_state.json"


def _collect_rhythm(bot, context, ck, out):
    r = getattr(bot, "rhythm", None)
    if r is not None:
        entry = r._state.get("chats", {}).get(ck)
    else:
        entry = _read_json(_rhythm_file(context), {}).get("chats", {}).get(ck)
    if isinstance(entry, dict) and entry:
        out["rhythm"] = dict(entry)


def _wipe_rhythm(bot, context, ck):
    r = getattr(bot, "rhythm", None)
    if r is not None:
        with r._lock:
            r._state.get("chats", {}).pop(ck, None)
            r._presence_ts.pop(ck, None)
            r._save()
        return
    path = _rhythm_file(context)
    data = _read_json(path, {})
    if isinstance(data, dict) and ck in (data.get("chats") or {}):
        data["chats"].pop(ck)
        _write_json(path, data)


def _restore_rhythm(bot, context, ck, data):
    r = getattr(bot, "rhythm", None)
    if r is not None:
        with r._lock:
            r._state.setdefault("chats", {})[ck] = dict(data)
            r._save()
        return
    path = _rhythm_file(context)
    cur = _read_json(path, {})
    if not isinstance(cur, dict):
        cur = {}
    cur.setdefault("chats", {})[ck] = data
    _write_json(path, cur)


# ════════════ living (LivingPersona + движки) + inventory ════════════
# Per-chat: состояние, офлайн-факты, отношения, ежедневные выжимки,
# расписание мира. Глобальные (мир, инвентарь, кэш контекста) — жизнь
# персоны одна, чат в веб-режиме один — стираем всё; всё лежит в снапшоте.

_WORLD_DEFAULT = {
    "npcs": [], "places": [], "storylines": [], "external_stimuli": [],
    "next_id": 1, "next_event_at": {}, "next_fetch_at": 0.0,
    "seeded": False, "plans": [],
}


def _living_files(context: str) -> dict:
    base = data_dir() / context / "living"
    return {
        "state": base / "state.json",
        "offline": base / "offline_log.json",
        "relationship": base / "relationship.json",
        "summarizer": base / "summarizer_state.json",
        "world": base / "world.json",
        "persona_context": base / "persona_context.json",
        "inventory": data_dir() / context / "inventory.json",
    }


def _collect_living(bot, context, ck, out):
    lv = getattr(bot, "living", None)
    inv = {}
    if lv is not None:
        se = lv.state_engine
        with se._lock:
            if ck in se._states:
                inv["state"] = json.loads(json.dumps(se._states[ck]))
            mine_log = [dict(e) for e in se._log if str(e.get("chat_id")) == ck]
            if mine_log:
                inv["offline"] = mine_log
        rel = lv.relationship
        with rel._lock:
            if ck in rel._chats:
                inv["relationship"] = json.loads(json.dumps(rel._chats[ck]))
        sm = lv.summarizer
        with sm._lock:
            ld = sm._state.get("last_daily", {}).get(ck)
            if ld:
                inv["last_daily"] = ld
        we = lv.world_engine
        with we._lock:
            inv["world"] = json.loads(json.dumps(we._world))
        pcl = lv.persona_context_layer
        with pcl._lock:
            if pcl._cache:
                inv["persona_context"] = json.loads(json.dumps(pcl._cache))
    else:
        f = _living_files(context)
        st = _read_json(f["state"], {}).get("chats", {}).get(ck)
        if st:
            inv["state"] = st
        log = _read_json(f["offline"], {})
        mine = [e for e in (log.get("entries") or [])
                if str(e.get("chat_id")) == ck]
        if mine:
            inv["offline"] = mine
        rel = _read_json(f["relationship"], {}).get(ck)
        if rel:
            inv["relationship"] = rel
        ld = _read_json(f["summarizer"], {}).get("last_daily", {}).get(ck)
        if ld:
            inv["last_daily"] = ld
        world = _read_json(f["world"], None)
        if isinstance(world, dict) and any(
                world.get(k) for k in ("npcs", "places", "storylines", "plans")):
            inv["world"] = world
        pc = _read_json(f["persona_context"], None)
        if pc:
            inv["persona_context"] = pc
    im = getattr(bot, "inventory_manager", None)
    if im is not None:
        with im._lock:
            if im._items:
                inv["inventory"] = json.loads(json.dumps(im._items))
    else:
        items = _read_json(_living_files(context)["inventory"], {}) \
            .get("items")
        if items:
            inv["inventory"] = items
    if inv:
        out["living"] = inv


def _wipe_living(bot, context, ck):
    lv = getattr(bot, "living", None)
    if lv is not None:
        se = lv.state_engine
        with se._lock:
            se._states.pop(ck, None)
            se._log = [e for e in se._log if str(e.get("chat_id")) != ck]
            se._save_state()
            se._save_log()
        rel = lv.relationship
        with rel._lock:
            rel._chats.pop(ck, None)
            rel._save()
        sm = lv.summarizer
        with sm._lock:
            if isinstance(sm._state.get("last_daily"), dict):
                sm._state["last_daily"].pop(ck, None)
            sm._save()
        we = lv.world_engine
        with we._lock:
            we._world = dict(_WORLD_DEFAULT)
            we._next_event_at = {}
            we._save()
        pcl = lv.persona_context_layer
        with pcl._lock:
            pcl._cache = None
            pcl._save()
        lv._persona_context = None
        # Иначе NPC не перезасеются до рестарта процесса
        lv._seeded_this_run = False
    else:
        f = _living_files(context)
        st = _read_json(f["state"], {})
        if isinstance(st, dict) and ck in (st.get("chats") or {}):
            st["chats"].pop(ck)
            _write_json(f["state"], st)
        log = _read_json(f["offline"], {})
        if isinstance(log, dict) and log.get("entries"):
            log["entries"] = [e for e in log["entries"]
                              if str(e.get("chat_id")) != ck]
            _write_json(f["offline"], log)
        rel = _read_json(f["relationship"], {})
        if isinstance(rel, dict) and ck in rel:
            rel.pop(ck)
            _write_json(f["relationship"], rel)
        sm = _read_json(f["summarizer"], {})
        if isinstance(sm, dict) and ck in (sm.get("last_daily") or {}):
            sm["last_daily"].pop(ck)
            _write_json(f["summarizer"], sm)
        if f["world"].is_file():
            _write_json(f["world"], dict(_WORLD_DEFAULT))
        if f["persona_context"].is_file():
            f["persona_context"].unlink()
    im = getattr(bot, "inventory_manager", None)
    if im is not None:
        with im._lock:
            im._items.clear()
            im._save()
    else:
        path = _living_files(context)["inventory"]
        if path.is_file():
            _write_json(path, {"items": []})


def _restore_living(bot, context, ck, inv):
    lv = getattr(bot, "living", None)
    if lv is not None:
        se = lv.state_engine
        with se._lock:
            if inv.get("state") is not None:
                se._states[ck] = inv["state"]
            if inv.get("offline"):
                se._log.extend(dict(e) for e in inv["offline"])
            se._save_state()
            se._save_log()
        rel = lv.relationship
        with rel._lock:
            if inv.get("relationship") is not None:
                rel._chats[ck] = inv["relationship"]
            rel._save()
        sm = lv.summarizer
        with sm._lock:
            if inv.get("last_daily") is not None:
                sm._state.setdefault("last_daily", {})[ck] = inv["last_daily"]
                sm._save()
        if inv.get("world") is not None:
            we = lv.world_engine
            with we._lock:
                we._world = dict(inv["world"])
                we._next_event_at = dict(inv["world"].get("next_event_at") or {})
                we._save()
            lv._seeded_this_run = bool(inv["world"].get("seeded"))
        if inv.get("persona_context") is not None:
            pcl = lv.persona_context_layer
            with pcl._lock:
                pcl._cache = inv["persona_context"]
                pcl._save()
    else:
        f = _living_files(context)
        if inv.get("state") is not None:
            st = _read_json(f["state"], {})
            st.setdefault("chats", {})[ck] = inv["state"]
            _write_json(f["state"], st)
        if inv.get("offline"):
            log = _read_json(f["offline"], {"entries": [], "next_id": 1})
            log.setdefault("entries", []).extend(inv["offline"])
            _write_json(f["offline"], log)
        if inv.get("relationship") is not None:
            _write_json(f["relationship"],
                        {**_read_json(f["relationship"], {}),
                         ck: inv["relationship"]})
        if inv.get("last_daily") is not None:
            sm = _read_json(f["summarizer"], {})
            sm.setdefault("last_daily", {})[ck] = inv["last_daily"]
            _write_json(f["summarizer"], sm)
        if inv.get("world") is not None:
            _write_json(f["world"], inv["world"])
        if inv.get("persona_context") is not None:
            _write_json(f["persona_context"], inv["persona_context"])
    if inv.get("inventory") is not None:
        im = getattr(bot, "inventory_manager", None)
        if im is not None:
            with im._lock:
                im._items = list(inv["inventory"])
                im._save()
        else:
            _write_json(_living_files(context)["inventory"],
                        {"items": list(inv["inventory"])})


# ════════════ режим управления (computer_control + агент задач + сценарии) ════════════

def _cc_dir(context: str) -> Path:
    return data_dir() / context / "computer_control"


def _collect_control(bot, context, ck, out):
    # Чистое чтение: запись страницы чата, строки аудита чата (текущий файл
    # и ротации), память задач чата. Живое состояние (pending, прогоны) —
    # транзиент, в корзину не кладётся
    from app.features.cc_privacy import AUDIT_BACKUPS
    base = _cc_dir(context)
    cc = getattr(bot, "computer_control", None)
    if cc is not None:
        base = Path(cc.base_dir)
    snap: dict = {}
    tabs = _read_json(base / "last_tab.json", {})
    if isinstance(tabs, dict):
        if isinstance(tabs.get("chats"), dict):
            if tabs["chats"].get(ck) is not None:
                snap["last_tab"] = tabs["chats"][ck]
        elif tabs.get("host"):
            snap["last_tab_legacy"] = tabs
    lines = []
    audit = base / "audit.jsonl"
    for i in range(AUDIT_BACKUPS, -1, -1):
        p = audit if i == 0 else audit.with_name(f"{audit.name}.{i}")
        try:
            raw = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            continue
        for line in raw:
            try:
                if str(json.loads(line).get("chat_id")) == ck:
                    lines.append(line)
            except Exception:
                continue
    if lines:
        snap["audit"] = lines
    ta = getattr(bot, "task_agent", None)
    mem_path = Path(ta._memory_path) if ta is not None \
        else base / "task_memory.json"
    mem = _read_json(mem_path, {})
    if isinstance(mem, dict) and mem.get(ck):
        snap["task_memory"] = mem[ck]
    if snap:
        out["control"] = snap


def _wipe_control(bot, context, ck):
    # Сначала живое (прогоны не должны дописать стёртое), потом файлы
    sm = getattr(bot, "scenario_manager", None)
    if sm is not None:
        with sm._lock:
            sm._runs.pop(ck, None)
            sm._recording.pop(ck, None)
            sm._offered.pop(ck, None)
    ta = getattr(bot, "task_agent", None)
    if ta is not None:
        ta.forget_chat(ck)
    else:
        path = _cc_dir(context) / "task_memory.json"
        mem = _read_json(path, {})
        if isinstance(mem, dict) and ck in mem:
            mem.pop(ck)
            _write_json(path, mem)
    cc = getattr(bot, "computer_control", None)
    if cc is not None:
        cc.forget_chat(ck)
    else:
        from app.features.computer_control import forget_chat_files
        forget_chat_files(_cc_dir(context), ck)
    # Бот: пароли, названные в чате (агент вводил их ходом позже), и
    # отложенные скриншоты страниц («ещё» — досылка альбома)
    vault = getattr(bot, "__dict__", {}).get("_cc_known_secrets")
    if vault is not None:
        vault.purge(ck)
    for name in ("_pending_photos", "_pending_more_photos"):
        store = getattr(bot, name, None)
        if isinstance(store, dict):
            store.pop(ck, None)


def _restore_control(bot, context, ck, data):
    cc = getattr(bot, "computer_control", None)
    files = {k: v for k, v in data.items() if k != "task_memory"}
    if cc is not None:
        cc.restore_chat(ck, files)
    else:
        from app.features.computer_control import restore_chat_files
        restore_chat_files(_cc_dir(context), ck, files)
    records = data.get("task_memory")
    if records:
        ta = getattr(bot, "task_agent", None)
        if ta is not None:
            ta.restore_memory(ck, records)
        else:
            path = _cc_dir(context) / "task_memory.json"
            mem = _read_json(path, {})
            if not isinstance(mem, dict):
                mem = {}
            mem[ck] = list(records) + list(mem.get(ck) or [])
            _write_json(path, mem)


# ════════════ публичный интерфейс ════════════

# Все части «Очистить диалог»; stm/ltm/diary/webchat и история инициатив
# стираются в server.chat_clear, срезы ниже — здесь
ALL_PARTS: tuple[str, ...] = get_args(ClearPart)

# Часть → (сбор в снапшот, стирание). initiatives здесь — отклик и
# ignore-streak самоинициатив (история и счётчик дня — в server.py)
_STORES = (
    ("todo", _collect_todo, _wipe_todo),
    ("reminders", _collect_reminders, _wipe_reminders),
    ("dossier", _collect_dossier, _wipe_dossier),
    ("learning", _collect_learning, _wipe_learning),
    ("initiatives", _collect_proactive, _wipe_proactive),
    ("rhythm", _collect_rhythm, _wipe_rhythm),
    ("living", _collect_living, _wipe_living),
    ("control", _collect_control, _wipe_control),
)


def collect_stores(bot, persona: str, chat_key: str, parts=None) -> dict:
    # Срезы памяти чата для снапшота корзины (до удаления). Чистое чтение.
    # parts — только эти части (None — все)
    context = f"api_{persona}"
    ck = str(chat_key)
    out: dict = {}
    for name, collect, _wipe in _STORES:
        if parts is not None and name not in parts:
            continue
        try:
            collect(bot, context, ck, out)
        except Exception as e:
            logger.warning(f"[MemoryWipe] {persona}: срез {name} не собран: {e}")
    return out


def wipe_stores(bot, persona: str, chat_key: str, parts=None):
    # Стирание памяти чата поверх STM/LTM/дневника (те — в server.py).
    # Живые менеджеры в приоритете, файлы — фолбэк при выключенных фичах.
    # parts — только эти части (None — все)
    context = f"api_{persona}"
    ck = str(chat_key)
    done = []
    for name, _collect, wipe in _STORES:
        if parts is not None and name not in parts:
            continue
        try:
            wipe(bot, context, ck)
            done.append(name)
        except Exception as e:
            logger.warning(f"[MemoryWipe] {persona}: очистка {name}: {e}")
    scope = "полностью" if parts is None else "частично"
    logger.info(f"[MemoryWipe] {persona}: память чата {ck} стёрта {scope} "
                f"({'/'.join(done) or '—'})")


def restore_stores(bot, persona: str, chat_key: str, stores: dict):
    # Вернуть срезы из снапшота корзины (undo полной очистки).
    if not stores:
        return
    context = f"api_{persona}"
    ck = str(chat_key)
    for name, fn, key in (
            ("todo", _restore_todo, "todo"),
            ("reminders", _restore_reminders, "reminders"),
            ("dossier", _restore_dossier, "dossier"),
            ("learning", _restore_learning, "learning"),
            ("rhythm", _restore_rhythm, "rhythm"),
            ("control", _restore_control, "control")):
        try:
            if stores.get(key) is not None:
                fn(bot, context, ck, stores[key])
        except Exception as e:
            logger.warning(f"[MemoryWipe] {persona}: восстановление {name}: {e}")
    try:
        _restore_proactive(bot, context, ck, stores)
    except Exception as e:
        logger.warning(f"[MemoryWipe] {persona}: восстановление proactive: {e}")
    try:
        if stores.get("living"):
            _restore_living(bot, context, ck, stores["living"])
    except Exception as e:
        logger.warning(f"[MemoryWipe] {persona}: восстановление living: {e}")
