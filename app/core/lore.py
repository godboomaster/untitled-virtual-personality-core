"""Лор персоны: история и мир отдельным файлом, в промпт — только нужные куски.

Весь лор в system_prompt стоит тысячи токенов на каждый ход и не влезает в
лимиты веб-чатов. Здесь он лежит отдельно (data/api_<id>/lore/ — папка едет со
сменой id и уходит в архив вместе с памятью, app/api/settings_api.py), а на
каждую реплику находятся до LIMIT коротких кусков. LLM на ходу не вызывается.

Поиск — из замера на эмбеддере ядра (23 вопроса, лор 3–130 тыс. знаков): одних
векторов хватает только на маленьком лоре, поэтому:
  - слова (BM25) рядом с векторами: находят редкое — имена, предметы;
  - имя персонажа в запросе: лор написан в третьем лице, спрашивают «у тебя»;
  - прошлые реплики: короткое «а тело нашли?» само по себе ни о чём;
  - условие на реплику: порог по сходству не отсекает «как приготовить борщ?»,
    поэтому реплика должна быть к персонажу или содержать редкое слово из лора.

Нарезка — по разделам и абзацам, кусок до CHUNK_CHARS: модель ядра читает 128
токенов (около 400 знаков русского текста), дальше кусок для неё не существует.

Хранение — обычные файлы, не Chroma: поиску по словам и так нужны все куски в
памяти, полный перебор на тысячах кусков мгновенный, а смена id — перенос папки.
Индекс index.json ссылается на файл векторов vectors.<поколение>.npy: новое
поколение становится видно разом с записью index.json, половинчатого индекса
не бывает. Читателю хватает stat файла на ход — правка из веба подхватывается
без связи с ботом, в том числе из другого процесса.

Разбор слов (terms) — тот же, что в web/src/offline/recall.ts: телефон без ядра
ищет по лору так же (сверка — scripts/test_lore.py).
"""

import hashlib
import logging
import math
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

from app.core.atomic_io import atomic_write_json, atomic_write_text, load_json_safe
from app.core.paths import data_dir
from app.core.st_embedder import ST_MODEL_NAME, create_st_embedder

logger = logging.getLogger(__name__)

INDEX_VERSION = 1

CHUNK_CHARS = 350       # кусок вместе с заголовком раздела
SECTION_CHARS = 60      # заголовок в векторе и в промпте
PLAIN_HEAD_WORDS = 5    # заголовок без разметки — не длиннее
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_CHARS = 2_000_000     # весь лор персоны: дальше это уже не лор, а книга
# Документы с текстом; картинки и таблицы лором не бывают
EXTENSIONS = frozenset({"txt", "md", "docx", "doc", "odt", "rtf", "pdf", "html", "epub"})

LIMIT = 10          # кусков на ход
LIGHT_LIMIT = 3     # слабая модель тонет в большом промпте
WEAK_LIMIT = 5      # ничего явного — только лучшие, с пометкой «может быть мимо»
RRF_K = 20
CONTEXT_WEIGHT = 0.6    # списки по запросу с прошлыми репликами — вес в слиянии
# Косинус с запросом без подставленного имени: с именем запрос близок всему
# лору. Лучший кусок сам по себе ничего не решает (нужное 0.29–0.80, посторонние
# лучшие 0.29–0.57), поэтому границы две, и обе — вместе со словами реплики:
STRONG_SIM = 0.50   # кусок так близок, что это находка и без общих слов
WORD_SIM = 0.35     # кусок с редким словом запроса близок и по смыслу: нужное
                    # от 0.35, посторонние («завтра», «вечером») до 0.33
SPREAD_SIM = 0.15   # кусок без редкого слова — не дальше этого от лучшего
RARE_SHARE = 0.015  # слово редкое: есть не больше чем в такой доле кусков
RARE_MIN = 3        # …или в стольких: на маленьком лоре доля бессмысленна
RARE_CAP = 15       # …но не больше: в томе на 4000 кусков и «погода» стала бы редкой
TAIL_CHARS = 200    # конец ответа персоны в запросе: на него ссылается «а кто это?»

K1 = 1.2
B = 0.75

HEAD = "What you remember about your own past and world, found for this message:"
WEAK_HEAD = ("What you remember about your own past and world. "
             "This may be unrelated to the message, so ignore it if it does not fit:")
RULE = (
    "These are your own memories, not a document: never mention excerpts, files or lore. "
    "Use them only when they fit the conversation, in your own voice, without retelling "
    "them unprompted."
)
GAP_RULE = (
    "Your history is longer than what you recall right now. If asked about your past and "
    "the detail is in neither your description nor these memories, stay vague or deflect "
    "in character. Never invent names, dates, places or events from your past."
)

# Служебные слова — как STOP в web/src/offline/recall.ts
_STOP = frozenset((
    "и в во не что он на я с со как а то все всё она так его но да ты к у же вы за бы по только ее её мне было вот от "
    "меня еще ещё нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь "
    "там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже "
    "себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее неё "
    "сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про "
    "всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более "
    "всегда конечно всю между это мне тебе ему мой твой твоя твои мои наш ваш привет пока спасибо давай ладно ок окей "
    "просто очень вообще знаешь помнишь скажи расскажи думаешь хочу можешь как-то что-то кто-то "
    "мою моё мое мои моих моим твою твоё твое свой свою своё свое свои меня мной тобой нам вами "
    "the a an and or but if then so to of in on at by for with about from as is are was were be been being it its "
    "this that these those i you he she we they me him her us them my your his our their do does did have has had "
    "what which who whom how when where why can could will would should just not no yes ok okay hi hello thanks please "
    "tell remember know think"
).split())

# Окончания — длинные раньше коротких, как ENDINGS в recall.ts
_ENDINGS = sorted((
    "иями ями ами ого его ому ему ыми ими ешь ете ишь ите ать ять ить еть уть ала ила ела али или ели ует уют "
    "ая яя ое ее ой ей ый ий ых их ом ем ам ям ах ях ов ев ью ия ие ют ут ят ат ет ит аю яю ую ал ил ел "
    "а я у ю о е ы и ь й ing ed es s"
).split(), key=len, reverse=True)

_WORD_RE = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)

# Обращение к персонажу: в векторном запросе — его имя (модель не знает, кто
# такой «ты», а лор написан о нём по имени)
_YOU_RE = re.compile(
    r"\b(?:у\s+тебя|с\s+тобой|ты|тебя|тебе|тобой|твой|твоя|твоё|твое|твои|твою|твоего|твоей|твоих|твоим|"
    r"you|your|yours)\b", re.IGNORECASE)
# Реплика к нему без «ты»: «почему не пьёшь кофе?», «расскажи о себе»
_YOU_VERB_RE = re.compile(r"\b(?:[а-яё]{2,}(?:ешь|ёшь|ишь)(?:ся)?|о\s+себе|про\s+себя)\b", re.IGNORECASE)
# Продолжение: «она ещё жива?», «а тело нашли?» — о том, что было в прошлой реплике
_THEM_RE = re.compile(r"\b(?:он|она|они|оно|его|её|ее|их|ему|ей|им|ним|ней|них|it|he|she|they|him|her|them)\b"
                      r"|^(?:а|и|но|and|but|so)\s", re.IGNORECASE)


def stem_word(word: str) -> str:
    # Основа слова — как stemWord в recall.ts: срез окончания, не длиннее 6 букв
    w = word.lower().replace("ё", "е")
    for e in _ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 3:
            w = w[:-len(e)]
            break
    return w[:6]


def terms(text: str) -> list[str]:
    # Слова темы: без служебных и коротких, по основам — как terms в recall.ts
    out = []
    for m in _WORD_RE.finditer((text or "").lower().replace("ё", "е")):
        w = m.group(0)
        if w in _STOP:
            continue
        if len(w) < (2 if w.isdigit() else 3):
            continue
        out.append(stem_word(w))
    return out


# ── Нарезка ──

_MD_HEAD_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_BOLD_HEAD_RE = re.compile(r"^\s*(?:\*\*|__)(.+?)(?:\*\*|__)\s*:?\s*$")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE_RE = re.compile(r"(?<=[,;:—])\s+")
_INVISIBLE_RE = re.compile(r"[\u200b-\u200d\u2060\ufeff\u00ad]")


def clean_text(text: str) -> str:
    # Единый вид текста: нормальная форма Юникода, переводы строк, пробелы
    text = unicodedata.normalize("NFC", text or "")
    text = _INVISIBLE_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _IMAGE_RE.sub("", text).replace("\u00a0", " ").replace("\t", " ")
    return "\n".join(re.sub(r" {2,}", " ", ln).strip() for ln in text.split("\n")).strip()


def _plain_heading(lines: list[str], i: int) -> bool:
    # Заголовок без разметки (txt): короткая строка без знаков конца фразы,
    # отделённая от текста. Не заголовки: пары «Возраст: 31», реплики диалога и
    # слова автора перед ними («Почти через минуту он спросил:») — в книгах из
    # docx каждая строка отделена пустой, поэтому длина здесь важнее отступов
    line = lines[i]
    words = len(line.split())
    if not line or len(line) > SECTION_CHARS or words > PLAIN_HEAD_WORDS:
        return False
    if line[-1] in ".!?…,;»\"" or not (line[0].isupper() or line[0].isdigit()):
        return False
    if line[-1] == ":" and words > 2:
        return False
    if re.search(r":\s+\S", line):
        return False
    prev_blank = i == 0 or not lines[i - 1]
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    if i + 1 >= len(lines):
        return False
    return not nxt or (prev_blank and len(nxt) > 80)


def _heading(lines: list[str], i: int) -> Optional[tuple[int, str]]:
    # (уровень, текст), если строка — заголовок
    line = lines[i]
    m = _MD_HEAD_RE.match(line)
    if m:
        return len(m.group(1)), m.group(2).strip(" *_:")
    m = _BOLD_HEAD_RE.match(line)
    if m and len(m.group(1)) <= SECTION_CHARS:
        return 4, m.group(1).strip(" *_:")
    if _plain_heading(lines, i):
        return 4, line.rstrip(" :")
    return None


def _split_long(text: str, limit: int) -> list[str]:
    # Абзац длиннее куска: по предложениям, длинное предложение — по частям
    # фразы, слово длиннее куска — как есть по границе
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    for regex in (_SENTENCE_RE, _CLAUSE_RE):
        parts = regex.split(text)
        if len(parts) > 1:
            for part in parts:
                out.extend(_split_long(part, limit))
            return _pack(out, limit)
    words = text.split(" ")
    if len(words) > 1:
        return _pack([piece for w in words for piece in _split_long(w, limit)], limit)
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def _pack(parts: Iterable[str], limit: int) -> list[str]:
    # Части подряд — в куски не длиннее limit
    out: list[str] = []
    cur = ""
    for part in parts:
        if cur and len(cur) + 1 + len(part) > limit:
            out.append(cur)
            cur = part
        else:
            cur = f"{cur} {part}" if cur else part
    if cur:
        out.append(cur)
    return out


def chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[dict]:
    # Текст → куски {"s": раздел, "t": текст}. Заголовки вложенные: в разделе —
    # два нижних уровня («Биография / Детство»)
    lines = clean_text(text).split("\n")
    stack: list[tuple[int, str]] = []
    section = ""
    paragraphs: list[str] = []
    out: list[dict] = []

    def flush():
        if not paragraphs:
            return
        room = max(120, limit - len(section) - 2)
        parts = [p for para in paragraphs for p in _split_long(para, room)]
        out.extend({"s": section, "t": piece} for piece in _pack(parts, room))
        paragraphs.clear()

    for i, line in enumerate(lines):
        if not line:
            continue
        head = _heading(lines, i)
        if head:
            flush()
            level, title = head
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            section = " / ".join(t for _, t in stack[-2:])[:SECTION_CHARS]
            continue
        paragraphs.append(line)
    flush()
    return out


def outline(text: str) -> list[dict]:
    """Текст для просмотра: заголовки — так, как их увидела нарезка
    ({"h": уровень, "t": текст}), остальное — абзацы ({"t": текст})."""
    lines = clean_text(text).split("\n")
    out: list[dict] = []
    for i, line in enumerate(lines):
        if not line:
            continue
        head = _heading(lines, i)
        out.append({"h": head[0], "t": head[1]} if head else {"t": line})
    return out


def embed_text(chunk: dict) -> str:
    # Что видит модель векторов: раздел тоже несёт смысл («Детство»)
    return f"{chunk['s']}: {chunk['t']}" if chunk.get("s") else chunk["t"]


# ── Индекс ──

class LoreError(Exception):
    # Отказ, который можно показать пользователю (загрузка в вебе)
    pass


@dataclass(frozen=True)
class _Snapshot:
    # Неизменяемый срез индекса: поиск берёт ссылку и работает без лока
    stamp: tuple
    files: tuple
    chunks: tuple
    vectors: np.ndarray             # N×D, нормированные
    postings: dict                  # основа → [(кусок, сколько раз)]
    doc_len: np.ndarray
    avg_len: float


_EMPTY = _Snapshot(None, (), (), np.zeros((0, 0), dtype=np.float32), {}, np.zeros(0), 1.0)


def _build_snapshot(stamp, files, chunks, vectors) -> _Snapshot:
    postings: dict = {}
    lens = np.zeros(len(chunks), dtype=np.float32)
    for i, chunk in enumerate(chunks):
        ts = terms(embed_text(chunk))
        lens[i] = len(ts)
        tf: dict = {}
        for t in ts:
            tf[t] = tf.get(t, 0) + 1
        for t, n in tf.items():
            postings.setdefault(t, []).append((i, n))
    return _Snapshot(stamp, tuple(files), tuple(chunks), vectors, postings, lens,
                     float(lens.mean()) if len(lens) and lens.mean() else 1.0)


@dataclass
class LoreHit:
    index: int              # номер куска в индексе (порядок текста)
    file: str
    section: str
    text: str
    similarity: float       # косинус с запросом
    words: bool             # есть редкое слово запроса


@dataclass
class LoreResult:
    hits: list = field(default_factory=list)
    weak: bool = False              # явных находок нет, это лучшее из слабых
    skipped: Optional[str] = None   # почему не искали: "smalltalk", "off_topic", "no_lore"


def _default_embed(texts: list[str]) -> np.ndarray:
    fn = _embedder()
    return np.asarray(fn(list(texts)), dtype=np.float32)


_embedder_lock = threading.Lock()
_embedder_fn = None


def _embedder():
    # Одна на процесс: у Chroma модель и так общая, здесь — без лишнего разбора
    global _embedder_fn
    with _embedder_lock:
        if _embedder_fn is None:
            _embedder_fn = create_st_embedder()
        return _embedder_fn


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return (vectors / np.maximum(norms, 1e-9)).astype(np.float32)


def _file_key(name: str) -> str:
    # Имя источника на диске: имя файла приходит от пользователя, а здесь нужен
    # только устойчивый ключ — так оно не может указать наружу папки
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]


def clean_filename(name: str) -> str:
    # Только имя, без пути (браузер шлёт и C:\fakepath\...); форма NFC —
    # macOS отдаёт имена в NFD, и один файл выглядел бы двумя
    name = unicodedata.normalize("NFC", str(name or "")).replace("\\", "/")
    return name.rsplit("/", 1)[-1].strip()[:200] or "lore.txt"


def file_ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


class LoreStore:
    """Лор одной персоны. Запись — по одной (write_lock), поиск идёт по
    неизменяемому срезу и записи не ждёт: индексация большого файла не держит
    ответ персоны."""

    def __init__(self, root: Path, embed: Optional[Callable[[list], np.ndarray]] = None):
        self.root = Path(root)
        self._embed_fn = embed
        self._snap = _EMPTY
        self._load_lock = threading.Lock()
        self._write_lock = threading.Lock()

    def _embed(self, texts: list) -> np.ndarray:
        return (self._embed_fn or _default_embed)(texts)

    # Пути
    @property
    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _source_path(self, name: str) -> Path:
        return self.root / "sources" / f"{_file_key(name)}.txt"

    def _original_path(self, name: str) -> Path:
        # Сам загруженный файл (pdf, docx…) — посмотреть его во вкладке «Лор»
        ext = file_ext(name)
        return self.root / "sources" / f"{_file_key(name)}.orig{'.' + ext if ext else ''}"

    def _drop_originals(self, name: str) -> None:
        for old in (self.root / "sources").glob(f"{_file_key(name)}.orig*"):
            old.unlink(missing_ok=True)

    # Чтение
    def _stat(self) -> Optional[tuple]:
        try:
            st = os.stat(self._index_path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def snapshot(self) -> _Snapshot:
        # Актуальный срез: файл на диске — источник правды, stat на каждый ход
        stamp = self._stat()
        if stamp == self._snap.stamp:
            return self._snap
        if stamp is None:
            self._snap = _EMPTY
            return _EMPTY
        with self._load_lock:
            if self._stat() == self._snap.stamp:
                return self._snap
            self._snap = self._read(stamp)
            return self._snap

    def _read(self, stamp) -> _Snapshot:
        data = load_json_safe(self._index_path, default=None, label="Lore")
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            return _EMPTY
        chunks = data.get("chunks") or []
        try:
            vectors = np.load(self.root / data["vectors"])
        except (OSError, KeyError, ValueError) as e:
            logger.warning(f"[Lore] Векторы не прочитаны ({self.root}): {e}")
            return _EMPTY
        if len(vectors) != len(chunks):
            logger.warning(f"[Lore] Индекс не сходится ({self.root}): "
                           f"{len(chunks)} кусков, {len(vectors)} векторов")
            return _EMPTY
        return _build_snapshot(stamp, data.get("files") or [], chunks, vectors)

    def has_lore(self) -> bool:
        return bool(self.snapshot().chunks)

    def stamp(self) -> str:
        # Метка версии лора: у телефона та же — забирать заново незачем
        snap = self.snapshot()
        return f"{snap.stamp[0]}-{snap.stamp[1]}" if snap.chunks else ""

    def export(self) -> dict:
        # Лор телефону: сам поиск там тот же (web/src/offline/lore.ts)
        snap = self.snapshot()
        return {"stamp": self.stamp(), "chunks": list(snap.chunks), "vectors": snap.vectors}

    def info(self) -> dict:
        # Для вкладки «Лор»: файлы и разделы
        snap = self.snapshot()
        sections: dict = {}
        for chunk in snap.chunks:
            per_file = sections.setdefault(chunk["f"], {})
            title = chunk.get("s") or ""
            per_file[title] = per_file.get(title, 0) + 1
        files = [{**f, "sections": [{"title": t, "chunks": n}
                                    for t, n in sections.get(f["name"], {}).items()]}
                 for f in snap.files]
        return {"files": files, "chars": sum(f["chars"] for f in snap.files),
                "chunks": len(snap.chunks)}

    # Запись
    def _commit(self, files: list, chunks: list, vectors: np.ndarray) -> None:
        # Сначала векторы новым поколением, затем index.json — он и делает их
        # видимыми. Старое поколение уходит только после этого
        self.root.mkdir(parents=True, exist_ok=True)
        gen = f"vectors.{time.time_ns()}.npy"
        tmp = self.root / f".{gen}.tmp"
        with open(tmp, "wb") as f:
            np.save(f, vectors)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.root / gen)
        atomic_write_json(self._index_path, {
            "version": INDEX_VERSION, "model": ST_MODEL_NAME, "dim": int(vectors.shape[1]) if vectors.size else 0,
            "vectors": gen, "files": files, "chunks": chunks,
        })
        for old in self.root.glob("vectors.*.npy"):
            if old.name != gen:
                try:
                    old.unlink()
                except OSError:
                    pass

    def _current(self) -> tuple[list, list, np.ndarray]:
        snap = self.snapshot()
        return list(snap.files), list(snap.chunks), snap.vectors

    def add_file(self, filename: str, text: str, original: Optional[bytes] = None) -> dict:
        """Добавить файл или заменить файл с тем же именем. Источник остаётся
        на диске — по нему можно пересчитать векторы другой моделью; original —
        сам загруженный файл, чтобы его можно было открыть из вкладки «Лор»."""
        name = clean_filename(filename)
        body = clean_text(text)
        chunks = chunk_text(body)
        if not chunks:
            raise LoreError("В файле не нашлось текста")
        with self._write_lock:
            files, old_chunks, old_vectors = self._current()
            other = sum(f["chars"] for f in files if f["name"] != name)
            if other + len(body) > MAX_TOTAL_CHARS:
                raise LoreError(f"Лор персоны не больше {MAX_TOTAL_CHARS // 1000} тыс. знаков")
            for chunk in chunks:
                chunk["f"] = name
            fresh = _normalize(self._embed([embed_text(c) for c in chunks]))
            keep = [i for i, c in enumerate(old_chunks) if c["f"] != name]
            merged = [old_chunks[i] for i in keep] + chunks
            vectors = np.vstack([old_vectors[keep], fresh]) if keep else fresh
            entry = {"name": name, "chars": len(body), "chunks": len(chunks), "ts": int(time.time())}
            atomic_write_text(self._source_path(name), body)
            self._drop_originals(name)
            if original is not None:
                path = self._original_path(name)
                tmp = path.with_name(f".{path.name}.tmp")
                tmp.write_bytes(original)
                os.replace(tmp, path)
                entry["original"] = len(original)
            self._commit([f for f in files if f["name"] != name] + [entry], merged, vectors)
        logger.info(f"[Lore] {self.root.parent.name}: файл {len(body)} зн., кусков {len(chunks)}")
        return entry

    def remove_file(self, filename: str) -> bool:
        name = clean_filename(filename)
        with self._write_lock:
            files, chunks, vectors = self._current()
            if not any(f["name"] == name for f in files):
                return False
            keep = [i for i, c in enumerate(chunks) if c["f"] != name]
            rest = [f for f in files if f["name"] != name]
            if keep:
                self._commit(rest, [chunks[i] for i in keep], vectors[keep])
            else:
                self._index_path.unlink(missing_ok=True)
                for old in self.root.glob("vectors.*.npy"):
                    old.unlink(missing_ok=True)
            self._source_path(name).unlink(missing_ok=True)
            self._drop_originals(name)
        return True

    # Просмотр файла из вкладки «Лор»
    def file_entry(self, filename: str) -> Optional[dict]:
        name = clean_filename(filename)
        return next((f for f in self.snapshot().files if f["name"] == name), None)

    def source_text(self, filename: str) -> Optional[str]:
        # Текст файла, как его нарезал лор (после разбора pdf/docx и чистки)
        try:
            return self._source_path(clean_filename(filename)).read_text(encoding="utf-8")
        except OSError:
            return None

    def original_path(self, filename: str) -> Optional[Path]:
        path = self._original_path(clean_filename(filename))
        return path if path.is_file() else None

    def file_chunks(self, filename: str) -> list[dict]:
        name = clean_filename(filename)
        return [{"s": c.get("s") or "", "t": c["t"]} for c in self.snapshot().chunks if c["f"] == name]

    # Поиск
    def _bm25(self, snap: _Snapshot, query_terms: Iterable[str]) -> tuple[np.ndarray, np.ndarray]:
        # (баллы BM25, есть ли редкое слово запроса) по всем кускам
        n = len(snap.chunks)
        scores = np.zeros(n, dtype=np.float32)
        rare = np.zeros(n, dtype=bool)
        rare_max = _rare_max(n)
        for t in set(query_terms):
            posting = snap.postings.get(t)
            if not posting:
                continue
            df = len(posting)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for i, tf in posting:
                norm = 1 - B + B * snap.doc_len[i] / snap.avg_len
                scores[i] += idf * tf * (K1 + 1) / (tf + K1 * norm)
                if df <= rare_max:
                    rare[i] = True
        return scores, rare

    def search(self, query: str, previous: str = "", name: str = "",
               names: Iterable[str] = (), limit: int = LIMIT) -> LoreResult:
        """Куски к реплике. previous — прошлые реплики, name — имя персонажа
        для векторного запроса, names — как к нему обращаются (снимается перед
        проверкой на болтовню). Находки — лучшие сверху."""
        snap = self.snapshot()
        if not snap.chunks:
            return LoreResult(skipped="no_lore")
        query = (query or "").strip()
        names = [n for n in (names or ()) if n] or ([name] if name else [])
        query_terms = terms(query)
        aimed = _aimed_at_character(query, names)
        if _is_smalltalk(query, names) or not (query_terms or aimed):
            return LoreResult(skipped="smalltalk")
        previous = (previous or "").strip()
        forms = [(query, 1.0)]
        if previous:
            forms.append((f"{previous} {query}", CONTEXT_WEIGHT))
        vecs = _normalize(self._embed([query] + [vector_query(q, name) for q, _ in forms]))
        plain = snap.vectors @ vecs[0]

        # К теме ли реплика. Слово из лора — ещё не тема: в длинном тексте есть
        # и «завтра», и «погода», поэтому кусок с ним должен быть близок и по смыслу
        _, rare_now = self._bm25(snap, query_terms)
        close_word = bool(rare_now.any()) and float(plain[rare_now].max()) >= WORD_SIM
        named = _names_in_lore(query, snap, names)
        follow_up = bool(previous and _THEM_RE.search(query))
        if not (aimed or close_word or named or follow_up):
            return LoreResult(skipped="off_topic")

        total = np.zeros(len(snap.chunks), dtype=np.float64)
        sims_best = np.full(len(snap.chunks), -1.0, dtype=np.float32)
        rare_any = np.zeros(len(snap.chunks), dtype=bool)
        for (text, weight), qvec in zip(forms, vecs[1:]):
            sims = snap.vectors @ qvec
            scores, rare = self._bm25(snap, terms(text))
            total += weight * _rrf(sims, scores)
            # Лучшее из форм запроса: для продолжения важна та, что с контекстом
            sims_best = np.maximum(sims_best, sims)
            rare_any |= rare

        order = np.argsort(-total)[:limit]
        # Явная находка: слово и смысл сошлись, названо имя из лора, персонажа
        # спросили о редкой вещи или кусок близок сам. Иначе («ты любишь
        # пиццу?») — несколько лучших с пометкой «может быть мимо»
        strong = (close_word or named or (aimed and bool(rare_now.any()))
                  or float(plain.max()) >= STRONG_SIM)
        weak = not strong
        top = order[:min(limit, WEAK_LIMIT) if weak else limit]
        # Хвост без своего слова и далеко от лучшего куска — шум и лишние токены
        best = float(sims_best[top].max())
        top = [int(i) for i in top
               if rare_any[i] or float(sims_best[i]) >= best - SPREAD_SIM]
        hits = [LoreHit(index=i, file=snap.chunks[i]["f"], section=snap.chunks[i].get("s") or "",
                        text=snap.chunks[i]["t"], similarity=float(sims_best[i]), words=bool(rare_any[i]))
                for i in top]
        return LoreResult(hits=hits, weak=weak)


def _rare_max(n: int) -> int:
    return min(RARE_CAP, max(RARE_MIN, int(n * RARE_SHARE)))


def _names_in_lore(query: str, snap: _Snapshot, names: Iterable[str]) -> bool:
    # Имя из лора в реплике: слово с большой буквы не в начале фразы
    # («что случилось с Тимуром?»), которое в лоре есть. Персонаж — не в счёт
    own = {stem_word(w) for n in names for w in _WORD_RE.findall(str(n).lower())}
    for sentence in _SENTENCE_RE.split(query):
        for word in _WORD_RE.findall(sentence)[1:]:
            if word[0].isupper() and not word.isupper():
                stem = stem_word(word)
                if stem not in own and stem in snap.postings:
                    return True
    return False


def _rrf(sims: np.ndarray, scores: np.ndarray) -> np.ndarray:
    # Слияние по местам: куски без единого слова запроса в список слов не входят
    rank_vec = np.empty(len(sims), dtype=np.int64)
    rank_vec[np.argsort(-sims)] = np.arange(len(sims))
    out = 1.0 / (RRF_K + rank_vec)
    hit = scores > 0
    if hit.any():
        rank_kw = np.empty(len(scores), dtype=np.int64)
        rank_kw[np.argsort(-scores)] = np.arange(len(scores))
        out = out + hit / (RRF_K + rank_kw)
    return out


def vector_query(query: str, name: str = "") -> str:
    # Запрос для модели векторов: «у тебя» → имя в именительном падеже. Склонять
    # произвольные имена незачем: модели важнее, что имя есть
    if not name:
        return query
    replaced = _YOU_RE.sub(lambda _m: name, query)
    return replaced if replaced != query else f"{name}: {query}"


def _aimed_at_character(query: str, names: Iterable[str]) -> bool:
    # Реплика к самому персонажу: «ты», глагол на «-ешь», его имя
    if _YOU_RE.search(query) or _YOU_VERB_RE.search(query):
        return True
    low = query.lower().replace("ё", "е")
    own = {stem_word(w) for n in names for w in _WORD_RE.findall(str(n).lower())}
    return any(stem_word(w) in own for w in _WORD_RE.findall(low))


def _is_smalltalk(query: str, names: Iterable[str]) -> bool:
    # Приветствие, «ок», «как дела»: вспоминать нечего. Те же шаблоны, что у
    # гейта веб-поиска
    from app.features.search_gate import search_skip_reason
    return bool(search_skip_reason(query, names))


def previous_turn(messages: Optional[list]) -> str:
    """Контекст для запроса: прошлая реплика пользователя и конец ответа
    персоны — на них опирается короткое продолжение. Текущая реплика уже
    стоит последней в истории и здесь не считается."""
    rows = [m for m in (messages or []) if isinstance(m, dict) and m.get("content")]
    if rows and rows[-1].get("role") == "user":
        rows = rows[:-1]
    user = next((str(m["content"]) for m in reversed(rows) if m.get("role") == "user"), "")
    reply = next((str(m["content"]) for m in reversed(rows) if m.get("role") == "assistant"), "")
    return f"{user} {reply[-TAIL_CHARS:]}".strip()


def build_block(result: LoreResult) -> str:
    # Блок для системного промпта. Правило про пробелы — и без находок: иначе
    # на промахе поиска персона выдумает себе прошлое
    if not result.hits:
        return GAP_RULE
    # В порядке текста: соседние куски читаются связно
    groups: list[tuple[str, list[str], int]] = []
    for hit in sorted(result.hits, key=lambda h: h.index):
        if groups and groups[-1][0] == hit.section and groups[-1][2] == hit.index - 1:
            groups[-1][1].append(hit.text)
            groups[-1] = (hit.section, groups[-1][1], hit.index)
        else:
            groups.append((hit.section, [hit.text], hit.index))
    lines = [f"[{section}] {' '.join(parts)}" if section else " ".join(parts)
             for section, parts, _ in groups]
    head = WEAK_HEAD if result.weak else HEAD
    return "\n".join([head, *lines, "", RULE, GAP_RULE])


# ── Лор персон процесса ──

_stores: dict = {}
_stores_lock = threading.Lock()


def lore_dir(persona: str) -> Path:
    # Веб-папка персоны и для Telegram: лор один на персону, а не на канал
    return data_dir() / f"api_{persona}" / "lore"


def store_for(persona: str) -> LoreStore:
    root = lore_dir(persona)
    key = str(root)
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = _stores[key] = LoreStore(root)
        return store
