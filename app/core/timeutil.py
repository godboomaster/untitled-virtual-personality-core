"""Единый источник «текущего времени пользователя» (задача №7 аудита).

Корень группы дефектов: каждый менеджер брал время сам —
``datetime.now()`` / ``date.today()`` / ``time.localtime()``, то есть
*системный* часовой пояс машины, на которой крутится процесс. Пока бот
запускался на ноутбуке пользователя, это совпадало с его временем; как только
процесс уезжает на сервер (или у машины UTC), «напомни завтра в 9» ставится на
9:00 чужого пояса, утреннее приветствие ритма приходит ночью, дневной лимит
инициатив обнуляется не в полночь, а посреди дня. Настройки часового пояса в
конфиге не было вовсе, и добавить её «в одном месте» было некуда.

Здесь это одно место. Пояс задаётся переменной окружения ``TIMEZONE``
(имя зоны IANA, например ``Europe/Moscow``); как фолбэк принимается
стандартная POSIX-переменная ``TZ``. Не задано или имя не распознано —
системный локальный пояс, то есть в точности прежнее поведение.

Договор (важно для персистентных состояний):

* :func:`now` / :func:`today` / :func:`from_ts` возвращают **naive**
  datetime/date — стенные часы пользователя, без tzinfo. Это сознательно:
  все менеджеры сравнивают время с naive-датами (прогноз Open-Meteo,
  ``datetime(...)``-конструкции, строки ``%Y-%m-%d``), и aware-датами это
  ломалось бы ``TypeError``-ами в неожиданных местах;
* переход «стенные часы ↔ epoch» идёт ТОЛЬКО через :func:`to_ts` /
  :func:`from_ts`. У naive-datetime ``.timestamp()`` трактует его как время
  системного пояса — при заданном TIMEZONE это молча даёт сдвиг на разницу
  поясов. Правило: получили момент из :func:`now` — в секунды его переводит
  :func:`to_ts`, и никогда ``dt.timestamp()``;
* обратная совместимость чтения: всё, что менеджеры хранят на диске, — это
  либо epoch-секунды (``trigger_at``, ``created_at``, ``since`` — они от
  пояса не зависят вовсе), либо строки-даты ``%Y-%m-%d`` для «раз в сутки»
  (утреннее приветствие, дневной лимит инициатив). Naive-datetime в файлах
  состояния не хранится нигде, поэтому старые записи читаются как раньше.
  Единственный видимый эффект смены TIMEZONE на уже накопленном состоянии —
  сдвиг границы суток у этих строк-дат (в худшем случае одно приветствие
  лишний раз или один пропуск), данные не портятся.

Использование:
    from app.core import timeutil
    now = timeutil.now()                  # вместо datetime.now()
    ts = timeutil.to_ts(now)              # вместо now.timestamp()
    dt = timeutil.from_ts(r["trigger_at"])  # вместо datetime.fromtimestamp(...)
"""

import logging
import os
from datetime import date, datetime, tzinfo
from typing import Optional

# Импорт ради побочного эффекта: config грузит .env/.env.config (load_dotenv),
# иначе TIMEZONE из .env не увидят модули, которые config сами не импортируют
# (env_context, rhythm_manager, proactive_messaging).
from app.core import config as _config  # noqa: F401

try:  # zoneinfo — stdlib с 3.9; на совсем старом питоне просто нет поясов
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

logger = logging.getLogger(__name__)

# Имя переменной окружения — основное и POSIX-фолбэк
ENV_VARS = ("TIMEZONE", "TZ")

# Кеш разбора имени пояса: (имя из окружения, объект tzinfo|None).
# Читаем окружение на каждый вызов (дешево) — веб-настройки/тесты меняют
# os.environ на живом процессе, и пересоздавать менеджеры для этого не надо.
_cache: tuple = ("", None)
_warned: set = set()


def tz_name() -> str:
    """Имя пояса из окружения ("" — не задано, значит системный локальный)."""
    for var in ENV_VARS:
        value = (os.getenv(var) or "").strip()
        if value:
            return value
    return ""


def tz() -> Optional[tzinfo]:
    """Пояс пользователя или None (None = системный локальный пояс)."""
    global _cache
    name = tz_name()
    if not name:
        return None
    cached_name, cached_tz = _cache
    if cached_name == name:
        return cached_tz
    zone = None
    if ZoneInfo is None:
        if "no-zoneinfo" not in _warned:
            _warned.add("no-zoneinfo")
            logger.warning("[timeutil] zoneinfo недоступен — TIMEZONE игнорируется, "
                           "время берётся по системному поясу")
    else:
        try:
            zone = ZoneInfo(name)
        except Exception as e:
            if name not in _warned:
                _warned.add(name)
                logger.warning(f"[timeutil] Неизвестный часовой пояс {name!r} ({e}) — "
                               "работаем по системному поясу")
            zone = None
    _cache = (name, zone)
    return zone


def now() -> datetime:
    """«Сейчас» по стенным часам пользователя (naive, см. договор в модуле)."""
    zone = tz()
    if zone is None:
        return datetime.now()
    return datetime.now(zone).replace(tzinfo=None)


def today() -> date:
    """Сегодняшняя дата по часам пользователя."""
    return now().date()


def from_ts(ts: float) -> datetime:
    """epoch-секунды → стенные часы пользователя (naive)."""
    zone = tz()
    if zone is None:
        return datetime.fromtimestamp(float(ts))
    return datetime.fromtimestamp(float(ts), zone).replace(tzinfo=None)


def to_ts(dt: datetime) -> float:
    """Стенные часы пользователя (naive, из :func:`now`/:func:`from_ts`) →
    epoch-секунды. Aware-datetime отдаёт свой момент как есть."""
    if dt.tzinfo is not None:
        return dt.timestamp()
    zone = tz()
    if zone is None:
        return dt.timestamp()
    return dt.replace(tzinfo=zone).timestamp()
