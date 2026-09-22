"""Ретенция персистентных словарей по chat_id (аудит: proactive_messaging,
state_engine, relationship растут на каждый chat_id и никогда не
уменьшаются — разовый чат остаётся в файле навсегда).

Проверяет:
  1. app.core.retention.prune_stale — общий helper: свежие записи остаются,
     старые (last_seen старше CHAT_RETENTION_DAYS) удаляются, записи БЕЗ
     метки (легаси) не трогаются, keep_min не даёт уйти ниже минимума.
  2. proactive_messaging.ChatActivityTracker — три поля (_known_chats/
     _last_activity/_chat_topics) прореживаются СОГЛАСОВАННО: живой чат не
     теряет topic_id, у устаревшего пропадают все три записи разом.
  3. state_engine.StateEngine — маркер updated_at, легаси-запись без него
     остаётся.
  4. relationship.RelationshipMemory — маркер last_message_at, легаси-запись
     без него остаётся.

Запуск: PYTHONPATH=. python3 scripts/test_retention.py
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ok = 0


def check(name, cond):
    global ok
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok = ok + 1 if cond else ok - 100


def main():
    from app.core.retention import CHAT_RETENTION_DAYS, prune_stale

    print("\n── 1. prune_stale: общий helper ──")
    now = time.time()
    fresh_ts = now - 3 * 86400          # 3 дня назад — свежо
    stale_ts = now - (CHAT_RETENTION_DAYS + 10) * 86400  # заведомо старше порога

    records = {
        "fresh": {"ts": fresh_ts},
        "stale": {"ts": stale_ts},
        "legacy": {"ts": None},         # метки нет — легаси
    }
    removed = prune_stale(dict(records), lambda k, v: v["ts"], CHAT_RETENTION_DAYS,
                          label="")
    check("prune_stale: удалён ровно старый ключ", removed == ["stale"])

    live = dict(records)
    removed2 = prune_stale(live, lambda k, v: v["ts"], CHAT_RETENTION_DAYS, label="")
    check("prune_stale: мутирует словарь на месте", "stale" not in live)
    check("prune_stale: свежая запись осталась", "fresh" in live)
    check("prune_stale: легаси (нет метки) осталась", "legacy" in live)
    check("prune_stale: возвращает список удалённых ключей", removed2 == ["stale"])

    check("prune_stale: пустой словарь → пустой список",
          prune_stale({}, lambda k, v: 0, CHAT_RETENTION_DAYS) == [])

    # last_seen бросает исключение — как «метки нет», не роняет вызывающего
    boom = {"a": object()}
    removed3 = prune_stale(boom, lambda k, v: 1 / 0, CHAT_RETENTION_DAYS)
    check("prune_stale: исключение в last_seen — запись не трогаем, не падаем",
          removed3 == [] and "a" in boom)

    # keep_min — не опускаться ниже минимума, удаляются самые старые сверх него
    many = {
        f"c{i}": {"ts": now - (CHAT_RETENTION_DAYS + 100 - i) * 86400}
        for i in range(5)
    }  # все "старые", c0 старше всех, c4 моложе всех (но всё ещё > порога)
    removed_km = prune_stale(many, lambda k, v: v["ts"], CHAT_RETENTION_DAYS, keep_min=3)
    check("prune_stale keep_min: не опустились ниже минимума", len(many) == 3)
    check("prune_stale keep_min: удалены самые старые (c0, c1)",
          set(removed_km) == {"c0", "c1"})
    check("prune_stale keep_min: свежие из старых (c2..c4) остались",
          set(many.keys()) == {"c2", "c3", "c4"})

    # ── Общий рабочий каталог для менеджеров (относительные data/{context}/…) ──
    tmp = tempfile.mkdtemp(prefix="retention_")
    os.chdir(tmp)

    print("\n── 2. proactive_messaging.ChatActivityTracker: три поля согласованно ──")
    from app.features.proactive_messaging import ChatActivityTracker

    chats_file = Path(f"data/pm_ctx/known_chats.json")
    chats_file.parent.mkdir(parents=True, exist_ok=True)
    chats_file.write_text(json.dumps({
        "chats": ["fresh_chat", "stale_chat", "legacy_chat"],
        "activity": {
            "fresh_chat": now - 3600,                                  # час назад
            "stale_chat": now - (CHAT_RETENTION_DAYS + 5) * 86400,     # давно
            # legacy_chat: намеренно отсутствует в activity — метки нет
        },
        "topics": {"fresh_chat": 42, "stale_chat": 7, "legacy_chat": 99},
    }), encoding="utf-8")

    tracker = ChatActivityTracker(context="pm_ctx")
    check("ActivityTracker: свежий чат остался в known_chats",
          "fresh_chat" in tracker.get_known_chats())
    check("ActivityTracker: устаревший чат удалён из known_chats",
          "stale_chat" not in tracker.get_known_chats())
    check("ActivityTracker: легаси-чат (без метки activity) НЕ удалён",
          "legacy_chat" in tracker.get_known_chats())
    check("ActivityTracker: у живого чата topic_id не потерян",
          tracker.get_topic("fresh_chat") == 42)
    check("ActivityTracker: у устаревшего чата topic_id тоже вычищен (согласованно)",
          tracker.get_topic("stale_chat") is None)
    check("ActivityTracker: у устаревшего чата activity тоже вычищена",
          tracker.get_last_activity("stale_chat") == 0)
    check("ActivityTracker: легаси-чат сохранил topic_id (не тронут вообще)",
          tracker.get_topic("legacy_chat") == 99)

    on_disk = json.loads(chats_file.read_text(encoding="utf-8"))
    check("ActivityTracker: прунинг сохранён на диск (не только в памяти)",
          "stale_chat" not in on_disk.get("chats", [])
          and "stale_chat" not in (on_disk.get("topics") or {}))

    print("\n── 3. state_engine.StateEngine: маркер updated_at ──")
    from app.core import timeutil
    from app.core.state_engine import StateEngine

    # get_db_paths("se_ctx")["stm"] == data/se_ctx/stm → .parent/living/state.json
    state_file = Path("data") / "se_ctx" / "living" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    fresh_iso = timeutil.now().isoformat(timespec="seconds")
    stale_dt = timeutil.from_ts(now - (CHAT_RETENTION_DAYS + 5) * 86400)
    stale_iso = stale_dt.isoformat(timespec="seconds")
    state_file.write_text(json.dumps({"chats": {
        "fresh_chat": {"energy": 50, "updated_at": fresh_iso},
        "stale_chat": {"energy": 50, "updated_at": stale_iso},
        "legacy_chat": {"energy": 50},  # без updated_at вовсе — легаси
    }}), encoding="utf-8")

    se = StateEngine(context="se_ctx", persona_name="tester", use_gemma=False)
    check("StateEngine: свежий чат остался", "fresh_chat" in se._states)
    check("StateEngine: устаревший чат удалён", "stale_chat" not in se._states)
    check("StateEngine: легаси-чат (без updated_at) НЕ удалён",
          "legacy_chat" in se._states)

    on_disk_se = json.loads(state_file.read_text(encoding="utf-8"))
    check("StateEngine: прунинг сохранён на диск",
          "stale_chat" not in on_disk_se.get("chats", {})
          and "fresh_chat" in on_disk_se.get("chats", {}))

    print("\n── 4. relationship.RelationshipMemory: маркер last_message_at ──")
    from app.core.relationship import RelationshipMemory

    rel_file = Path("data") / "rel_ctx" / "living" / "relationship.json"
    rel_file.parent.mkdir(parents=True, exist_ok=True)
    rel_file.write_text(json.dumps({"chats": {
        "fresh_chat": {"first_met_at": now - 86400, "user_messages": 5,
                       "last_message_at": now - 3600,
                       "shared_topics": [], "shared_moments": [], "stances": []},
        "stale_chat": {"first_met_at": now - 86400 * 300, "user_messages": 3,
                       "last_message_at": now - (CHAT_RETENTION_DAYS + 5) * 86400,
                       "shared_topics": [], "shared_moments": [], "stances": []},
        "legacy_chat": {"first_met_at": now - 86400, "user_messages": 1,
                        "last_message_at": None,  # легаси: метки нет
                        "shared_topics": [], "shared_moments": [], "stances": []},
    }}), encoding="utf-8")

    rel = RelationshipMemory(context="rel_ctx")
    check("RelationshipMemory: свежий чат остался", "fresh_chat" in rel._chats)
    check("RelationshipMemory: устаревший чат удалён", "stale_chat" not in rel._chats)
    check("RelationshipMemory: легаси-чат (last_message_at=None) НЕ удалён",
          "legacy_chat" in rel._chats)

    on_disk_rel = json.loads(rel_file.read_text(encoding="utf-8"))
    check("RelationshipMemory: прунинг сохранён на диск",
          "stale_chat" not in on_disk_rel.get("chats", {})
          and "fresh_chat" in on_disk_rel.get("chats", {}))

    print("\n── 5. RetentionTimer: дозор «не чаще раза в interval» ──")
    from app.core.retention import RETENTION_TICK_HOURS, RetentionTimer

    timer = RetentionTimer(interval_sec=3600)
    check("RetentionTimer: первый due() — True (ещё не срабатывал)",
          timer.due(now=10_000) is True)
    check("RetentionTimer: сразу повторный due() в пределах интервала — False",
          timer.due(now=10_100) is False)
    check("RetentionTimer: due() перед самой границей интервала — всё ещё False",
          timer.due(now=10_000 + 3600 - 1) is False)
    check("RetentionTimer: due() по истечении интервала — True",
          timer.due(now=10_000 + 3600) is True)
    check("RetentionTimer: срабатывание взводит новый отсчёт (снова False сразу после)",
          timer.due(now=10_000 + 3600 + 10) is False)

    print("\n── 6. Ретенция на живущем процессе: тик-пути, а не record_activity ──")

    # 6a. proactive_messaging.ChatActivityTracker.maybe_prune — зовётся из
    # цикла инициатив (_check_all_chats), не из record_activity
    tracker2 = ChatActivityTracker(context="pm_ctx2")
    tracker2.record_activity("late_stale")
    tracker2._last_activity["late_stale"] = now - (CHAT_RETENTION_DAYS + 5) * 86400
    tracker2._chat_topics["late_stale"] = 5

    tracker2._retention_timer._last_fire = time.time()  # «дозор только что сработал»
    tracker2.maybe_prune()
    check("ActivityTracker.maybe_prune: второй прогон в пределах интервала не прунит",
          "late_stale" in tracker2.get_known_chats())

    tracker2._retention_timer._last_fire = time.time() - RETENTION_TICK_HOURS * 3600 - 1
    tracker2.maybe_prune()
    check("ActivityTracker.maybe_prune: прогон после интервала прунит устаревшее",
          "late_stale" not in tracker2.get_known_chats()
          and tracker2.get_topic("late_stale") is None)

    # 6b. StateEngine — дозор внутри tick()/tick_and_score(), не на каждое
    # входящее сообщение (тик и так не вызывается на каждое сообщение)
    late_stale_iso = timeutil.from_ts(
        now - (CHAT_RETENTION_DAYS + 5) * 86400).isoformat(timespec="seconds")
    se._states["late_stale"] = {"energy": 50, "updated_at": late_stale_iso}

    se._retention_timer._last_fire = time.time()
    se.tick("trigger_chat", {})
    check("StateEngine.tick: второй прогон в пределах интервала не прунит",
          "late_stale" in se._states)

    se._retention_timer._last_fire = time.time() - RETENTION_TICK_HOURS * 3600 - 1
    se.tick("trigger_chat", {})
    check("StateEngine.tick: прогон после интервала прунит устаревшее",
          "late_stale" not in se._states)

    # 6c. RelationshipMemory — дозор внутри add_extracted() (урожай диалога
    # раз в несколько сообщений/по таймеру), не в record_message
    rel._chats["late_stale"] = {
        "first_met_at": now - 86400 * 300, "user_messages": 3,
        "last_message_at": now - (CHAT_RETENTION_DAYS + 5) * 86400,
        "shared_topics": [], "shared_moments": [], "stances": [],
    }

    rel._retention_timer._last_fire = time.time()
    rel.add_extracted("trigger_chat")
    check("RelationshipMemory.add_extracted: второй прогон в пределах интервала не прунит",
          "late_stale" in rel._chats)

    rel._retention_timer._last_fire = time.time() - RETENTION_TICK_HOURS * 3600 - 1
    rel.add_extracted("trigger_chat")
    check("RelationshipMemory.add_extracted: прогон после интервала прунит устаревшее",
          "late_stale" not in rel._chats)

    print(f"\nИтого: {ok} проверок")
    return 0


if __name__ == "__main__":
    sys.exit(main())
