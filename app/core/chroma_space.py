"""Единая точка открытия коллекций Chroma — с ЯВНО заданной метрикой.

Корень группы дефектов (аудит, п.6): коллекции создавались через
``client.get_or_create_collection(name, embedding_function=...)`` без
``hnsw:space``, а Chroma по умолчанию берёт ``l2``. При этом пороги в коде
подобраны под cosine-distance (0 = идентично, 2 = противоположно):
``LongTermMemory.forget`` отбрасывал кандидата при ``distance > 1.0``, а
``update_fact`` — при ``> 0.3``. Эмбеддинги SentenceTransformer не
нормированы, поэтому l2-расстояния у них другого масштаба (единицы и
десятки) — оба порога не проходил ни один факт, и «забудь про X» / правка
факта из веб-UI молча не делали ничего. Плюс ранжирование поиска по l2 на
ненормированных векторах отличается от cosine, под который писался поиск.

Здесь метрика задаётся ОДИН раз (``VECTOR_SPACE``) и применяется ко всем
коллекциям проекта, а уже существующие коллекции (созданные до фикса с
l2) переносятся на cosine без потери данных:

  1. фактическая метрика читается из конфигурации коллекции на диске —
     ``get_or_create_collection`` с новыми metadata молча возвращает старую
     коллекцию со старой метрикой, проверять по переданным metadata нельзя;
  2. данные (ids + документы + метаданные + УЖЕ посчитанные эмбеддинги)
     копируются страницами во временную коллекцию с нужной метрикой —
     эмбеддинги переносятся как есть, модель заново не считает;
  3. количество записей сверяется, и только после этого старая коллекция
     удаляется, а временная переименовывается на её имя.

Обрыв процесса посреди переноса не теряет данные: на следующем открытии
``open_collection`` находит временную коллекцию и либо доводит перенос до
конца (старой уже нет / она пуста), либо выбрасывает недокопированный
остаток (старая на месте). Любая ошибка переноса — WARNING в лог и работа
со старой коллекцией: пользовательские факты важнее метрики.
"""

import logging

logger = logging.getLogger(__name__)

# Метрика для ВСЕХ векторных коллекций проекта. Под неё подобраны пороги
# схожести в memory.py (см. FORGET_MAX_DISTANCE / UPDATE_FACT_MAX_DISTANCE).
VECTOR_SPACE = "cosine"

# Имена коллекций по виду базы — одно определение на проект (memory.py,
# file_vector_db.py, restore_memory.py, migrate_stm*.py)
COLLECTION_NAMES = {
    "stm": "short_term_memory",
    "ltm": "long_term_memory",
    "files": "file_documents",
    "full_docs": "file_full_docs",
}

# Размер страницы при переносе: и get, и add у Chroma ограничены по объёму
# батча, а коллекции STM бывают на тысячи записей
_COPY_BATCH = 500

# Суффикс временной коллекции переноса. Имя коллекции Chroma — только
# [a-zA-Z0-9._-], начинается и заканчивается буквой/цифрой.
_MIG_SUFFIX = ".mig-"


def collection_space(collection) -> str:
    """Фактическая метрика коллекции — из её конфигурации на диске.

    Не из переданных при открытии metadata: у существующей коллекции они
    игнорируются, и проверка по ним всегда показывала бы желаемое, а не
    реальное.
    """
    try:
        cfg = getattr(collection, "configuration_json", None) or {}
        for key in ("hnsw", "spann"):
            section = cfg.get(key) or {}
            if section.get("space"):
                return section["space"]
    except Exception:
        pass
    meta = getattr(collection, "metadata", None) or {}
    return meta.get("hnsw:space") or "l2"


def _try_get(client, name: str, embedding_function=None):
    """Коллекция или None, если её нет (Chroma бросает разные типы ошибок
    в разных версиях — ловим широко)."""
    try:
        if embedding_function is not None:
            return client.get_collection(name, embedding_function=embedding_function)
        return client.get_collection(name)
    except Exception:
        return None


def _count(collection) -> int:
    try:
        return collection.count()
    except Exception:
        return 0


def _copy_all(src, dst) -> int:
    """Копирует все записи src в dst страницами. Возвращает скопированное."""
    total = _count(src)
    copied = 0
    offset = 0
    while offset < total:
        page = src.get(
            limit=_COPY_BATCH,
            offset=offset,
            include=["documents", "metadatas", "embeddings"],
        )
        ids = page.get("ids") or []
        if not ids:
            break
        embeddings = page.get("embeddings")
        # embeddings приходят numpy-массивом: len() и срезы работают,
        # но `or []` на массиве бросает ValueError — проверяем через is None
        dst.add(
            ids=ids,
            documents=page.get("documents"),
            metadatas=page.get("metadatas"),
            embeddings=embeddings if embeddings is not None else None,
        )
        copied += len(ids)
        offset += len(ids)
    return copied


def _recover_interrupted(client, name: str, tmp_name: str, embedding_function=None):
    """Доводит до конца или откатывает перенос, прерванный падением процесса."""
    tmp = _try_get(client, tmp_name, embedding_function)
    if tmp is None:
        return
    main = _try_get(client, name, embedding_function)
    if main is None or (_count(main) == 0 and _count(tmp) > 0):
        # Старой коллекции уже нет (или она пустая заготовка) — данные лежат
        # во временной, доводим переименование
        try:
            if main is not None:
                client.delete_collection(name)
            tmp.modify(name=name)
            logger.warning(f"[Chroma] Прерванный перенос метрики {name} "
                           f"достроен: {_count(tmp)} записей")
        except Exception as e:
            logger.error(f"[Chroma] Не удалось достроить перенос {name}: {e}")
        return
    # Старая коллекция на месте — временная это недокопированный остаток
    try:
        client.delete_collection(tmp_name)
        logger.warning(f"[Chroma] Недокопированный остаток {tmp_name} удалён, "
                       f"данные {name} не тронуты")
    except Exception as e:
        logger.warning(f"[Chroma] Не удалось удалить {tmp_name}: {e}")


def _migrate_space(client, existing, name: str, tmp_name: str,
                   embedding_function, space: str, actual: str):
    """Перенос коллекции на метрику space без потери данных."""
    if _count(existing) == 0:
        # Пустую проще пересоздать, чем копировать
        try:
            client.delete_collection(name)
            return client.create_collection(
                name, embedding_function=embedding_function,
                metadata={"hnsw:space": space})
        except Exception as e:
            logger.error(f"[Chroma] Не удалось пересоздать пустую {name}: {e}")
            return existing

    src_count = _count(existing)
    logger.warning(f"[Chroma] Коллекция {name}: метрика {actual} вместо {space} — "
                   f"переношу {src_count} записей (эмбеддинги копируются как есть)")
    try:
        # Остаток прошлой неудачной попытки (если _recover его не тронул)
        if _try_get(client, tmp_name, embedding_function) is not None:
            client.delete_collection(tmp_name)
        tmp = client.create_collection(
            tmp_name, embedding_function=embedding_function,
            metadata={"hnsw:space": space})
        copied = _copy_all(existing, tmp)
        if copied < src_count or _count(tmp) < src_count:
            raise RuntimeError(
                f"скопировано {copied}/{src_count} — перенос отменён")
        # Только теперь можно расстаться со старой коллекцией
        client.delete_collection(name)
        tmp.modify(name=name)
        logger.info(f"[Chroma] Коллекция {name} переведена на {space} "
                    f"({copied} записей)")
        return _try_get(client, name, embedding_function) or tmp
    except Exception as e:
        logger.error(f"[Chroma] Перенос {name} на {space} не удался ({e}) — "
                     f"работаю со старой коллекцией ({actual}), данные целы")
        try:
            if _try_get(client, name, embedding_function) is not None:
                client.delete_collection(tmp_name)
        except Exception:
            pass
        return _try_get(client, name, embedding_function) or existing


def open_collection(client, name: str, embedding_function=None,
                    space: str = VECTOR_SPACE):
    """Открыть (или создать) коллекцию с явной метрикой ``space``.

    Замена ``client.get_or_create_collection(...)`` во всём проекте: новая
    коллекция создаётся сразу с нужной метрикой, уже существующая с чужой —
    переносится (см. модульный docstring).
    """
    tmp_name = f"{name}{_MIG_SUFFIX}{space}"
    _recover_interrupted(client, name, tmp_name, embedding_function)

    existing = _try_get(client, name, embedding_function)
    if existing is None:
        try:
            return client.create_collection(
                name, embedding_function=embedding_function,
                metadata={"hnsw:space": space})
        except Exception:
            # Кто-то создал коллекцию между get и create — берём её
            existing = _try_get(client, name, embedding_function)
            if existing is None:
                raise

    actual = collection_space(existing)
    if actual == space:
        return existing
    return _migrate_space(client, existing, name, tmp_name,
                          embedding_function, space, actual)
