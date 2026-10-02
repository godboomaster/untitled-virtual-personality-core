"""Общие защитные примитивы API-слоя.

Строки из тела/query запроса (имя персоны в первую очередь) не должны идти
в Path(...) без проверки формата: ``persona="../../persona_template"``
превратил бы ``_PERSONAS_DIR / f"{persona}.yaml"`` в путь вне app/personas/,
а ``context=f"api_{persona}"`` тем же способом увёл бы BotInstance вне
data/.

Здесь — единственное место, где определён допустимый формат id персоны
(и вообще «безопасного» id вроде черновика персоны), плюс safe_join —
защита в глубину поверх regex на случай путей, которые придут в обход него.
Плюс безопасная запись .env (см. persist_env/remove_env).
"""

import json
import re
import threading
from pathlib import Path
from typing import Annotated

from fastapi import Path as ApiPath
from fastapi import Query as ApiQuery
from pydantic import AfterValidator

# Единственная реализация атомарной записи — app/core/atomic_io.py; реэкспорт
# сохраняет рабочими импорты `from app.api.security import atomic_write_text`
# (settings_api и др.).
from app.core.atomic_io import atomic_write_text  # noqa: F401

# id персоны = имя YAML-файла в app/personas/ (и черновика в data/persona_drafts/,
# формат тот же): латиница, цифры, "_", "-", 1..64 символов. "/" и ".." в
# алфавит не входят — traversal-строка просто не пройдёт regex.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PERSONA_ID_RE = SAFE_ID_RE  # алиас: так понятнее в местах про персон конкретно


def is_safe_id(value) -> bool:
    return isinstance(value, str) and bool(SAFE_ID_RE.match(value))


def _check_safe_id(value: str) -> str:
    if not is_safe_id(value):
        raise ValueError(
            "недопустимый id: разрешены латиница, цифры, «_» и «-» (1-64 символов)"
        )
    return value


# ── Аннотации для pydantic-схем и FastAPI path/query-параметров ────────
# Annotated с pattern защищает эндпоинт ДО того, как строка попадёт в
# бизнес-логику: невалидное имя → 422, а не 404/500 где-то внутри. Новый
# эндпоинт, использующий эти типы вместо голого str, получает защиту
# автоматически — не нужно помнить регекс на каждом месте.
SafeId = Annotated[str, AfterValidator(_check_safe_id)]
PersonaId = SafeId

PersonaIdPath = Annotated[
    str, ApiPath(pattern=SAFE_ID_RE.pattern, description="id персоны (имя YAML-файла)")
]
PersonaIdQuery = Annotated[
    str, ApiQuery(pattern=SAFE_ID_RE.pattern, description="id персоны (имя YAML-файла)")
]


def safe_join(base: Path, name: str, suffix: str = "", prefix: str = "") -> Path | None:
    """``base / f"{prefix}{name}{suffix}"``, только если результат не выходит
    за пределы ``base``. None — имя не прошло формат ИЛИ (запасной рубеж)
    итоговый путь после resolve() всё равно оказался снаружи base (символьные
    ссылки, нестандартный base и т.п.) — так путь физически не может
    указывать наружу, даже если regex-проверку выше по стеку забыли сделать
    или обойти.

    Валидируется ИМЕННО ``name`` (то, что реально пришло из запроса) — префикс
    вроде "api_" (контекст персоны в data/) не в счёт лимита длины имени,
    иначе персона у самой границы 64 символов ложно бы не проходила.
    """
    if not is_safe_id(name):
        return None
    base_r = base.resolve()
    candidate = base / f"{prefix}{name}{suffix}"
    try:
        candidate_r = candidate.resolve()
        candidate_r.relative_to(base_r)
    except (ValueError, OSError):
        return None
    return candidate


# ── Безопасная запись .env ──────────────────────────────────────────────
# python-dotenv (set_key/unset_key) уже пишет атомарно (tmp-файл в той же
# директории + os.replace, права исходного файла сохраняются) и корректно
# квотирует значение — перенос строки внутри значения остаётся ВНУТРИ
# кавычек и не порождает вторую строку KEY=... при обратном чтении. Здесь
# добавляем: проверку имени переменной, отказ на \r/\n/NUL (даже с учётом
# безопасного квотирования — лишняя строка в .env не должна появляться и
# по форме, не только по факту парсинга) и общий лок против гонки конкурентных
# запросов API, которые пишут в один файл.
_env_lock = threading.Lock()

ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def validate_env_value(value: str) -> None:
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise ValueError("значение не может содержать переносы строк или NUL-байт")


def persist_env(path: Path, var: str, value: str) -> None:
    # Записать/обновить переменную в .env атомарно и под локом.
    if not ENV_VAR_RE.match(var):
        raise ValueError(f"недопустимое имя переменной окружения: {var!r}")
    validate_env_value(value)
    from dotenv import set_key
    with _env_lock:
        set_key(str(path), var, value, quote_mode="always")


def remove_env(path: Path, var: str) -> None:
    # Удалить переменную из .env атомарно и под локом (нет файла/ключа — no-op).
    if not ENV_VAR_RE.match(var):
        raise ValueError(f"недопустимое имя переменной окружения: {var!r}")
    from dotenv import unset_key
    with _env_lock:
        if path.exists():
            unset_key(str(path), var)


# ── Общий лок для read-modify-write YAML персон ────────────────────────
# YAML персоны правится в settings_api по схеме read → merge → write
# (safe_load всего файла, правка словаря, safe_dump обратно) — без лока
# конкурентные запросы (например, автосохранение формы) чередуют чтение и
# запись и теряют чужие правки. Атомарность самой записи — write_text во
# temp-файл + os.replace, лок только сериализует read-modify-write целиком.
yaml_write_lock = threading.Lock()


# ── Безопасный path-сегмент из произвольной строки (chat_id, user_id...) ──
# id персоны идёт через SAFE_ID_RE (строгий формат, отказ на невалидном
# вводе — обосновано: имя персоны выбирает сам проект). chat_id/user_id —
# формат диктует Telegram/фронтенд, не наш код (например, у групп он
# отрицательный: "-1001234567890"), поэтому здесь не отказ, а приведение к
# безопасному виду: любой символ вне "буква/цифра (в т.ч. юникод)/_/-"
# заменяется на "_". Traversal невозможен по построению — "/", "\\", ".."
# в это множество не входят и превращаются в "_", какой бы алфавит ни был.
_UNSAFE_SEGMENT_RE = re.compile(r"[^\w-]+", re.UNICODE)


def safe_segment(value) -> str:
    s = _UNSAFE_SEGMENT_RE.sub("_", str(value))
    s = s[:128]
    return s or "_"


# ── Лимит размера тела запроса ──
# FastAPI читает и разбирает тело целиком ДО зависимостей и валидации
# полей: лимиты вроде «3 МБ на файл скина» срабатывают, когда многомегабайтный
# JSON уже в памяти и распарсен. BodySizeLimit — ASGI-middleware для
# выбранных маршрутов, отказывает раньше: по Content-Length — сразу, не
# читая тела; тело без длины (chunked) или с заниженной длиной читается с
# потолком и при превышении обрывается тем же 413. Уложившееся тело
# отдаётся приложению из буфера как обычно.

class BodySizeLimit:
    """rules — [(метод, regex полного пути, лимит в байтах)]: первое
    совпадение задаёт лимит; остальные запросы проходят без изменений."""

    def __init__(self, app, rules):
        self.app = app
        self.rules = [(m.upper(), re.compile(p), int(n)) for m, p, n in rules]

    def _limit(self, method: str, path: str) -> int | None:
        for m, pattern, limit in self.rules:
            if m == method and pattern.fullmatch(path):
                return limit
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self._limit(scope.get("method", ""), scope.get("path", ""))
        if limit is None:
            return await self.app(scope, receive, send)

        declared = None
        for name, value in scope.get("headers") or ():
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    return await _reply(send, 400, "Некорректный Content-Length")
                break
        if declared is not None and declared > limit:
            return await _reply(send, 413, _too_large(limit))

        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return  # клиент ушёл, не дослав тело — отвечать некому
            body = message.get("body", b"")
            total += len(body)
            if total > limit:
                return await _reply(send, 413, _too_large(limit))
            chunks.append(body)
            if not message.get("more_body"):
                break

        buffered = {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        replayed = False

        async def replay():
            # Тело — одним сообщением; дальше — настоящий receive (разрыв
            # соединения для StreamingResponse/SSE)
            nonlocal replayed
            if not replayed:
                replayed = True
                return buffered
            return await receive()

        await self.app(scope, replay, send)


# ── Только свой фронт ──
# API слушает 127.0.0.1, но до него достаёт JavaScript любой страницы,
# открытой в браузере на этой машине, в том числе в браузере бота (выдача
# поиска, магазины агента задач). Две проверки:
#  - Origin. Браузер ставит его на кросс-доменные запросы и на любые не-GET;
#    такой запрос принимается, только если страница — свой фронт. Одного CORS
#    мало: он лишь не даёт прочитать ответ, а «простые» запросы (POST без
#    тела, multipart) сервер всё равно исполнил бы;
#  - Host — от DNS rebinding: домен атакующего указывает на 127.0.0.1, и для
#    браузера это «тот же сайт», Origin свой. Принимаются только известные
#    имена хоста.
# Запросы без Origin (curl, скрипты, TestClient) проходят: локальный процесс
# и так может всё.

# Якоря — на случай Starlette, где allow_origin_regex проверяется через match
LOOPBACK_ORIGIN_RE = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def host_without_port(value: str) -> str:
    v = value.strip().lower()
    if v.startswith("["):  # IPv6: [::1]:8000
        return v[1:v.find("]")] if "]" in v else v
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


class LocalOriginGuard:
    """origins — разрешённые Origin ("*" — любой), origin_regex — шаблон
    разрешённых Origin, hosts — разрешённые имена в заголовке Host
    (None — Host не проверяется)."""

    def __init__(self, app, origins=(), origin_regex=None, hosts=None):
        self.app = app
        self.any_origin = "*" in origins
        self.origins = {o.strip().rstrip("/").lower() for o in origins}
        self.origin_re = re.compile(origin_regex) if origin_regex else None
        self.hosts = {h.lower() for h in hosts} if hosts is not None else None

    def origin_allowed(self, origin: str) -> bool:
        if self.any_origin:
            return True
        o = origin.strip().rstrip("/").lower()
        return o in self.origins or bool(self.origin_re and self.origin_re.fullmatch(o))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v.decode("latin-1") for k, v in scope.get("headers") or ()}
        if self.hosts is not None:
            host = host_without_port(headers.get(b"host", ""))
            if host not in self.hosts:
                return await _reply(send, 400, f"Host {host!r} не разрешён (API_ALLOWED_HOSTS)")
        origin = headers.get(b"origin")
        if origin is not None and not self.origin_allowed(origin):
            return await _reply(send, 403, f"Источник {origin!r} не разрешён (API_CORS_ORIGINS)")
        await self.app(scope, receive, send)


def _too_large(limit: int) -> str:
    return f"Тело запроса больше {limit / 1024 / 1024:.0f} МБ"


async def _reply(send, status: int, detail: str) -> None:
    # Ответ в формате HTTPException FastAPI: {"detail": ...}
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
        (b"connection", b"close"),
    ]})
    await send({"type": "http.response.body", "body": body})
