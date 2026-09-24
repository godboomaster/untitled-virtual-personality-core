"""Тесты ядра памяти.

Проверяет:
  1. self_memory — лок только на состояние: LLM-вызов (суммаризация архива)
     идёт ВНЕ лока, tick()/export_state() не ждут сеть; результат вливается
     с проверкой поколения (очистка/восстановление во время генерации не
     затирается) и не теряет эпизоды, доехавшие в архив за время вызова;
  2. chroma_space — единая точка открытия коллекций с явной метрикой cosine,
     перенос уже существующей l2-коллекции без потери данных и эмбеддингов,
     доигрывание прерванного переноса; forget/update_fact на cosine
     находят перефразированный факт;
  3. file_vector_db — все операции под одним RLock (конкурентные add_file
     не нарушают лимит max_docs), _loaded_docs — ограниченный кеш;
  4. bounded_cache — LRU/TTL, dict-API, потокобезопасность; буферы STM
     ограничены и перечитываются из ChromaDB после вытеснения;
  5. migrate_stm — порядок «прочитать и провалидировать импорт → бэкап →
     замена»: битый/пустой/чужого формата импорт не трогает базу;
  6. rich_message_formatter — HTML внутри код-блоков и инлайн-кода
     экранируется тем же путём, что и остальной текст;
  7. restore_memory — цели восстановления вычисляются из каталога дампов и
     конфига, а не из прошитого списка персон.

Всё — на временных каталогах (cwd переносится в tmp), реальный data/ не
трогается. Запуск: PYTHONPATH=. python3 scripts/test_memory_core.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time
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


def _shared_embedder():
    """Один инстанс эмбеддера на весь тест (модель грузится один раз) и
    offline-режим HF, если модель уже в кэше — иначе стартовые HEAD-запросы
    к hub'у добавляют десятки секунд к тесту."""
    from app.core import st_embedder
    if st_embedder._model_cached(st_embedder.ST_MODEL_NAME):
        st_embedder.force_hf_offline()
    embedder = st_embedder.create_st_embedder()
    # Подменяем фабрику во всех модулях, которые её вызывают: модель
    # кешируется внутри SentenceTransformer, но так надёжнее и быстрее
    import app.core.file_vector_db as fvdb
    import app.core.memory as memory_mod
    import migrate_stm
    for module in (st_embedder, memory_mod, fvdb, migrate_stm):
        module.create_st_embedder = lambda *a, **k: embedder
    return embedder


# ─── Фейки ───────────────────────────────────────────────

class FakeRouter:
    # Роутер с управляемой задержкой — имитирует LLM-вызов на секунды

    def __init__(self, delay: float = 0.0, reply: str = None):
        self.delay = delay
        self.reply = reply if reply is not None else (
            "Итог: много разговоров, много новых тем, стало спокойнее.")
        self.calls = 0
        self.started = threading.Event()
        self._lock = threading.Lock()

    def get_response(self, messages, **kw):
        with self._lock:
            self.calls += 1
        self.started.set()
        time.sleep(self.delay)
        return self.reply


class FakeMainRouter:
    # Основной роутер персоны для LongTermMemory (без сети и провайдеров)
    active_provider = "fake"

    def is_local_primary(self):
        return False

    def get_response(self, messages, **kw):
        return None


def _episode(text: str) -> dict:
    return {"text": text, "timestamp": "2026-01-01T00:00:00", "msg_count": 1}


# ─── 1. self_memory: лок только на состояние ─────────────

def test_self_memory(tmp: Path):
    print("\n── self_memory: лок и свежесть состояния ──")
    from app.core.self_memory import BotSelfMemory, MAX_ARCHIVE_EPISODES

    router = FakeRouter(delay=1.5)
    sm = BotSelfMemory("memcore_self", "Тест", router)

    # Архив заполнен до порога — добавление эпизода запустит суммаризацию
    with sm._lock:
        sm._episodes["archive"] = [_episode(f"старая запись {i}")
                                   for i in range(MAX_ARCHIVE_EPISODES)]

    t = threading.Thread(target=sm.add_external_episode,
                         args=("внешний эпизод из офлайн-жизни персоны",))
    t.start()
    router.started.wait(timeout=5)
    time.sleep(0.05)

    t0 = time.time()
    sm.export_state()                      # берёт лок
    sm.tick([], "u1", "короткое")           # тоже берёт лок
    waited = time.time() - t0
    check(f"add_external_episode: лок не держится на время LLM "
          f"(ожидание {waited:.2f}с)", waited < 0.5)

    # Пока идёт LLM, в архив уезжает новый эпизод (как при параллельной
    # архивации из _write_episode) — он не должен пропасть
    with sm._lock:
        sm._episodes["archive"].append(_episode("доехала во время саммари"))
    t.join(timeout=15)
    check("суммаризация записала life_summary",
          sm._episodes["life_summary"].startswith("Итог:"))
    check("эпизод, доехавший в архив во время LLM, не потерян",
          [e["text"] for e in sm._episodes["archive"]] == ["доехала во время саммари"])
    check("внешний эпизод лежит в активных",
          any("офлайн-жизни" in e["text"] for e in sm._episodes["active"]))

    # Очистка во время LLM-вызова: результат вливать нельзя
    router2 = FakeRouter(delay=1.0, reply="Другое саммари, которое не должно попасть в дневник.")
    sm2 = BotSelfMemory("memcore_self2", "Тест2", router2)
    with sm2._lock:
        sm2._episodes["archive"] = [_episode(f"з{i}") for i in range(MAX_ARCHIVE_EPISODES)]
    t2 = threading.Thread(target=sm2._summarize_archive)
    t2.start()
    router2.started.wait(timeout=5)
    sm2.clear_all()
    t2.join(timeout=15)
    check("очистка во время LLM: саммари не влилось в очищенный дневник",
          sm2._episodes["life_summary"] == "" and sm2._episodes["archive"] == [])

    # Параллельные суммаризации не дублируют LLM-вызов
    router3 = FakeRouter(delay=0.8)
    sm3 = BotSelfMemory("memcore_self3", "Тест3", router3)
    with sm3._lock:
        sm3._episodes["archive"] = [_episode(f"я{i}") for i in range(MAX_ARCHIVE_EPISODES)]
    threads = [threading.Thread(target=sm3._summarize_archive) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=15)
    check(f"параллельные суммаризации: один LLM-вызов (было {router3.calls})",
          router3.calls == 1)

    # Битый файл дневника: общий helper персистентности (atomic_io)
    ep_file = sm3._episodes_file
    ep_file.write_text("{битый json", encoding="utf-8")
    sm4 = BotSelfMemory("memcore_self3", "Тест3", FakeRouter())
    corrupt = list(ep_file.parent.glob("episodes.json.corrupt-*"))
    check("битый episodes.json: копия .corrupt-* рядом, дефолт вместо падения",
          bool(corrupt) and sm4._episodes["active"] == [])


# ─── 2. chroma_space: единая метрика ─────────────────────

def test_chroma_space(tmp: Path):
    print("\n── chroma_space: явная метрика и перенос ──")
    import chromadb
    from app.core.chroma_space import (
        VECTOR_SPACE, collection_space, open_collection, _MIG_SUFFIX,
    )

    client = chromadb.PersistentClient(path=str(tmp / "chroma_space_db"))

    fresh = open_collection(client, "fresh_col", embedding_function=None)
    check(f"новая коллекция открывается с метрикой {VECTOR_SPACE}",
          collection_space(fresh) == VECTOR_SPACE)

    # Легаси-коллекция с дефолтной l2 и данными
    legacy = client.create_collection("legacy_col", embedding_function=None)
    legacy.add(ids=["x", "y"], documents=["первый", "второй"],
               embeddings=[[1.0, 0.0], [0.0, 1.0]],
               metadatas=[{"user_id": "u1"}, {"user_id": "u2"}])
    check("легаси-коллекция действительно была l2",
          collection_space(legacy) == "l2")

    migrated = open_collection(client, "legacy_col", embedding_function=None)
    got = migrated.get(include=["documents", "metadatas", "embeddings"])
    check("перенос: метрика стала cosine",
          collection_space(migrated) == VECTOR_SPACE)
    check("перенос: данные целы (ids/документы/метаданные)",
          sorted(got["ids"]) == ["x", "y"]
          and sorted(got["documents"]) == ["второй", "первый"]
          and {m["user_id"] for m in got["metadatas"]} == {"u1", "u2"})
    embeddings = {rid: list(vec) for rid, vec in zip(got["ids"], got["embeddings"])}
    check("перенос: эмбеддинги скопированы как есть (модель не пересчитывала)",
          embeddings["x"] == [1.0, 0.0] and embeddings["y"] == [0.0, 1.0])
    names = [c.name for c in client.list_collections()]
    check("перенос: временная коллекция не осталась",
          not any(_MIG_SUFFIX in n for n in names))

    # Прерванный перенос: временная коллекция есть, основной нет
    tmp_name = f"broken_col{_MIG_SUFFIX}{VECTOR_SPACE}"
    half = client.create_collection(tmp_name, embedding_function=None,
                                    metadata={"hnsw:space": VECTOR_SPACE})
    half.add(ids=["a"], documents=["уцелело"], embeddings=[[0.5, 0.5]])
    recovered = open_collection(client, "broken_col", embedding_function=None)
    check("прерванный перенос доигран: данные на месте под нужным именем",
          recovered.count() == 1
          and recovered.get()["documents"] == ["уцелело"]
          and collection_space(recovered) == VECTOR_SPACE)
    check("прерванный перенос: временной коллекции больше нет",
          not any(_MIG_SUFFIX in c.name for c in client.list_collections()))

    # Недокопированный остаток при живой основной коллекции — выбрасывается
    main = open_collection(client, "keep_col", embedding_function=None)
    main.add(ids=["m"], documents=["основное"], embeddings=[[1.0, 1.0]])
    leftover = client.create_collection(f"keep_col{_MIG_SUFFIX}{VECTOR_SPACE}",
                                        embedding_function=None)
    leftover.add(ids=["m"], documents=["остаток"], embeddings=[[1.0, 1.0]])
    kept = open_collection(client, "keep_col", embedding_function=None)
    check("остаток неудачного переноса удалён, основная коллекция не тронута",
          kept.get()["documents"] == ["основное"]
          and not any(_MIG_SUFFIX in c.name for c in client.list_collections()))


# ─── 3. Пороги forget/update_fact на cosine ──────────────

def test_ltm_thresholds(tmp: Path):
    print("\n── LTM: пороги схожести работают на cosine ──")
    from app.core.chroma_space import VECTOR_SPACE, collection_space
    from app.core.memory import FORGET_MAX_DISTANCE, LongTermMemory

    ltm = LongTermMemory(db_path=str(tmp / "ltm_db"), context="memcore_ltm",
                         main_router=FakeMainRouter())
    check(f"коллекция LTM открыта с метрикой {VECTOR_SPACE}",
          collection_space(ltm.collection) == VECTOR_SPACE and ltm.space == VECTOR_SPACE)

    ltm.save_facts("Город: �город", user_id="u1")
    ltm.save_facts("Хобби: играет на гитаре", user_id="u1")

    # Перефразировка должна укладываться в порог forget на cosine —
    # иначе «забудь про X» молча ничего не находит
    res = ltm.collection.query(query_texts=["он живёт в �городе"], n_results=1,
                               where={"user_id": "u1"}, include=["distances"])
    distance = res["distances"][0][0]
    check(f"перефразировка попадает в порог forget (d={distance:.3f} <= "
          f"{FORGET_MAX_DISTANCE})", distance <= FORGET_MAX_DISTANCE)
    # Посторонний запрос должен быть дальше порога — иначе «/forget что
    # угодно» стирал бы ближайший факт пользователя
    res_far = ltm.collection.query(query_texts=["квантовая хромодинамика"], n_results=1,
                                   where={"user_id": "u1"}, include=["distances"])
    d_far = res_far["distances"][0][0]
    check(f"посторонний запрос вне порога forget (d={d_far:.3f} > {FORGET_MAX_DISTANCE})",
          d_far > FORGET_MAX_DISTANCE)
    check("forget по постороннему запросу ничего не удаляет",
          ltm.forget("квантовая хромодинамика", "u1") is None
          and sorted(ltm.get_all_facts("u1")) == ["Город: �город", "Хобби: играет на гитаре"])
    forgotten = ltm.forget("он живёт в �городе", "u1")
    check("forget удалил найденный факт", forgotten == "Город: �город")
    check("forget не задел остальные факты",
          ltm.get_all_facts("u1") == ["Хобби: играет на гитаре"])

    old = ltm.update_fact("Хобби: играет на гитаре", "Хобби: играет на банджо", "u1")
    check("update_fact заменил факт по точному совпадению",
          old == "Хобби: играет на гитаре"
          and ltm.get_all_facts("u1") == ["Хобби: играет на банджо"])
    missed = ltm.update_fact("совершенно посторонний запрос про коллайдер",
                             "Город: Томск", "u1")
    check("update_fact не затирает чужой факт при непохожем запросе",
          missed is None and ltm.get_all_facts("u1") == ["Хобби: играет на банджо"])


# ─── 4. bounded_cache + буферы STM ───────────────────────

def test_bounded_cache_and_stm(tmp: Path):
    print("\n── bounded_cache и буферы STM ──")
    from app.core.bounded_cache import BoundedCache
    from app.core.chroma_space import VECTOR_SPACE, collection_space
    from app.core.memory import ShortTermMemory

    cache = BoundedCache(max_entries=3)
    for i in range(5):
        cache[f"k{i}"] = i
    check("LRU: размер не превышает лимит", len(cache) == 3)
    check("LRU: вытеснены самые давние ключи",
          cache.keys() == ["k2", "k3", "k4"] and cache.get("k0") is None)
    cache["k2"]  # обращение поднимает ключ
    cache["k5"] = 5
    check("LRU: обращение спасает ключ от вытеснения", "k2" in cache)
    check("dict-API: get/pop/setdefault/values",
          cache.get("нет", "дефолт") == "дефолт"
          and cache.pop("k5") == 5
          and cache.setdefault("k9", 9) == 9
          and 9 in cache.values())

    ttl_cache = BoundedCache(max_entries=10, ttl=0.15)
    ttl_cache["a"] = 1
    check("TTL: свежая запись читается", ttl_cache.get("a") == 1)
    time.sleep(0.25)
    check("TTL: просроченная запись исчезла",
          ttl_cache.get("a") is None and "a" not in ttl_cache and len(ttl_cache) == 0)

    errors = []

    def hammer(n):
        try:
            for i in range(200):
                cache[f"t{n}_{i}"] = i
                cache.get(f"t{n}_{i}")
                cache.keys()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    check("потокобезопасность: без исключений и без превышения лимита",
          not errors and len(cache) <= 3)

    # Буферы STM: ограничены и перечитываются из БД после вытеснения
    stm = ShortTermMemory(max_messages=10, db_path=str(tmp / "stm_db"),
                          context="memcore_stm")
    check(f"коллекция STM открыта с метрикой {VECTOR_SPACE}",
          collection_space(stm.collection) == VECTOR_SPACE)
    stm.buffers = BoundedCache(max_entries=2)
    stm.add_message("user", "сообщение чата A", user_id="u1", chat_id="chatA")
    stm.add_message("user", "сообщение чата B", user_id="u1", chat_id="chatB")
    stm.add_message("user", "сообщение чата C", user_id="u1", chat_id="chatC")
    check("буферы чатов ограничены лимитом", len(stm.buffers) <= 2)
    restored = stm.get_messages(chat_id="chatA")
    check("вытесненный буфер перечитан из ChromaDB (контекст чата не потерян)",
          [m["content"] for m in restored] == ["сообщение чата A"])

    # Легаси-записи без chat_id в метаданных (только user_id) — тоже поднимаются
    stm.collection.add(ids=["legacy_1"], documents=["легаси без chat_id"],
                       metadatas=[{"role": "user", "timestamp": 1, "user_id": "legacy_chat"}])
    check("подгрузка легаси-записей (метаданные без chat_id)",
          [m["content"] for m in stm.get_messages(chat_id="legacy_chat")]
          == ["легаси без chat_id"])

    # Конкурентные add_message в разные чаты — без гонок и потерь
    def writer(n):
        for i in range(10):
            stm.add_message("user", f"поток {n} сообщение {i}",
                            user_id=f"u{n}", chat_id=f"chat{n}")

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    counts = [len(stm.get_messages(chat_id=f"chat{n}")) for n in range(3)]
    check(f"конкурентная запись: все сообщения на месте {counts}",
          all(c == 10 for c in counts))


# ─── 5. FileVectorDB: один лок на инстанс ────────────────

def test_file_vector_db(tmp: Path):
    print("\n── FileVectorDB: лок и ограниченный кеш ──")
    from app.core.bounded_cache import BoundedCache
    from app.core.chroma_space import VECTOR_SPACE, collection_space
    from app.core.file_vector_db import FileVectorDB

    db = FileVectorDB(db_path=str(tmp / "files_db"), context="memcore_files", max_docs=3)
    check(f"коллекции файлов открыты с метрикой {VECTOR_SPACE}",
          collection_space(db.collection) == VECTOR_SPACE
          and collection_space(db.full_docs) == VECTOR_SPACE)
    check("_loaded_docs — ограниченный кеш, а не вечный dict",
          isinstance(db._loaded_docs, BoundedCache))

    errors = []

    def adder(n):
        try:
            for i in range(3):
                db.add_file("u1", f"файл_{n}_{i}.txt", f"содержимое {n}-{i} " * 30)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=adder, args=(n,)) for n in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    files = db.get_loaded_files("u1")
    check("конкурентные add_file: без исключений", not errors)
    check(f"конкурентные add_file: лимит max_docs соблюдён ({len(files)} файлов)",
          len(files) <= db.max_docs)
    # Полный текст последнего документа собирается целиком
    db.add_file("u1", "итог.txt", "первая часть. вторая часть. третья часть.")
    check("полный текст документа читается после конкурентных записей",
          db.get_full_document("u1", "итог.txt")
          == "первая часть. вторая часть. третья часть.")
    check("remove_file удаляет и чанки, и полный текст",
          db.remove_file("u1", "итог.txt")
          and db.get_full_document("u1", "итог.txt") is None)


# ─── 6. migrate_stm: порядок операций ────────────────────

def test_migrate_stm(tmp: Path):
    print("\n── migrate_stm: импорт → бэкап → замена ──")
    import chromadb
    import json as _json
    import migrate_stm as mig
    from app.core.chroma_space import COLLECTION_NAMES, open_collection

    db_path = tmp / "data" / "migr_ctx" / "stm"
    client = chromadb.PersistentClient(path=str(db_path))
    collection = open_collection(client, COLLECTION_NAMES["stm"],
                                embedding_function=mig.create_st_embedder())
    collection.add(ids=["old1", "old2"], documents=["старое одно", "старое два"],
                   metadatas=[{"role": "user", "timestamp": 1, "chat_id": "c1"},
                              {"role": "assistant", "timestamp": 2, "chat_id": "c1"}])

    # Валидация импорта — до любого удаления
    for name, payload in [
        ("пустой список", "[]"),
        ("битый JSON", "{не json"),
        ("не список", '{"a": 1}'),
        ("запись без текста", '[{"id": "a"}]'),
        ("дубли id", '[{"id":"a","document":"x"},{"id":"a","document":"y"}]'),
    ]:
        bad = tmp / "bad_import.json"
        bad.write_text(payload, encoding="utf-8")
        raised = False
        try:
            mig.load_import_file(str(bad))
        except ValueError:
            raised = True
        check(f"load_import_file отвергает импорт: {name}", raised)

    raised = False
    try:
        mig.load_import_file(str(tmp / "нет_такого_файла.json"))
    except ValueError:
        raised = True
    check("load_import_file отвергает отсутствующий файл", raised)

    # Главное: неудачный импорт НЕ трогает существующую STM
    bad = tmp / "bad_import.json"
    bad.write_text("[]", encoding="utf-8")
    raised = False
    try:
        mig.migrate_stm(db_path=str(db_path), import_file=str(bad))
    except ValueError:
        raised = True
    check("плохой импорт: миграция прервана, STM не тронута",
          raised and collection.count() == 2)

    # Успешный путь: бэкап появляется до замены, оба формата импорта понятны
    good = tmp / "good_import.json"
    good.write_text(_json.dumps([
        {"id": "new1", "document": "новое вложенное",
         "metadata": {"role": "user", "timestamp": 10, "chat_id": "c2",
                      "user_name": "Гость"}},
        {"chroma_id": "new2", "document": "новое плоское",
         "role": "assistant", "timestamp": 11, "chat_id": "c2"},
    ], ensure_ascii=False), encoding="utf-8")

    os.chdir(tmp)  # бэкап пишется в ./memory_export
    inserted, backup_path = mig.migrate_stm(db_path=str(db_path),
                                            import_file=str(good))
    check("успешная миграция: вставлены обе записи (оба формата)", inserted == 2)
    check("бэкап создан до замены и содержит старые записи",
          backup_path is not None and backup_path.is_file()
          and len(_json.loads(backup_path.read_text(encoding="utf-8"))["documents"]) == 2)
    check("бэкап лежит в формате memory_export (имя {контекст}_stm_*)",
          backup_path.name.startswith("migr_ctx_stm_"))
    rows = collection.get(include=["documents", "metadatas"])
    check("в коллекции только новые записи",
          sorted(rows["ids"]) == ["new1", "new2"])
    check("метаданные нормализованы (chat_id/role/user_name)",
          all(m["chat_id"] == "c2" for m in rows["metadatas"])
          and any(m.get("user_name") == "Гость" for m in rows["metadatas"]))

    # Бэкап читается штатным restore_memory
    from app.features.restore_memory import restore_collection
    restore_db = tmp / "data" / "restored_ctx" / "stm"
    count = restore_collection(str(restore_db), COLLECTION_NAMES["stm"],
                               str(backup_path))
    check("бэкап восстанавливается через restore_memory.restore_collection",
          count == 2)


# ─── 7. rich_message_formatter: экранирование кода ───────

def test_formatter():
    print("\n── rich_message_formatter: экранирование кода ──")
    from app.core.rich_message_formatter import RichMessageFormatter as F

    src = ("Пример:\n```python\nif a < b and c > d:\n    print(\"<b>жирный</b> & co\")\n```\n"
           "и инлайн `x <script>alert(1)</script> & y` в тексте")
    for name, out in (("markdown_to_rich_html", F.markdown_to_rich_html(src)),
                      ("to_current_html", F.to_current_html(src))):
        check(f"{name}: '<' в код-блоке экранирован",
              "if a &lt; b and c &gt; d:" in out)
        check(f"{name}: теги внутри код-блока не остались разметкой",
              "&lt;b&gt;жирный&lt;/b&gt;" in out and "<b>жирный</b>" not in out)
        check(f"{name}: '&' в код-блоке экранирован", "&amp; co" in out)
        check(f"{name}: инлайн-код экранирован",
              "&lt;script&gt;alert(1)&lt;/script&gt;" in out)
        check(f"{name}: обёртки кода на месте",
              "<pre><code class=\"language-python\">" in out and "<code>" in out)


# ─── 8. restore_memory: цели из каталога дампов ──────────

def test_restore_targets(tmp: Path):
    print("\n── restore_memory: цели вычисляются, а не прошиты ──")
    from app.core.chroma_space import COLLECTION_NAMES
    from app.core.config import Config
    from app.features import restore_memory as rm

    export_dir = tmp / "memory_export_test"
    export_dir.mkdir(parents=True, exist_ok=True)
    for name in ("api_arrodes_ltm_20260101_101010.json",
                 "api_arrodes_ltm_20260505_121212.json",
                 "connor_stm_20260101_101010.json",
                 "новая_персона_files_20260101_101010.json",
                 "..%2F..%2Fetc_stm_20260101_101010.json",
                 "мусор.json"):
        (export_dir / name).write_text("{}", encoding="utf-8")
    (export_dir / "../evil_stm_20260101_101010.json").write_text("{}", encoding="utf-8")

    targets = rm.find_latest_export(str(export_dir))
    check("дамп веб-персоны (api_*) больше не игнорируется",
          "api_arrodes_ltm" in targets)
    check("берётся самый свежий дамп по метке времени",
          targets["api_arrodes_ltm"][0].endswith("api_arrodes_ltm_20260505_121212.json"))
    check("новая персона не требует правки константы",
          "новая_персона_files" in targets)
    check("имя коллекции — из общей карты COLLECTION_NAMES",
          targets["connor_stm"][2] == COLLECTION_NAMES["stm"]
          and targets["api_arrodes_ltm"][2] == COLLECTION_NAMES["ltm"])
    check("путь к базе — из конфига (get_db_paths), а не из константы",
          targets["connor_stm"][1] == os.path.join(Config.DATA_DIR, "connor", "stm"))
    check("файлы не по схеме и подозрительные имена отброшены",
          all("evil" not in k and "etc" not in k for k in targets))

    filtered = rm.find_latest_export(str(export_dir), contexts=["connor"])
    check("фильтр по контекстам работает", list(filtered) == ["connor_stm"])
    check("нет каталога дампов — пустой результат",
          rm.find_latest_export(str(tmp / "нет_такого")) == {})


def main():
    tmp = Path(tempfile.mkdtemp(prefix="memory_core_smoke_"))
    os.chdir(tmp)                       # data/ и memory_export/ — внутри tmp
    from app.core.config import Config
    Config.DATA_DIR = str(tmp / "data")  # get_db_paths() тоже смотрит в tmp
    try:
        _shared_embedder()
        test_chroma_space(tmp)
        test_bounded_cache_and_stm(tmp)
        test_ltm_thresholds(tmp)
        test_file_vector_db(tmp)
        test_self_memory(tmp)
        test_migrate_stm(tmp)
        test_formatter()
        test_restore_targets(tmp)
    finally:
        os.chdir("/")
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИтого: {ok} проверок, {failures} провалов")


if __name__ == "__main__":
    main()
