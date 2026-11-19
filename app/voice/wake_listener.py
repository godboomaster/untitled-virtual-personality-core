"""Голосовая активация на компьютере ядра: «Коннор, какая погода?» — и
персона отвечает голосом, без открытого веба.

Где слушать. Есть панель в трее (desktop/) — слушает она: этот модуль
отдельным процессом панели (main(), python -m app.voice.wake_listener
--status), микрофон принадлежит панели, ядро для этого не нужно. Без
панели — в процессе ядра (WAKE_LISTEN=1 в .env, start_with_core); такой
слушатель уступает микрофон слушателю панели, пока тот о себе сообщает.

Микрофон слушается постоянно, речь распознаётся офлайн потоковыми моделями
sherpa-onnx (качаются с HuggingFace при первом запуске): малая русская (~28 МБ,
data/models/wake-ru) и английская (~73 МБ, data/models/wake-en). Языки —
WAKE_LANG (ru, en или ru,en — по умолчанию оба): при двух каждый кусок звука
слушают обе модели, фраза берётся у той, что увереннее (Decoder). Имя ищет
app/voice/wake_match. Реплика уходит в ядро тем же путём, что из веба
(POST /api/chat, чат web_user) — обмен виден в открытом чате; ответ
озвучивает система (say на macOS).

Состояния:
  IDLE   — ждём имя. Имя в промежуточном результате — короткий сигнал сразу.
  AWAIT  — имя прозвучало без просьбы («Коннор…»): следующая фраза — просьба
           (AWAIT_SEC). После ответа с вопросом — так же, FOLLOWUP_SEC.
  BUSY   — ждём ответ ядра и озвучиваем его; микрофон глушится, чтобы
           персона не будила саму себя. В разговоре у портрета не глушится
           вовсе: пока ядро думает, вы можете сказать новое (_while_waiting),
           пока она говорит — перебить.

Разговор у портрета (только слушатель панели, --portrait): имя прозвучало,
а чат этой персоны в вебе на этом компьютере не открыт (ядро сообщает
открытые чаты в ответе на /report) — панель зовёт портрет персоны на
рабочий стол, и дальше с ней говорят без имени: после каждого ответа AWAIT
на CONVERSE_SEC, пока не замолчат (или «хватит» — сразу, «пока» — после
её ответа). Фраза уходит не на первой паузе, а когда человек молчит
TALK_PAUSE_SEC: успеть подумать и договорить (_phrase, _due). Что человек говорит, видно на портрете по ходу речи:
status()["talk"] — персона, фаза (TALK_*), распознанный текст. Чат открыт —
как раньше, без портрета.

В разговоре можно назвать другую персону — встаёт и её портрет (рядом,
talk["personas"] — все, кто в разговоре), слушает теперь она, а прежняя
ждёт, пока её не назовут снова. Персону можно перебить: пока она отвечает
голосом, микрофон не глушится (ответ звучит в своём потоке), её голос из
колонок узнаётся по словам ответа (EchoRef, barge_start); ваши слова поверх
него — BARGE_WORDS, имя персоны, «подожди» или «хватит» — она замолкает и
слушает (talk["cut"] растёт: панель обрывает её реплику на портрете).

Фон (толпа, соседи, телевизор): вы ближе к микрофону, и ваша речь в нём
громче — звук до распознавателя идёт через фильтр громкости
(app/voice/near_gate): что тише вашего голоса больше чем на WAKE_NEAR_DB,
распознаватель слышит тишиной. Чужие слова не попадают во фразу, и она
кончается, когда замолчали вы. Громкость вашего голоса — по фразе с именем
и вашим фразам в разговоре, или «Запомнить мой голос» в панели (stdin
«calibrate»). Пока ждём имя, фильтр мягче; WAKE_NEAR=0 — без него.

Тот же автомат (без разговора у портрета и без фильтра громкости) — в
службе телефона (WakeWordService.java).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from app.voice import near_gate
from app.voice.near_gate import NearGate
from app.voice.wake_match import WakeHit, WakePhrase, match_wake, to_cyrillic, to_latin, wake_phrases

logger = logging.getLogger(__name__)

MODEL_URL = "https://huggingface.co/alphacep/vosk-model-small-streaming-ru/resolve/main/"
MODEL_FILES = (
    "am-onnx/encoder.int8.onnx",
    "am-onnx/decoder.int8.onnx",
    "am-onnx/joiner.int8.onnx",
    "lang/tokens.txt",
    "lang/unigram_500.vocab",
)
# Английская: zipformer icefall (LibriSpeech + GigaSpeech), пишет ЗАГЛАВНЫМИ.
# Малая en-20M теряла первое слово фразы — то есть имя
_EN_STEM = "epoch-99-avg-1-chunk-16-left-128.int8.onnx"
MODELS = {
    "ru": {
        "url": MODEL_URL, "files": MODEL_FILES,
        "encoder": MODEL_FILES[0], "decoder": MODEL_FILES[1], "joiner": MODEL_FILES[2],
        "tokens": MODEL_FILES[3], "vocab": MODEL_FILES[4],
    },
    "en": {
        "url": "https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26/resolve/main/",
        "files": (f"encoder-{_EN_STEM}", f"decoder-{_EN_STEM}", f"joiner-{_EN_STEM}", "tokens.txt", "bpe.model"),
        "encoder": f"encoder-{_EN_STEM}", "decoder": f"decoder-{_EN_STEM}", "joiner": f"joiner-{_EN_STEM}",
        "tokens": "tokens.txt",
        # Словаря кусков (для подсказки имён) в репозитории нет — делается
        # из bpe.model (spm_vocab)
        "vocab": "bpe.vocab", "spm": "bpe.model",
    },
}
LANGS = tuple(MODELS)
# Два языка: фраза — у модели, которая увереннее (средняя лог-вероятность
# токенов); языку разговора — перевес LANG_STICK: короткое «да»/«yes» обе
# слышат почти одинаково уверенно
LANG_STICK = 0.15
# Ниже этой уверенности текст модели — не речь (английская на ровном
# звуке пишет «and» с −3.7; настоящие слова, даже имя с акцентом, — выше −2.3)
LANG_MIN_SCORE = -3.0
# Язык разговора меняет фраза хотя бы из стольких слов (одно слово — не
# повод: «humph» на вздох не должен переключить русский разговор)
LANG_SWITCH_WORDS = 2
AWAIT_SEC = 8.0
# Окно ответа без имени после реплики персоны с вопросом; 0 — выключено
FOLLOWUP_SEC = float(os.getenv("WAKE_FOLLOWUP_SEC", "6") or 0)
# Разговор у портрета: столько секунд тишины после ответа — и разговор
# кончился (снова нужно имя); 0 — разговора у портрета нет
CONVERSE_SEC = float(os.getenv("WAKE_CONVERSE_SEC", "60") or 0)
# Открытые в вебе чаты из ответа ядра старше этого — не в счёт (ядро молчит)
CHATS_FRESH_SEC = 5.0
# Разговор у портрета: фраза кончилась (пауза распознавателя, 0,8 с) —
# ждём ещё столько: человек мог задуматься, продолжение допишется к той же
# фразе, и уйдёт она целиком; 0 — сразу, как вне разговора
TALK_PAUSE_SEC = float(os.getenv("WAKE_TALK_PAUSE_SEC", "5") or 0)
# Перебить персону в разговоре у портрета: столько ваших слов (не из её
# ответа) поверх её голоса — она замолкает. Ваша речь — хвост распознанного,
# где таких слов не меньше FOREIGN_SHARE
BARGE_WORDS = 2
FOREIGN_SHARE = 0.6
# Озвучка кончилась — её хвост ещё в микрофоне (эхо комнаты, буфер звука)
ECHO_TAIL_SEC = 0.5
# Список имён перечитывается раз в столько секунд (персону могли добавить)
PHRASES_REFRESH_SEC = 600
HOTWORD_SCORE = 2.0
# Ответ модели бывает долгим (веб-чат, поиск, режим управления)
CHAT_TIMEOUT_SEC = 300
# Каким устройством слушатель называется ядру (ChatRequest.client_id): его
# голосовые просьбы — одно устройство, телефон — другое (дубль одной фразы)
VOICE_CLIENT_ID = "wake-desktop"
# Столько секунд без кадров (и не во время ответа) — микрофон пропал
STALL_SEC = 5.0
# Пауза по просьбе клиента — не дольше (клиент продлевает её сам)
HOLD_MAX_SEC = 120.0
# Первые секунды звука — проверка, что это не нули запрета macOS
ZERO_CHECK_SEC = 3
NO_MIC_ACCESS = ("микрофон отдаёт ровные нули — похоже, macOS не дала доступ: "
                 "Системные настройки → Конфиденциальность и безопасность → Микрофон → "
                 "включите приложение, которое слушает (панель Virtual Persona или "
                 "Терминал, если ядро запущено из него)")

IDLE, AWAIT, BUSY = "idle", "await", "busy"
# Разговор у портрета (status()["talk"]["phase"]): ждёт фразу, слышит её
# (text — что распознано на сейчас), ждёт ответ ядра (sent — что ушло), говорит
TALK_LISTEN, TALK_HEAR, TALK_THINK, TALK_SPEAK = "listening", "hearing", "thinking", "speaking"
# Этап слушателя (status()["stage"]): грузит распознаватель, ждёт имена от
# ядра, слушает, микрофон недоступен (повтор), уступил микрофон панели
ST_MODEL, ST_NAMES, ST_LISTEN, ST_MIC, ST_YIELD = "model", "names", "listening", "mic", "yielded"
# Что не так (status()["issue"]) — панель переводит это сама; текст — в error
ISSUE_MIC_ACCESS, ISSUE_MIC, ISSUE_CORE, ISSUE_MODULES, ISSUE_FAILED = (
    "mic_access", "mic", "core", "modules", "failed")


def enabled_by_env() -> bool:
    return (os.getenv("WAKE_LISTEN") or "").strip().lower() in ("1", "true", "yes", "on")


def wake_langs() -> tuple[str, ...]:
    """Языки распознавания из WAKE_LANG: «ru», «en», «ru,en» (порядок — какой
    язык разговора с начала); пусто, «auto», «both» — оба, русский первым."""
    raw = (os.getenv("WAKE_LANG") or "").strip().lower()
    out: list[str] = []
    for part in re.split(r"[\s,;+/]+", raw):
        if part in MODELS and part not in out:
            out.append(part)
    return tuple(out) or LANGS


def model_dir(lang: str = "ru") -> Path:
    custom = (os.getenv("WAKE_MODEL_DIR" if lang == "ru" else f"WAKE_MODEL_DIR_{lang.upper()}") or "").strip()
    if custom:
        return Path(custom)
    from app.core.paths import data_dir
    return data_dir() / "models" / f"wake-{lang}"


def model_ready(d: Path, lang: str = "ru") -> bool:
    spec = MODELS[lang]
    need = [spec[k] for k in ("encoder", "decoder", "joiner", "tokens", "vocab")]
    return all((d / rel).is_file() and (d / rel).stat().st_size > 0 for rel in need)


def ensure_model(d: Path, lang: str = "ru") -> None:
    """Докачать недостающие файлы модели (частями в .part — обрыв не оставит
    битый файл под настоящим именем)."""
    if model_ready(d, lang):
        return
    import httpx
    spec = MODELS[lang]
    for rel in spec["files"]:
        dst = d / rel
        if dst.is_file() and dst.stat().st_size > 0:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        part = dst.with_name(dst.name + ".part")
        logger.info(f"[Wake] Качаю модель распознавания ({lang}): {rel}")
        with httpx.stream("GET", spec["url"] + rel, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
        part.replace(dst)
    if spec.get("spm"):
        vocab = d / spec["vocab"]
        if not (vocab.is_file() and vocab.stat().st_size > 0):
            tmp = vocab.with_name(vocab.name + ".part")
            spm_vocab(d / spec["spm"], tmp)
            tmp.replace(vocab)


def spm_vocab(model: Path, out: Path) -> None:
    """bpe.model sentencepiece (protobuf ModelProto) → словарь кусков
    «кусок<TAB>оценка» построчно, как *.vocab у sentencepiece: по нему
    sherpa-onnx режет подсказку имён на токены. Без модуля sentencepiece —
    разбор protobuf руками: pieces (поле 1) → piece (1, строка), score (2, float)."""
    import struct
    data = model.read_bytes()

    def varint(buf: bytes, i: int) -> tuple[int, int]:
        r = s = 0
        while True:
            c = buf[i]
            i += 1
            r |= (c & 0x7F) << s
            s += 7
            if c < 0x80:
                return r, i

    def skip(buf: bytes, i: int, wt: int) -> int:
        if wt == 0:
            return varint(buf, i)[1]
        if wt == 1:
            return i + 8
        if wt == 2:
            n, i = varint(buf, i)
            return i + n
        if wt == 5:
            return i + 4
        raise ValueError(f"protobuf: тип поля {wt}")

    lines = []
    i = 0
    while i < len(data):
        key, i = varint(data, i)
        field, wt = key >> 3, key & 7
        if field != 1 or wt != 2:
            i = skip(data, i, wt)
            continue
        n, i = varint(data, i)
        sub, i = data[i:i + n], i + n
        j, piece, score = 0, "", 0.0
        while j < len(sub):
            k, j = varint(sub, j)
            f2, w2 = k >> 3, k & 7
            if f2 == 1 and w2 == 2:
                m, j = varint(sub, j)
                piece, j = sub[j:j + m].decode("utf-8"), j + m
            elif f2 == 2 and w2 == 5:
                score, j = struct.unpack("<f", sub[j:j + 4])[0], j + 4
            else:
                j = skip(sub, j, w2)
        lines.append(f"{piece}\t{score:g}")
    if not lines:
        raise ValueError(f"{model.name}: нет кусков словаря")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_recognizer(d: Path, lang: str = "ru"):
    import sherpa_onnx
    spec = MODELS[lang]
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        encoder=str(d / spec["encoder"]),
        decoder=str(d / spec["decoder"]),
        joiner=str(d / spec["joiner"]),
        tokens=str(d / spec["tokens"]),
        # Один поток: с двумя процессор тратится втрое больше (onnxruntime
        # ждёт потоки вхолостую), а успевает и один — 2–4 % реального времени
        num_threads=1,
        sample_rate=16000,
        # modified_beam_search — ради подсказки имён (hotwords): с ней
        # «коннер» распознаётся как «коннор», «жабко» — как «жабка»
        decoding_method="modified_beam_search",
        max_active_paths=4,
        modeling_unit="bpe",
        bpe_vocab=str(d / spec["vocab"]),
        hotwords_score=HOTWORD_SCORE,
        enable_endpoint_detection=True,
        # Конец фразы: 0.8 с тишины после речи (по умолчанию 1.2 — ответ
        # казался заторможенным), 2.4 с — если речи не было
        rule1_min_trailing_silence=2.4,
        rule2_min_trailing_silence=0.8,
        rule3_min_utterance_length=20,
    )


def hotwords_for(phrases: list[WakePhrase], lang: str = "ru") -> str:
    """Подсказка имён модели языка: русской — кириллицей, английской —
    латиницей заглавными (своё написание персоны, иначе транслит)."""
    words = []
    for p in phrases:
        w = to_latin(p.latin or p.phrase) if lang == "en" else to_cyrillic(p.phrase)
        if w and w not in words:
            words.append(w)
    return "/".join(f"{w} :{HOTWORD_SCORE}" for w in words)


def _clean(text: str, lang: str) -> str:
    # Английская модель пишет заглавными — как у русской, строчными
    return text.lower() if lang == "en" else text


class Decoder:
    """Распознаватели (язык → модель) и их потоки с подсказкой имён.

    Один язык — как всегда. Два — каждый кусок звука слушают оба, текст
    (промежуточный и фразы) — у того, кто увереннее: средняя
    лог-вероятность токенов, языку разговора (lang) — перевес LANG_STICK;
    текст ниже LANG_MIN_SCORE — не речь. Конец фразы — по модели выбранного
    языка (ничего не слышно — языка разговора), сбрасываются все: другая
    модель находит паузу по-своему, и её конец посреди имени оборвал бы
    фразу. Язык разговора меняют фраза из LANG_SWITCH_WORDS слов
    на другом языке и язык ответа персоны (слушатель). lock — только этот
    язык (пока звучит её ответ: её голос из колонок узнаётся по словам ответа,
    в его же языке). alts — тексты остальных языков (имя персоны ищется и в
    них)."""

    def __init__(self, recognizer, phrases: list[WakePhrase], lang: Optional[str] = None):
        self.recognizers: dict = dict(recognizer) if isinstance(recognizer, dict) else {"ru": recognizer}
        self.langs: tuple[str, ...] = tuple(self.recognizers)
        self.lang = lang if lang in self.recognizers else self.langs[0]
        self.lock: Optional[str] = None
        # На каком языке последний текст и что услышали остальные модели
        self.heard_lang = self.lang
        self.alts: dict[str, str] = {}
        self.renew(phrases)

    def renew(self, phrases: list[WakePhrase]) -> None:
        self.streams = {lang: r.create_stream(hotwords=hotwords_for(phrases, lang))
                        for lang, r in self.recognizers.items()}
        self.alts = {}

    def feed(self, rate: int, chunk) -> tuple[str, bool]:
        """→ (текст фразы на сейчас, фраза кончилась)."""
        ends: dict[str, bool] = {}
        for lang, r in self.recognizers.items():
            s = self.streams[lang]
            s.accept_waveform(rate, chunk)
            while r.is_ready(s):
                r.decode_stream(s)
            ends[lang] = r.is_endpoint(s)
        if len(self.recognizers) == 1:
            lang, r = next(iter(self.recognizers.items()))
            text = _clean(r.get_result(self.streams[lang]), lang)
            self.heard_lang = lang
            end = ends[lang]
        else:
            heard: dict[str, tuple[str, float]] = {}
            for lang, r in self.recognizers.items():
                res = r.get_result_all(self.streams[lang])
                text = _clean(res.text, lang).strip()
                probs = list(res.ys_probs)
                if text and probs and sum(probs) / len(probs) >= LANG_MIN_SCORE:
                    heard[lang] = (text, sum(probs) / len(probs))
            lang = self._pick(heard)
            text = heard[lang][0] if lang else ""
            self.heard_lang = lang or self.lang
            self.alts = {k: t for k, (t, _) in heard.items() if k != lang}
            end = ends[lang or (self.lock if self.lock in self.recognizers else self.lang)]
            if end and lang and lang != self.lang and self.lock is None \
                    and len(text.split()) >= LANG_SWITCH_WORDS:
                logger.info(f"[Wake] Язык разговора: {lang}")
                self.lang = lang
        if end:
            for lang, r in self.recognizers.items():
                r.reset(self.streams[lang])
        return text, end

    def _pick(self, heard: dict[str, tuple[str, float]]) -> Optional[str]:
        if self.lock in self.recognizers:
            return self.lock if self.lock in heard else None
        if not heard:
            return None
        return max(heard, key=lambda k: heard[k][1] + (LANG_STICK if k == self.lang else 0.0))


# ===== Озвучка и сигналы =====

_URL = re.compile(r"https?://\S+|www\.\S+")
_MARKUP = re.compile(r"[*_`#>|~\[\]]+")
# Эмодзи и прочие символы вне букв: say читает их вслух названиями
_SYMBOLS = re.compile("[\U0001F000-\U0001FFFF☀-➿️‍]+")


def speech_text(text: str) -> str:
    s = _URL.sub(" ", text)
    s = _MARKUP.sub(" ", s)
    s = _SYMBOLS.sub(" ", s)
    return " ".join(s.split())


def _is_latin(text: str) -> bool:
    lat = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    cyr = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch in "ёЁ")
    return lat > cyr


class Voice:
    """Озвучка системой: macOS — say, Windows — System.Speech (PowerShell),
    иначе espeak, если есть. stop() обрывает реплику."""

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def _command(self, text: str) -> Optional[list[str]]:
        if sys.platform == "darwin":
            voice = (os.getenv("WAKE_TTS_VOICE_EN") or "Samantha") if _is_latin(text) \
                else (os.getenv("WAKE_TTS_VOICE_RU") or "Milena")
            return ["say", "-v", voice, text]
        if sys.platform == "win32":
            ps = ("Add-Type -AssemblyName System.Speech; "
                  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  "$s.Speak([Console]::In.ReadToEnd())")
            return ["powershell", "-NoProfile", "-Command", ps]
        if shutil.which("espeak-ng") or shutil.which("espeak"):
            return [shutil.which("espeak-ng") or "espeak", "-v", "en" if _is_latin(text) else "ru", text]
        return None

    def speak(self, text: str, alive=None) -> None:
        """Сказать и дождаться конца. alive() — нужно ли ещё: проверяется под
        замком прямо перед запуском (stop() между проверкой и запуском
        невозможен — оборвёт уже запущенное)."""
        text = speech_text(text)
        if not text:
            return
        cmd = self._command(text)
        if cmd is None:
            logger.warning("[Wake] Нечем озвучить ответ (нет say/PowerShell/espeak)")
            return
        try:
            with self._lock:
                if alive is not None and not alive():
                    return
                self._proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE if sys.platform == "win32" else subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                proc = self._proc
            if sys.platform == "win32" and proc.stdin:
                proc.stdin.write(text.encode("utf-8"))
                proc.stdin.close()
            proc.wait()
        except Exception as e:
            logger.warning(f"[Wake] Озвучка не удалась: {e}")
        finally:
            with self._lock:
                self._proc = None

    def stop(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()


def _chime(kind: str = "wake") -> None:
    """Короткий сигнал: «слушаю» / «принято» / «не дождался». Не блокирует."""
    if sys.platform != "darwin":
        return
    sound = {"wake": "Tink", "sent": "Pop", "timeout": "Bottle", "error": "Basso"}.get(kind, "Tink")
    path = f"/System/Library/Sounds/{sound}.aiff"
    if os.path.exists(path):
        try:
            subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


# ===== Связь с ядром =====

class CoreClient:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def personas(self) -> list[dict]:
        import httpx
        r = httpx.get(f"{self.base_url}/api/personas", headers=self._headers(), timeout=15)
        r.raise_for_status()
        return [p for p in r.json() if isinstance(p, dict)]

    def chat(self, persona: str, message: str, supersede: bool = False,
             called_as: Optional[str] = None) -> Optional[str]:
        """Ответ персоны; None — эту же фразу уже взял телефон (duplicate).
        supersede — реплика заменяет ход персоны, который ещё генерируется
        (сказали новое, пока она думала): ядро его обрывает. Отменённый
        ход (cancelled) — пустой ответ."""
        import httpx
        body = {
            "persona": persona, "message": message,
            # Тот же чат, что у веба: обмен голосом виден в открытом чате
            "user_id": "web_user", "chat_id": "web_user", "voice": True,
            # Своё устройство: новая фраза, пока прежняя ещё в работе, — не
            # дубль с телефона (ядро ставит её в очередь чата)
            "client_id": VOICE_CLIENT_ID,
        }
        if supersede:
            body["supersede"] = True
        if called_as:
            # Каким именем позвали (в message его нет) — имя режима персоны
            # включает режим
            body["called_as"] = called_as
        r = httpx.post(f"{self.base_url}/api/chat", headers=self._headers(), timeout=CHAT_TIMEOUT_SEC, json=body)
        r.raise_for_status()
        data = r.json()
        if data.get("duplicate"):
            return None
        if data.get("cancelled"):
            return ""
        parts = [data.get("reply") or ""] + [m for m in data.get("extra_messages") or [] if isinstance(m, str)]
        return " ".join(p.strip() for p in parts if p and p.strip())

    def cancel(self, persona: str) -> int:
        """Оборвать ход персоны, который ещё генерируется (ответ уже не нужен):
        ядро перестаёт ждать модель и отдаёт лок чата. → сколько отменено."""
        import httpx
        r = httpx.post(f"{self.base_url}/api/chat/cancel", headers=self._headers(), timeout=5,
                       json={"persona": persona, "user_id": "web_user", "chat_id": "web_user"})
        r.raise_for_status()
        return int((r.json() or {}).get("cancelled") or 0)

    def report(self, status: dict) -> dict:
        """Слушатель панели → ядро: своё состояние. Ответ — hold: сколько
        секунд ещё молчать (открыт голосовой чат веба), chat: чьи чаты
        открыты в вебе на этом компьютере (туда портрет не зовём)."""
        import httpx
        r = httpx.post(f"{self.base_url}/api/voice/wake/report", headers=self._headers(),
                       timeout=3, json=status)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {}


def _error_phrase(e: Exception, lang: str = "ru") -> str:
    """Что сказать вслух при сбое — на языке, на котором просили."""
    import httpx
    en = lang == "en"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code in (401, 403):
            return "No access to the core." if en else "Нет доступа к ядру."
        if code == 409:
            return "The persona is frozen and silent right now." if en else "Персона сейчас заморожена и молчит."
        return "The core couldn't answer." if en else "Ядро не смогло ответить."
    if isinstance(e, httpx.TimeoutException):
        return "The core is thinking too long." if en else "Ядро слишком долго думает."
    return "The core isn't responding." if en else "Ядро не отвечает."


# ===== Конец разговора у портрета =====

def _norm_words(text: str) -> list[str]:
    """Слова, как их пишет распознаватель: строчные, без «ё» и знаков
    («that's» — «that s»)."""
    return re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е")).split()


def _phrases(*items: str) -> frozenset:
    return frozenset(" ".join(_norm_words(x)) for x in items)


# Стоп-фраза — разговор кончается сразу, персоне ничего не уходит; прощание
# уходит ей (пусть попрощается), и разговор кончается после ответа.
# Распознаватель пишет строчными, без «ё» и знаков — сравнение после того же
_TALK_STOP = _phrases(
    "хватит", "все", "стоп", "отбой", "закончим", "закончили", "конец", "конец разговора",
    "все спасибо", "спасибо все", "на этом все", "можешь идти", "свободна", "свободен", "достаточно",
    "stop", "enough", "that's enough", "that's all", "that is all", "we're done", "i'm done",
    "end of conversation", "thanks that's all", "thank you that's all",
    "that'll do", "you can go", "dismissed", "never mind", "nevermind",
)
_TALK_BYE = _phrases(
    "пока", "пока пока", "до свидания", "до встречи", "до завтра", "спокойной ночи", "доброй ночи",
    "увидимся", "давай пока", "все пока", "спасибо пока",
    "bye", "bye bye", "goodbye", "good bye", "see you", "see you later", "see ya", "see you tomorrow",
    "good night", "goodnight", "talk to you later", "thanks bye", "thank you bye",
)
# Перед стоп-фразой или прощанием: «ну пока», «ладно, хватит», «okay, bye»
_TALK_FILLER = _phrases("ну", "ладно", "так", "ок", "окей", "хорошо", "ну ладно",
                        "ok", "okay", "alright", "well", "so", "oh", "right")
# «Подожди» — перебить: персона замолкает и слушает, разговор идёт дальше,
# ей самой ничего не уходит
_TALK_HUSH = _phrases(
    "подожди", "погоди", "постой", "стой", "тихо", "тише", "замолчи", "секунду", "секундочку",
    "минутку", "минуточку", "сек", "ща", "щас", "дай", "сказать", "скажу", "я", "ну", "ладно", "так",
    "а", "и", "ой", "эй", "слушай", "стоп",
    "wait", "hold", "on", "hang", "one", "a", "second", "sec", "moment", "minute", "just", "quiet",
    "hush", "shush", "shh", "hey", "listen", "let", "me", "speak", "say", "i", "oh", "please", "stop",
)
_TALK_HUSH_KEY = _phrases("подожди", "погоди", "постой", "стой", "тихо", "тише", "замолчи", "секунду",
                          "секундочку", "минутку", "минуточку",
                          "wait", "hold", "hang", "second", "sec", "minute", "moment", "quiet", "hush", "shush", "shh")
TALK_STOP, TALK_BYE, TALK_HUSH = "stop", "bye", "hush"
# Фраза только из этих слов — вздох, «хм», а не реплика (английская модель
# пишет «humph» на вздох и «and» на ровный звук)
_NOISE_WORDS = _phrases("хм", "мм", "эм", "ээ", "э", "м",
                        "hmm", "hm", "mm", "mhm", "um", "uh", "er", "erm", "ah", "oh", "huh", "humph",
                        "and", "the", "a")


def talk_end(text: str) -> Optional[str]:
    """Реплика в разговоре у портрета — его конец? TALK_STOP — замолчать
    сразу, TALK_BYE — отправить и закончить после ответа, TALK_HUSH —
    «подожди»: замолчать и слушать дальше, None — обычная."""
    words = _norm_words(text)
    if words and all(w in _TALK_HUSH for w in words) and any(w in _TALK_HUSH_KEY for w in words):
        return TALK_HUSH
    while len(words) > 1 and words[0] in _TALK_FILLER:
        words = words[1:]
    phrase = " ".join(words)
    if phrase in _TALK_STOP:
        return TALK_STOP
    if phrase in _TALK_BYE:
        return TALK_BYE
    return None


def _letters(text: str) -> int:
    return sum(ch.isalpha() for ch in text)


def _noise(text: str) -> bool:
    """Не реплика: меньше двух букв или только «хм», «humph», «and»."""
    if _letters(text) < 2:
        return True
    words = _norm_words(text)
    return bool(words) and all(w in _NOISE_WORDS for w in words)


# ===== Её голос в микрофоне =====

# Числа ответ пишет цифрами, а озвучка читает словами — такие слова в
# микрофоне тоже её (если в ответе были цифры)
_NUM_STEMS = (
    "ноль", "нул", "один", "одна", "одно", "одн", "два", "две", "двух", "двум", "три", "трех", "трем",
    "четыр", "пят", "шест", "сем", "восем", "девят", "десят", "одиннадц", "двенадц", "тринадц",
    "двадц", "тридц", "сорок", "девяност", "сто", "двест", "трист", "тысяч", "миллион", "полтор",
    "перв", "втор", "трет", "четверт", "процент", "градус", "рубл", "час", "минут",
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
    "twelve", "thirt", "fift", "twent", "forty", "hundred", "thousand", "million", "first", "second",
    "third", "point", "percent", "degree", "dollar", "hour", "minute", "clock",
)


class EchoRef:
    """Ответ, который персона говорит вслух: узнать её слова в микрофоне.
    Распознаватель пишет их не всегда точно («сеньор» → «синьор», «ни
    облачка» → «необлачка») — сверка с допуском, результат на слово
    запоминается (оно приходит в каждом промежуточном тексте заново)."""

    def __init__(self, reply: str):
        words = [w for w in _norm_words(speech_text(reply)) if not w.isdigit()]
        self.words = frozenset(words)
        self.pairs = frozenset(a + b for a, b in zip(words, words[1:]))
        self.stems = frozenset(w[:4] for w in words if len(w) >= 5)
        self.digits = any(ch.isdigit() for ch in reply)
        # Язык ответа: сверить её слова можно, только если его слушает своя
        # модель (английский ответ русская слышит как попало)
        self.latin = _is_latin(reply)
        self.lang = "en" if self.latin else "ru"
        self._seen: dict[str, bool] = {}

    def has(self, w: str) -> bool:
        hit = self._seen.get(w)
        if hit is None:
            hit = self._match(w)
            self._seen[w] = hit
        return hit

    def _match(self, w: str) -> bool:
        if w in self.words or w in self.pairs:
            return True
        if len(w) >= 5 and w[:4] in self.stems:
            return True
        if self.digits and w.startswith(_NUM_STEMS):
            return True
        from difflib import SequenceMatcher
        return any(SequenceMatcher(None, w, r).ratio() >= 0.75
                   for r in self.words if abs(len(r) - len(w)) <= 2)

    def mark(self, token: str) -> Optional[bool]:
        """Слово распознавателя: True — ваше, False — её, None — короткое (не
        понять). Пишет он с «ё» и дефисом («что-нибудь», «ещё») — сверка
        после того же, что у ответа."""
        pieces = _norm_words(token)
        joined = "".join(pieces)
        if len(joined) <= 2:
            return None
        if self.has(joined) or all(len(p) <= 2 or self.has(p) for p in pieces):
            return False
        return True


def barge_start(words: list[str], ref: EchoRef, enough) -> Optional[int]:
    """Перебили ли её — и с какого слова ваша речь. Ваши слова появляются
    поверх её голоса последними: распознанное делится на её часть и вашу там,
    где слева больше всего её узнанных слов, а справа — ваших (одно её слово
    внутри вашей речи — совпадение: «а что будет завтра» поверх «будет
    двенадцать»; одно не узнанное посреди её — путаница распознавателя).
    Перебили, когда последнее слово — ваше, ваших справа не меньше
    FOREIGN_SHARE и их достаточно (enough(ваша часть, сколько ваших)).
    None — пока не перебили."""
    marks = [ref.mark(w) for w in words]
    known = [m for m in marks if m is not None]
    if not known or known[-1] is not True:
        return None
    total_mine = sum(1 for m in known if m)
    total_hers = len(known) - total_mine
    best, best_j = None, 0
    hers_left = mine_left = 0
    for j in range(len(words) + 1):
        if j:
            m = marks[j - 1]
            hers_left += m is False
            mine_left += m is True
        score = hers_left - mine_left + (total_mine - mine_left) - (total_hers - hers_left)
        if best is None or score >= best:
            best, best_j = score, j
    j = best_j
    # Короткие слова прямо перед вашей частью («а», «и») — тоже ваши
    while j > 0 and marks[j - 1] is None:
        j -= 1
    mine = sum(1 for m in marks[j:] if m)
    hers = sum(1 for m in marks[j:] if m is False)
    if not mine or mine / (mine + hers) < FOREIGN_SHARE or not enough(" ".join(words[j:]), mine):
        return None
    return j


# ===== Слушатель =====

class WakeListener:
    def __init__(self, client: CoreClient, voice: Optional[Voice] = None, only: Optional[list[str]] = None):
        self.client = client
        self.voice = voice or Voice()
        self.only = only
        self.state = IDLE
        self.persona: Optional[str] = None
        self.error = ""
        self.issue = ""
        self.stage = ST_MODEL
        self.phrases: list[WakePhrase] = []
        self.last_heard = ""
        self._deadline = 0.0
        self._chimed = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._audio: "queue.Queue" = queue.Queue(maxsize=200)
        self._phrases_at = 0.0
        # Пауза по просьбе клиента: открыт голосовой режим чата — голосом
        # занят он, иначе одна фраза ушла бы дважды, а ответ звучал бы и тут
        self._held_until = 0.0
        # Разговор у портрета — только у слушателя панели (портрет рисует она)
        self.with_portrait = False
        # Идёт разговор: {persona, phase, text, sent}; меняется целиком (его
        # читает поток печати состояния), None — нет
        self._talk: Optional[dict] = None
        # Ушло прощание — после ответа разговор кончится
        self._talk_last = False
        # Панель просит закончить (убрали портрет) — сделает поток слушателя
        self._talk_end_req = False
        # Панель убрала портреты этих персон — разговор дальше без них
        self._talk_leave: list[str] = []
        # Имя другой персоны в промежуточном тексте — её портрет уже встал, но
        # фраза не кончилась: (кто слушал до неё, добавлена ли она только что)
        self._peek: Optional[tuple[str, bool]] = None
        # Чьи чаты открыты в вебе на этом компьютере (ответ ядра на /report)
        self._chats: frozenset = frozenset()
        self._chats_at = 0.0
        # Состояние для панели изменилось — печатать сразу, а не по таймеру
        self.changed = threading.Event()
        # Разговор у портрета: ответ звучит в своём потоке (handle), автомат —
        # в потоке микрофона; переходы — под замком
        self._lock = threading.RLock()
        # Номер хода: перебили или убрали — ответ прежнего хода не звучит
        self._turn = 0
        # Каким именем позвали персону (последним): уходит ядру с её просьбой
        # (called_as) — имя режима персоны включает режим, обычное — снимает
        self._called: dict[str, str] = {}
        # Что персона говорит вслух (её голос в микрофоне) и до какого
        # времени он там слышен (идёт — бесконечно, кончился — ECHO_TAIL_SEC)
        self._echo: Optional[EchoRef] = None
        self._echo_until = 0.0
        # Текущая фраза распознавателя застала её голос: ваше в ней — со слова
        # _seg_cut (None — ваших слов пока нет); последний текст фразы
        self._seg_echo = False
        self._seg_cut: Optional[int] = None
        self._seg_text = ""
        # Когда звуковая карта последний раз отдала кадры (и заглушённые тоже)
        self._frames_at = 0.0
        # Разговор у портрета: фраза ждёт паузы TALK_PAUSE_SEC —
        # {persona, message, heard, until}; None — не ждёт
        self._pending: Optional[dict] = None
        # Фильтр громкости (near_gate) текущего микрофона — свой у каждого:
        # громкость голоса у встроенного и гарнитуры разная
        self._gates: dict[str, NearGate] = {}
        self._gate: Optional[NearGate] = None
        # Панель попросила «Запомнить мой голос» — начнёт поток микрофона
        self._calib_req = False
        # Когда записан звук начала текущей фразы распознавателя
        self._seg_at: Optional[float] = None
        # Языки распознавания (Decoder.langs), язык разговора, на каком языке
        # последняя фраза; модель языка не загрузилась — почему
        self.langs: tuple[str, ...] = ("ru",)
        self.lang = "ru"
        self.lang_error = ""
        self._heard_lang = "ru"
        # Что услышали модели других языков (имя ищется и там)
        self._alts: dict[str, str] = {}
        # Язык ответа персоны — язык разговора (Decoder.lang) с следующего куска
        self._lang_hint: Optional[str] = None
        # Что ушло персоне в ходе, ответ на который ещё ждём: (как услышано,
        # что отправлено) — вернуть на портрет «думает», если сказанное в это
        # время ход не прервало
        self._think_was: tuple[str, str] = ("", "")
        # Персоны, чей ход прервали, пока ядро думало: следующая просьба им —
        # с supersede (на случай, если отмена до ядра не дошла)
        self._supersede: set[str] = set()

    # --- снаружи ---

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wake-listener", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 0.0) -> None:
        """Остановить; wait — дождаться, пока поток закроет микрофон."""
        self._stop.set()
        self.voice.stop()
        t = self._thread
        if wait and t and t.is_alive() and t is not threading.current_thread():
            t.join(wait)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def hold(self, seconds: float) -> None:
        """Не слушать столько секунд (0 — снять паузу). Клиент продлевает её,
        пока открыт голосовой режим; закрылся без «снять» — пауза истечёт сама."""
        self._held_until = time.monotonic() + max(0.0, min(float(seconds), HOLD_MAX_SEC))
        if seconds > 0 and self.state == AWAIT:
            self.state, self.persona = IDLE, None
        # Голосом занят веб — разговор у портрета кончился (недослушанная
        # фраза тоже: кадры больше не идут, конца фразы не будет)
        if seconds > 0 and self.state != BUSY:
            self._talk_stop("голосовой режим веба")

    @property
    def held(self) -> bool:
        return time.monotonic() < self._held_until

    def status(self) -> dict:
        talk = self._talk
        return {
            "running": self.running, "state": self.state, "persona": self.persona,
            "held": self.held, "stage": self.stage,
            "names": [p.phrase for p in self.phrases], "error": self.error,
            "issue": self.issue, "last_heard": self.last_heard,
            "talk": dict(talk) if talk else None,
            "near": self._near_status(),
            "langs": list(self.langs), "lang": self.lang, "lang_error": self.lang_error,
        }

    # --- фильтр громкости ---

    def _near_status(self) -> dict:
        g = self._gate
        if g is not None:
            st = g.status()
        else:
            st = {"on": near_gate.enabled_by_env(), "level": None, "source": "", "margin": near_gate.MARGIN_DB,
                  "calibrating": False, "calib_left": 0.0, "calib_result": ""}
        if self._calib_req:
            st["calibrating"] = True
            st["calib_left"] = near_gate.CALIB_SEC
        return st

    def calibrate(self) -> None:
        """«Запомнить мой голос» (панель): следующие CALIB_SEC вы говорите
        как обычно — это громкость вашего голоса; распознавание в это время
        не идёт (вы читаете текст, а не просите)."""
        self._calib_req = True
        self.changed.set()

    def forget_voice(self) -> None:
        """Забыть громкость вашего голоса (у этого микрофона): фильтр
        пропускает всё, пока не прозвучит имя."""
        g = self._gate
        if g is not None:
            g.forget()
        self.changed.set()

    # --- разговор у портрета ---

    def set_chats(self, personas) -> None:
        """Чьи чаты сейчас открыты в вебе на этом компьютере (от ядра)."""
        self._chats = frozenset(p for p in personas or () if isinstance(p, str))
        self._chats_at = time.monotonic()

    def chat_open(self, persona: str) -> bool:
        return persona in self._chats and time.monotonic() - self._chats_at < CHATS_FRESH_SEC

    @property
    def talking(self) -> bool:
        return self._talk is not None

    def _talk_begin(self, persona: str) -> None:
        """Прозвучало имя: разговор у портрета — если портрет есть и чат
        персоны в вебе не на экране. Уже идёт — теперь слушает эта персона
        (её портрет встаёт рядом), прежняя ждёт, пока её не назовут."""
        self._peek = None
        talk = self._talk
        if talk is not None:
            if talk["persona"] != persona or persona not in talk["personas"]:
                logger.info(f"[Wake] Разговор у портрета — теперь с {persona}")
                personas = talk["personas"] + ([] if persona in talk["personas"] else [persona])
                self._talk_set(persona=persona, personas=personas)
            return
        if not self.with_portrait or CONVERSE_SEC <= 0 or self.chat_open(persona):
            return
        logger.info(f"[Wake] Разговор с {persona} у портрета")
        self._talk = {"persona": persona, "phase": TALK_LISTEN, "text": "", "sent": "",
                      "personas": [persona], "cut": 0, "cut_persona": ""}
        self._talk_last = False
        self.changed.set()

    def _talk_peek(self, text: str) -> None:
        """Фраза ещё звучит, а в её начале — имя другой персоны: её портрет
        встаёт сразу, не дожидаясь конца фразы. Имя из текста ушло
        (распознаватель передумал) — как было."""
        talk = self._talk
        if talk is None or self.state != AWAIT or not text:
            return
        hit = match_wake(text, self.phrases)
        if hit is not None and hit.persona != talk["persona"]:
            if self._peek is not None:
                self._talk_unpeek()
                talk = self._talk or talk
            prev, added = talk["persona"], hit.persona not in talk["personas"]
            self._talk_begin(hit.persona)
            self._peek = (prev, added)
        elif hit is None and self._peek is not None:
            self._talk_unpeek()

    def _talk_unpeek(self) -> None:
        """Откатить подсмотренное имя: слушает та, что слушала до него, а
        портрет, вставший из-за него, уходит."""
        peek, self._peek = self._peek, None
        talk = self._talk
        if peek is None or talk is None:
            return
        prev, added = peek
        personas = [p for p in talk["personas"] if not (added and p == talk["persona"])]
        if prev not in personas:
            personas.append(prev)
        self._talk_set(persona=prev, personas=personas)

    def _talk_set(self, **kw) -> None:
        talk = self._talk
        if talk is None or all(talk.get(k) == v for k, v in kw.items()):
            return
        self._talk = {**talk, **kw}
        self.changed.set()

    def _talk_stop(self, why: str = "") -> None:
        talk = self._talk
        if talk is None:
            return
        self._talk = None
        self._talk_last = False
        self._talk_end_req = False
        self._talk_leave = []
        self._peek = None
        self._pending = None
        logger.info(f"[Wake] Разговор с {talk['persona']} закончен" + (f": {why}" if why else ""))
        self.changed.set()

    def end_talk(self) -> None:
        """Панель: разговор всё (значок микрофона) — и реплику не
        договаривать. Автомат меняет поток слушателя — здесь только просьба."""
        if self._talk is None:
            return
        self._talk_end_req = True
        self.voice.stop()
        self.changed.set()

    def leave_talk(self, persona: str) -> None:
        """Панель убрала портрет персоны — разговор дальше без неё (её
        реплику не договаривать). Делает поток слушателя."""
        talk = self._talk
        if talk is None or persona not in talk["personas"]:
            return
        with self._lock:
            self._talk_leave.append(persona)
        if talk["persona"] == persona and self.state == BUSY:
            self.voice.stop()
        self.changed.set()

    def _talk_end_requested(self) -> bool:
        """Просьба панели закончить разговор — выполнить. True — был разговор."""
        if not self._talk_end_req:
            return False
        self._talk_end_req = False
        if self._talk is None:
            return False
        who = self._talk["persona"] if self._talk else ""
        waiting = self._waiting()
        self._talk_stop("портрет убрали")
        if self.state == BUSY:
            # Ход ещё идёт (думает или говорит) — его ответ уже не нужен
            self._turn += 1
            self.voice.stop()
            if waiting and who:
                self._cancel_core(who)
        if self.state in (AWAIT, BUSY):
            self.state, self.persona = IDLE, None
        return True

    def _talk_leave_requested(self) -> None:
        """Портреты, которые панель убрала: этих персон в разговоре больше нет.
        Слушала одна из них — дальше слушает последняя оставшаяся; никого не
        осталось — разговор кончился."""
        with self._lock:
            gone, self._talk_leave = self._talk_leave, []
        for persona in gone:
            talk = self._talk
            if talk is None or persona not in talk["personas"]:
                continue
            rest = [p for p in talk["personas"] if p != persona]
            if self._pending is not None and self._pending["persona"] == persona:
                self._pending = None
            if talk["persona"] == persona and self.state == BUSY:
                # Думала или говорила — её ответ уже не нужен
                if self._waiting():
                    self._cancel_core(persona)
                self._interrupt()
            if not rest:
                self._talk_stop("портрет убрали")
                if self.state == AWAIT:
                    self.state, self.persona = IDLE, None
                continue
            self._peek = None
            if talk["persona"] == persona:
                logger.info(f"[Wake] Портрет {persona} убрали — дальше слушает {rest[-1]}")
                self._talk_set(persona=rest[-1], personas=rest, phase=TALK_LISTEN, text="", sent="")
                if self.state == AWAIT:
                    self.persona = rest[-1]
            else:
                self._talk_set(personas=rest)

    def _interrupt(self) -> None:
        """Ответ, который звучит (или ещё не пришёл), — не нужен: замолчать и
        слушать персону дальше без имени."""
        self._turn += 1
        self._talk_last = False
        self.voice.stop()
        self._echo_until = min(self._echo_until, time.monotonic() + ECHO_TAIL_SEC)
        self.state = AWAIT
        self._deadline = time.monotonic() + CONVERSE_SEC

    def _speaking(self) -> bool:
        talk = self._talk
        return self.state == BUSY and talk is not None and talk["phase"] == TALK_SPEAK

    def _waiting(self) -> bool:
        """Разговор у портрета: фраза ушла, ответа ядра ещё нет (она думает)."""
        talk = self._talk
        return self.state == BUSY and talk is not None and talk["phase"] != TALK_SPEAK

    def _barge(self) -> None:
        """Перебили: персона замолкает и слушает вас (панель обрывает её
        реплику на портрете — talk["cut"])."""
        talk = self._talk
        who = talk["persona"] if talk else (self.persona or "")
        self._interrupt()
        self.persona = who
        logger.info(f"[Wake] {who}: перебили — замолкает и слушает")
        if talk is not None:
            self._talk_set(phase=TALK_LISTEN, text="", sent="", cut=talk.get("cut", 0) + 1, cut_persona=who)

    def _echo_on(self, at: Optional[float] = None) -> bool:
        return self._echo is not None and (time.monotonic() if at is None else at) < self._echo_until

    def _enough(self, tail: str, mine: int) -> bool:
        """Хватит ли этого, чтобы перебить: BARGE_WORDS ваших слов, имя
        персоны, «подожди», «хватит», «пока»."""
        return mine >= BARGE_WORDS or (mine >= 1 and (
            match_wake(tail, self.phrases) is not None or talk_end(tail) is not None))

    def _through_echo(self, text: str) -> Optional[str]:
        """Фраза застала её голос: что в ней ваше (со слова _seg_cut). None —
        пока только её голос. Пока она говорит, вашего достаточно (_enough) —
        перебили."""
        words = text.split()
        if self._seg_cut is None:
            ref = self._echo
            if ref is None or ref.lang not in self.langs:
                return None
            k = barge_start(words, ref, self._enough)
            if k is None:
                return None
            if self._speaking():
                self._barge()
            self._seg_cut = k
        return " ".join(words[self._seg_cut:])

    def _echo_tail(self, dec: Optional["Decoder"], at: float) -> None:
        """Она договорила (и хвост её голоса в микрофоне прошёл), а фраза
        распознавателя, где только её голос, ещё не кончилась — выбросить её
        и слушать с чистого листа: ваше «да» сразу после ответа не
        склеится с её словами."""
        if not self._seg_echo or self._seg_cut is not None or self._echo_on(at) or self._speaking():
            return
        ref = self._echo
        words = self._seg_text.split()
        if ref is not None and ref.lang in self.langs:
            k = barge_start(words, ref, self._enough)
            if k is not None:
                self._seg_cut = k
                return
        self._seg_echo, self._seg_text = False, ""
        if dec is not None:
            dec.renew(self.phrases)

    def _await_sec(self) -> float:
        return CONVERSE_SEC if self._talk is not None else AWAIT_SEC

    def _fail(self, issue: str, text: str) -> None:
        self.issue, self.error = issue, text[:300]

    def _ok(self) -> None:
        self.issue, self.error = "", ""

    # --- имена ---

    def refresh_phrases(self, force: bool = False) -> bool:
        """Перечитать имена из ядра. True — список изменился."""
        now = time.monotonic()
        if not force and now - self._phrases_at < PHRASES_REFRESH_SEC:
            return False
        self._phrases_at = now
        try:
            fresh = wake_phrases(self.client.personas(), self.only)
        except Exception as e:
            logger.debug(f"[Wake] Имена не перечитаны: {e}")
            # Ядро ещё поднимается — повторим скоро, а не через 10 минут
            self._phrases_at = now - PHRASES_REFRESH_SEC + 5
            return False
        changed = fresh != self.phrases
        if changed:
            self.phrases = fresh
            logger.info(f"[Wake] Слушаю имена: {', '.join(p.phrase for p in fresh) or '— нет —'}")
        return changed

    # --- автомат (отдельно от звука — его гоняет тест) ---

    def on_partial(self, text: str) -> None:
        if self._seg_echo or (text and self._echo_on()):
            # Фраза застала её голос: ваше — только то, что поверх него
            self._seg_echo = True
            self._seg_text = text
            mine = self._through_echo(text) if text else None
            if mine is None:
                return
            text = mine
        elif self.state == BUSY and not self._waiting():
            return
        if text:
            self.last_heard = text
        if self.state == AWAIT and text:
            # Человек ещё говорит — не обрываем ожидание посреди фразы
            self._deadline = max(self._deadline, time.monotonic() + 2.0)
        if self.state == IDLE and not self._chimed and text:
            hit, _ = self._wake_hit(text, alts=not self._seg_echo)
            if hit is not None:
                self._chimed = True
                _chime("wake")
                # Портрет — уже на имени, не дожидаясь конца фразы
                self._talk_begin(hit.persona)
        if text and (self.state != BUSY or self._waiting()):
            # Назвали другую персону — её портрет сразу, до конца фразы
            self._talk_peek(text)
            # Что человек говорит — на портрете, по ходу речи; продолжение
            # ждущей фразы — вместе с ней
            shown = text
            pend = self._pending
            if pend is not None:
                pend["until"] = max(pend["until"], time.monotonic() + TALK_PAUSE_SEC)
                if self._talk is not None and self._talk["persona"] == pend["persona"]:
                    shown = f"{pend['heard']} {text}"
            self._talk_set(phase=TALK_HEAR, text=shown)

    def on_final(self, text: str) -> Optional[tuple[str, str]]:
        """Конец фразы. Вернёт (персона, просьба), если пора отправлять."""
        text = text.strip()
        alts = not self._seg_echo
        if self._seg_echo:
            # Фраза застала её голос: ваше в ней — со слова _seg_cut; вашего
            # нет — фраза не ваша, разговор как шёл
            mine = self._through_echo(text) if text else None
            self._seg_echo, self._seg_cut, self._seg_text = False, None, ""
            if mine is None:
                self._chimed = False
                if self.state == AWAIT and self._talk is not None and self._talk["phase"] == TALK_HEAR:
                    self._talk_set(phase=TALK_LISTEN, text="")
                return None
            text = mine
        if self.state == BUSY:
            if self._waiting():
                return self._while_waiting(text)
            # Ждём ответ ядра — фраза мимо (микрофон и так глушится)
            self._chimed = False
            return None
        chimed, self._chimed = self._chimed, False
        if not text:
            self._talk_set(phase=TALK_LISTEN, text="")
            if self.state == IDLE:
                self._talk_stop("имя послышалось")
            return None
        self.last_heard = text
        hit: Optional[WakeHit] = match_wake(text, self.phrases)
        if self.state == IDLE:
            if hit is None:
                # Имя по-английски с акцентом русская модель слышит «ханой» —
                # оно могло быть у модели другого языка
                hit, text = self._wake_hit(text, alts=alts)
                if hit is not None:
                    self.last_heard = text
            if hit is None:
                # Имя было только в промежуточном тексте — портрет не нужен
                self._talk_stop("имя послышалось")
                return None
            if not chimed:
                _chime("wake")
            self._note_called(hit)
            self._talk_begin(hit.persona)
            if hit.command:
                return self._phrase(hit.persona, hit.command, text)
            self._await(hit.persona, self._await_sec())
            return None
        if self.state == AWAIT:
            if hit is not None:
                self._note_called(hit)
                self._talk_begin(hit.persona)
                if hit.command:
                    return self._phrase(hit.persona, hit.command, text)
                if self._pending is None or self._pending["persona"] != hit.persona:
                    self._await(hit.persona, self._await_sec())
                return None
            # Имя другой персоны послышалось только по ходу фразы — как было
            self._talk_unpeek()
            if _noise(text):
                # «а», «хм», «humph» — шум или вздох, не реплика: слушаем
                # дальше (ждущая фраза — как была)
                if self._talk is None:
                    return None
                pend = self._pending
                if pend is not None:
                    self._talk_set(phase=TALK_HEAR, text=pend["heard"])
                else:
                    self._talk_set(phase=TALK_LISTEN, text="")
                return None
            return self._phrase(self.persona or "", text, text)
        return None

    def _while_waiting(self, text: str) -> Optional[tuple[str, str]]:
        """Фраза, пока персона думает над ответом (ядро ещё не ответило).
        Раньше микрофон в это время глушился, и минуту-другую долгого ответа
        ни имя, ни новая фраза не доходили. Шум, её имя без просьбы,
        «подожди» — она думает дальше (на имя — сигнал «слышу»); остальное —
        новая реплика: прежний ход прерван (его ответ останется в чате, но не
        прозвучит), фраза — как в ожидании просьбы («хватит» — конец, другая
        персона — слушает она, своя просьба — уйдёт ей; ядро поставит её в
        очередь за прежней)."""
        self._chimed = False
        talk = self._talk
        who = talk["persona"] if talk else (self.persona or "")
        heard, sent = self._think_was

        def back() -> None:
            self._talk_set(phase=TALK_THINK, text=heard, sent=sent)

        text = text.strip()
        if not text or _noise(text):
            back()
            return None
        hit = match_wake(text, self.phrases)
        rest = hit.command if hit is not None else text
        if hit is not None and hit.persona == who and not rest:
            _chime("wake")
            back()
            return None
        if hit is None and talk_end(text) == TALK_HUSH:
            back()
            return None
        logger.info(f"[Wake] {who} ещё думает, а сказано «{text}» — прежний ответ не прозвучит")
        if hit is None or hit.persona == who:
            # Вы о новом с ней же (или «хватит») — её генерация в ядре
            # обрывается сразу, а не через минуту; назвали другую — пусть
            # допишет: ответ ляжет в её чат
            self._cancel_core(who)
            self._supersede.add(who)
        self._interrupt()
        self.persona = who
        self._talk_set(phase=TALK_LISTEN, text="", sent="")
        return self.on_final(text)

    def _note_called(self, hit: WakeHit) -> None:
        # Имя, которым назвали персону, — до её следующей просьбы ядру. Только
        # у персоны с режимами: остальным ядро его не ждёт (словарь — из потока
        # микрофона и потока ответа; операции атомарны)
        if not any(ph.mode for ph in self.phrases if ph.persona == hit.persona):
            return
        called = getattr(self, "_called", None)
        if called is None:
            called = self._called = {}
        called[hit.persona] = hit.phrase

    def _take_called(self, persona: str) -> Optional[str]:
        called = getattr(self, "_called", None)
        return called.pop(persona, None) if called else None

    def _cancel_core(self, persona: str) -> None:
        """Ядро: оборвать ход персоны, который ещё генерируется (в фоне —
        автомат ждать сеть не должен)."""
        cancel = getattr(self.client, "cancel", None)
        if not persona or not callable(cancel):
            return

        def run():
            try:
                if cancel(persona):
                    logger.info(f"[Wake] {persona}: ядро оборвало прежний ход")
            except Exception as e:
                logger.debug(f"[Wake] Отмена хода {persona} не дошла: {e}")

        threading.Thread(target=run, name="wake-cancel", daemon=True).start()

    def _wake_hit(self, text: str, alts: bool = True) -> tuple[Optional[WakeHit], str]:
        """Имя в начале фразы: в тексте выбранного языка, иначе (alts) — в
        тексте модели другого языка; тогда и фраза — её. → (имя, фраза)."""
        hit = match_wake(text, self.phrases)
        if hit is not None or not alts:
            return hit, text
        for lang, alt in self._alts.items():
            hit = match_wake(alt, self.phrases)
            if hit is not None:
                self._heard_lang = lang
                return hit, alt
        return None, text

    def _phrase(self, persona: str, message: str, heard: str) -> Optional[tuple[str, str]]:
        """Фраза кончилась (пауза распознавателя). Вне разговора у портрета —
        просьба сразу. В разговоре — ждёт TALK_PAUSE_SEC (_due): человек мог
        задуматься, продолжение допишется к ней. «Хватит» — сразу;
        «подожди» — ждём дальше, ей не уходит. Назвали другую персону, пока
        фраза ждёт, — прежняя уходит своей персоне сейчас, новая ждёт паузы
        и уйдёт после её ответа."""
        if self._talk is None or TALK_PAUSE_SEC <= 0:
            return self._request(persona, message, heard)
        end = talk_end(message)
        if end == TALK_STOP:
            return self._request(persona, message, heard)
        now = time.monotonic()
        pend = self._pending
        if end == TALK_HUSH:
            if pend is None:
                return self._request(persona, message, heard)
            pend["until"] = now + TALK_PAUSE_SEC
            self._talk_set(phase=TALK_HEAR, text=pend["heard"])
            return None
        flushed = None
        if pend is not None and pend["persona"] != persona:
            flushed, pend = pend, None
        if pend is None:
            pend = {"persona": persona, "message": message, "heard": heard}
        else:
            pend = {**pend, "message": f"{pend['message']} {message}", "heard": f"{pend['heard']} {heard}"}
        pend["until"] = now + TALK_PAUSE_SEC
        self._pending = pend
        self.state, self.persona = AWAIT, persona
        self._deadline = max(self._deadline, now + CONVERSE_SEC)
        self._talk_set(persona=persona, phase=TALK_HEAR, text=pend["heard"], sent="")
        if flushed is not None:
            logger.info(f"[Wake] Назвали {persona}, пока фраза к {flushed['persona']} ждала паузы — она уходит сейчас")
            return self._request(flushed["persona"], flushed["message"], flushed["heard"])
        return None

    def _due(self) -> Optional[tuple[str, str]]:
        """Фраза в разговоре у портрета дождалась паузы — пора отправлять."""
        pend = self._pending
        if pend is None or self.state != AWAIT or self._talk is None or time.monotonic() < pend["until"]:
            return None
        self._pending = None
        return self._request(pend["persona"], pend["message"], pend["heard"])

    def _request(self, persona: str, message: str, heard: str) -> Optional[tuple[str, str]]:
        """Просьба готова. В разговоре у портрета — не конец ли это:
        «хватит» — молчим, «пока» — уходит персоне, и после ответа всё."""
        if self._talk is None:
            return persona, message
        end = talk_end(message)
        if end == TALK_STOP:
            self._talk_stop("стоп-фраза")
            self.state, self.persona = IDLE, None
            _chime("timeout")
            return None
        if end == TALK_HUSH:
            # «Подожди» — она уже молчит и слушает; ей самой ничего не уходит
            self._await(persona, CONVERSE_SEC)
            return None
        self._talk_last = end == TALK_BYE
        self._think_was = (heard, message)
        self._talk_set(persona=persona, phase=TALK_THINK, text=heard, sent=message)
        return persona, message

    def on_tick(self) -> None:
        self._talk_end_requested()
        self._talk_leave_requested()
        if self.state == AWAIT and time.monotonic() > self._deadline:
            if self._talk is not None:
                self._talk_stop("тишина")
            else:
                logger.info("[Wake] Просьбы не дождался")
            self.state, self.persona = IDLE, None
            _chime("timeout")

    def _await(self, persona: str, sec: float) -> None:
        self.state, self.persona = AWAIT, persona
        self._deadline = time.monotonic() + sec
        self._talk_set(persona=persona, phase=TALK_LISTEN, text="", sent="")
        logger.info(f"[Wake] Слушаю просьбу к {persona}")

    def handle(self, persona: str, message: str) -> None:
        """Отправить просьбу и озвучить ответ. Вне разговора у портрета
        микрофон в это время глушится; в разговоре — только пока ждём ответ:
        говорит она в своём потоке (process), её можно перебить (_barge), и
        тогда этот ход дальше ничего не меняет."""
        with self._lock:
            self._turn += 1
            turn = self._turn
            self.state, self.persona = BUSY, persona
        lang = self._heard_lang
        logger.info(f"[Wake] {persona} ← «{message}»" + (f" ({lang})" if len(self.langs) > 1 else ""))
        # «Принято»: ответ модели бывает долгим — без сигнала тишина похожа на сбой
        _chime("sent")
        asks_back = False
        answered = False

        def say(text: str) -> None:
            # Её голос в микрофоне — узнавать по словам ответа, пока звучит
            with self._lock:
                if turn != self._turn or self._stop.is_set() or self._talk_end_req:
                    return
                # На каком языке она отвечает — на том и разговор
                self._lang_hint = "en" if _is_latin(text) else "ru"
                if self._talk is not None:
                    self._talk_set(phase=TALK_SPEAK)
                    self._echo = EchoRef(text)
                    self._echo_until = float("inf")
            try:
                # Перебили, пока ход шёл сюда, — не начинать
                self.voice.speak(text, alive=lambda: turn == self._turn)
            finally:
                with self._lock:
                    if turn == self._turn and self._echo is not None:
                        self._echo_until = min(self._echo_until, time.monotonic() + ECHO_TAIL_SEC)

        t0 = time.monotonic()
        with self._lock:
            supersede = persona in self._supersede
            self._supersede.discard(persona)
        try:
            called = self._take_called(persona)
            reply = self.client.chat(persona, message, **({"supersede": True} if supersede else {}),
                                     **({"called_as": called} if called else {}))
            if reply is None:
                logger.info(f"[Wake] {persona}: ту же просьбу уже взял другой слушатель")
            else:
                answered = True
                logger.info(f"[Wake] {persona} → «{reply[:120]}» ({time.monotonic() - t0:.0f} с, "
                            f"{len(reply)} симв.)" + ("" if turn == self._turn else " — уже не нужен"))
                # Портрет убрали, пока персона думала, — не договаривать
                say(reply)
                asks_back = reply.rstrip().endswith("?")
            if self.issue == ISSUE_CORE:
                self._ok()
        except Exception as e:
            logger.warning(f"[Wake] Ответ не получен ({time.monotonic() - t0:.0f} с): {e}")
            self._fail(ISSUE_CORE, str(e)[:200])
            # Ход уже прерван (сказали новое) — о сбое прежнего не объявлять
            if turn == self._turn:
                _chime("error")
                if not self._stop.is_set():
                    self.voice.speak(_error_phrase(e, lang), alive=lambda: turn == self._turn)
        with self._lock:
            if turn != self._turn:
                # Перебили или убрали её портрет — автомат уже слушает дальше
                return
            if self._talk_end_requested():
                self.state, self.persona = IDLE, None
            elif self._talk is not None:
                # Разговор у портрета: дальше без имени, пока не замолчат.
                # Попрощались, ответа нет (взял другой слушатель) или сбой — всё
                if answered and not self._talk_last:
                    pend = self._pending
                    # Пока она отвечала, ждала фраза к другой — слушает та, фраза
                    # уйдёт, как только дождётся паузы
                    self._await(pend["persona"] if pend else persona, CONVERSE_SEC)
                    if pend is not None:
                        self._talk_set(phase=TALK_HEAR, text=pend["heard"])
                else:
                    self._talk_stop("попрощались" if self._talk_last else "ответа нет")
                    self.state, self.persona = IDLE, None
            elif asks_back and FOLLOWUP_SEC > 0:
                self._await(persona, FOLLOWUP_SEC)
            else:
                self.state, self.persona = IDLE, None

    # --- звук ---

    def _on_audio(self, indata, frames, time_info, status) -> None:
        # Поток PortAudio: только переложить кадры, без работы. Пока ждём
        # ответ и она говорит вне разговора у портрета — глушим (в разговоре
        # её слышно и перебить, и сказать новое, пока думает). Кадры идут —
        # микрофон жив, даже если заглушён (STALL_SEC)
        self._frames_at = time.monotonic()
        if (self.state == BUSY and self._talk is None) or self.held or self._stop.is_set():
            return
        try:
            self._audio.put_nowait((time.monotonic(), indata[:, 0].copy()))
        except queue.Full:
            pass

    def _near_reset(self) -> None:
        if self._gate is not None:
            self._gate.reset()
        self._seg_at = None

    def _drain(self) -> None:
        while True:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                return

    def _near(self, rate: int, chunk, at: float):
        """Кусок звука через фильтр громкости → (кусок распознавателю, когда
        он записан) или None — распознавателю ничего (идёт «Запомнить мой
        голос» или задержка фильтра ещё не набралась)."""
        gate = self._gate
        if gate is None:
            return chunk, at
        if self._calib_req:
            self._calib_req = False
            gate.calibrate(at)
            logger.info("[Wake] «Запомнить мой голос»: слушаю вашу речь")
            self.changed.set()
        calib = gate.calibrating
        # Назвали персону (и до конца разговора) — строже: дальше ваша речь
        strict = self._chimed or self.state != IDLE or self._talk is not None
        out = gate.feed(rate, chunk, at, strict)
        if calib or gate.calibrating:
            if not gate.calibrating:
                self.changed.set()
            return None
        if not len(out):
            return None
        return out, at - near_gate.LOOK_SEC

    def _decoder_setup(self, dec: "Decoder", at: float) -> None:
        """Перед куском звука: языки распознавателя, язык разговора по ответу
        персоны; пока её голос в микрофоне — только язык её ответа (её слова
        узнаются по ответу, его язык слушает своя модель)."""
        self.langs = dec.langs
        hint, self._lang_hint = self._lang_hint, None
        if hint in dec.recognizers:
            dec.lang = hint
        ref = self._echo
        multi = len(dec.langs) > 1
        dec.lock = ref.lang if multi and ref is not None and ref.lang in dec.langs and (
            self._seg_echo or self._echo_on(at)) else None

    def process(self, dec: "Decoder", rate: int, chunk, at: Optional[float] = None) -> None:
        """Кусок звука (at — когда записан): через фильтр громкости,
        распознать, проверить имя, по концу фразы — действовать."""
        at_in = time.monotonic() if at is None else at
        near = self._near(rate, chunk, at_in)
        if near is None:
            with self._lock:
                self.on_tick()
            return
        chunk, at = near
        gate = self._gate
        if self._seg_at is None:
            self._seg_at = at_in - len(chunk) / rate
        with self._lock:
            self._echo_tail(dec, at)
            self._decoder_setup(dec, at)
        text, final = dec.feed(rate, chunk)
        with self._lock:
            self._alts = dict(dec.alts)
            self._heard_lang = dec.heard_lang
            self.lang = dec.lang
            idle = self.state == IDLE and not self._chimed
            awaiting = self.state == AWAIT
            self.on_partial(text)
            seg_echo = self._seg_echo
            todo = self.on_final(text) if final else None
            heard_name = idle and (self._chimed or self.state != IDLE or todo is not None)
            if todo is None:
                todo = self._due()
            talk = todo is not None and self._talk is not None
            if talk:
                # Ждём ответ — микрофон глушится с этой же фразы, а не когда
                # поток хода дойдёт до handle
                self.state, self.persona = BUSY, todo[0]
        if gate is not None:
            # Имя сказали вы — громкость вашего голоса по нему; ваша фраза в
            # разговоре — понемногу по ней (её голос из колонок — не в счёт)
            if heard_name:
                lv = gate.learn_name(at_in)
                if lv is not None:
                    logger.info(f"[Wake] Громкость вашего голоса по имени: {lv:.1f} дБ")
            elif final and awaiting and not seg_echo and _letters(text) >= 2:
                gate.learn_phrase(self._seg_at)
        if final:
            self._seg_at = None
        if todo is not None:
            if talk:
                # Разговор у портрета: ход — в своём потоке, микрофон слушает
                # дальше (пока она говорит, её можно перебить)
                threading.Thread(target=self.handle, args=todo, name="wake-turn", daemon=True).start()
            else:
                self.handle(*todo)
                # Хвост собственной озвучки и всё, что накопилось, — мимо
                time.sleep(0.3)
                self._drain()
                self._near_reset()
                dec.renew(self.phrases)
        with self._lock:
            self.on_tick()

    def _run(self) -> None:
        try:
            self._loop()
        except Exception as e:
            self._fail(ISSUE_FAILED, str(e))
            logger.error(f"[Wake] Слушатель остановлен: {e}")

    def _load_models(self) -> dict:
        """Модели языков WAKE_LANG (докачать, если нет). Из двух одна не
        загрузилась (нет сети при первом запуске) — слушаем без неё, причина
        в status()["lang_error"]; не загрузилась единственная — сбой."""
        langs = wake_langs()
        recognizers: dict = {}
        errors = []
        for lang in langs:
            d = model_dir(lang)
            try:
                ensure_model(d, lang)
                recognizers[lang] = make_recognizer(d, lang)
            except Exception as e:
                if len(langs) == 1:
                    raise
                logger.warning(f"[Wake] Модель распознавания ({lang}) не загружена: {e} — слушаю без неё")
                errors.append(f"{lang}: {e}")
        self.lang_error = "; ".join(errors)[:300]
        if not recognizers:
            raise RuntimeError(self.lang_error or "нет моделей распознавания")
        self.langs = tuple(recognizers)
        self.lang = self._heard_lang = self.langs[0]
        logger.info(f"[Wake] Языки распознавания: {', '.join(self.langs)}")
        return recognizers

    def _loop(self) -> None:
        self.stage = ST_MODEL
        recognizers = self._load_models()
        # Ядро поднимается вместе со слушателем — ждём, пока ответит
        self.stage = ST_NAMES
        while not self._stop.is_set() and not self.phrases:
            self.refresh_phrases(force=True)
            if not self.phrases:
                self._stop.wait(5)
        dec = Decoder(recognizers, self.phrases)
        # Микрофон пропал (наушники отключились, устройство сменилось) —
        # открываем заново: слушатель живёт, пока живо ядро
        while not self._stop.is_set():
            if remote_active():
                # Слушает панель: свой микрофон закрыт, пока она о себе сообщает
                if self.stage != ST_YIELD:
                    logger.info("[Wake] Имя слушает панель в трее — микрофон ядра закрыт")
                self.stage = ST_YIELD
                self._stop.wait(1)
                continue
            try:
                self._listen(dec)
            except Exception as e:
                self.stage = ST_MIC
                self._fail(ISSUE_MIC, f"микрофон: {e}")
                logger.warning(f"[Wake] Микрофон недоступен ({e}) — повтор через 5 с")
            with self._lock:
                if self.state == BUSY:
                    # Ход ещё идёт — его ответ не звучит: микрофон уже не глушится
                    self._turn += 1
                    self.voice.stop()
                self.state, self.persona = IDLE, None
                self._talk_stop("микрофон")
            if remote_active():
                continue
            self._stop.wait(5)

    def _listen(self, dec: Decoder) -> None:
        import sounddevice as sd
        # Список устройств PortAudio запоминает при старте — перечитываем
        sd._terminate()
        sd._initialize()
        device = pick_input_device(sd)
        info = sd.query_devices(device, "input")
        rate = int(info["default_samplerate"])
        name = str(info.get("name") or "")
        if name not in self._gates:
            self._gates[name] = NearGate(name, near_gate.levels_path(), on=near_gate.enabled_by_env())
        self._gate = self._gates[name]
        self._drain()
        self._near_reset()
        dec.renew(self.phrases)
        with sd.InputStream(device=device, channels=1, samplerate=rate, dtype="float32",
                            blocksize=rate // 10, callback=self._on_audio):
            logger.info(f"[Wake] Микрофон: {info['name']} ({rate} Гц)")
            self.stage = ST_LISTEN
            if self.issue in (ISSUE_MIC, ISSUE_FAILED):
                self._ok()
            last = time.monotonic()
            # Ровные нули — не тишина комнаты (в ней всегда шум), а запрет
            # macOS на микрофон для приложения, которое слушает
            heard = 0.0
            fed = 0
            while not self._stop.is_set():
                if remote_active():
                    return
                if self.refresh_phrases():
                    dec.renew(self.phrases)
                try:
                    at, chunk = self._audio.get(timeout=0.3)
                except queue.Empty:
                    with self._lock:
                        self.on_tick()
                    # Заглушён (ждём ответ, пауза веба) — не «замолчал»: кадры
                    # от микрофона идут, их просто не берём
                    if self.held:
                        last = time.monotonic()
                    elif time.monotonic() - max(last, self._frames_at) > STALL_SEC:
                        logger.warning("[Wake] Микрофон замолчал — переоткрываю")
                        return
                    continue
                if fed < ZERO_CHECK_SEC * rate:
                    fed += len(chunk)
                    heard = max(heard, float(abs(chunk).max()) if len(chunk) else 0.0)
                    if fed >= ZERO_CHECK_SEC * rate and heard == 0.0:
                        if self.issue != ISSUE_MIC_ACCESS:
                            logger.warning(f"[Wake] {NO_MIC_ACCESS}")
                        self._fail(ISSUE_MIC_ACCESS, NO_MIC_ACCESS)
                        # Микрофон — заново через 5 с: доступ, выданный в
                        # «Системных настройках», подхватится без перезапуска
                        return
                    elif heard > 0.0 and self.issue == ISSUE_MIC_ACCESS:
                        self._ok()
                self.process(dec, rate, chunk, at)
                last = time.monotonic()


def pick_input_device(sd):
    """Микрофон: WAKE_DEVICE (номер или часть имени; default — системный),
    иначе встроенный микрофон ноутбука, иначе системный. Встроенный — не
    системный по умолчанию: системным часто становится гарнитура Bluetooth,
    а микрофон гарнитуры переводит её в режим звонка (звук хуже)."""
    want = (os.getenv("WAKE_DEVICE") or "").strip()
    if want.lower() == "default":
        return None
    if want:
        return int(want) if want.isdigit() else want
    for i, d in enumerate(sd.query_devices()):
        name = str(d.get("name", "")).lower()
        if d.get("max_input_channels", 0) > 0 and any(
                k in name for k in ("macbook", "built-in", "встроенн")):
            return i
    return None


# ===== Запуск вместе с ядром =====

_listener: Optional[WakeListener] = None
# Почему слушатель не поднялся (видно в GET /api/voice/wake, а не только в логе)
_start_error = ""


def get_listener() -> Optional[WakeListener]:
    return _listener


def start_error() -> str:
    return _start_error


def create_listener(base_url: str, token: str = "") -> tuple[Optional[WakeListener], str]:
    """Слушатель с ядром по адресу base_url. Нет модулей распознавания или
    микрофона в этом Python — (None, причина с командой установки)."""
    try:
        import sherpa_onnx  # noqa: F401
        import sounddevice  # noqa: F401
    except Exception as e:
        # Частый случай — Python не тот, в который ставились модули (другой venv)
        return None, (f"нет модулей в этом Python ({e}): {sys.executable} -m pip install "
                      f"\"sherpa-onnx>=1.13.8\" sounddevice")
    only = [p for p in (os.getenv("WAKE_PERSONAS") or "").split(",") if p.strip()] or None
    return WakeListener(CoreClient(base_url, token), only=only), ""


def start_with_core(host: str, port: int) -> Optional[WakeListener]:
    """Поднять слушатель в фоне процесса ядра (WAKE_LISTEN=1). Не мешает
    запуску: нет модулей или микрофона — предупреждение в лог."""
    global _listener, _start_error
    if not enabled_by_env():
        return None
    if host in ("", "0.0.0.0", "::", "localhost"):
        host = "127.0.0.1"
    lst, _start_error = create_listener(f"http://{host}:{port}", os.getenv("API_TOKEN") or "")
    if lst is None:
        logger.warning(f"[Wake] Голосовая активация выключена — {_start_error}")
        return None
    _listener = lst
    _listener.start()
    return _listener


# Ждать закрытия микрофона при остановке ядра — не дольше (цикл слушателя
# проверяет остановку каждые 0.3 с; запрос к уже закрытому API падает сразу)
STOP_WAIT_SEC = 5.0


def stop_with_core() -> None:
    """Остановка ядра: ждём, пока поток закроет микрофон. Иначе Python
    завершается, atexit sounddevice выключает PortAudio, а поток ещё в
    CloseStream — «bus error» (отчёт macOS 13.10: libportaudio CloseStream в
    потоке слушателя, главный поток — в Py_FinalizeEx)."""
    if _listener is not None:
        _listener.stop(wait=STOP_WAIT_SEC)


# ===== Слушатель панели: что о нём знает ядро =====
# Слушатель панели раз в REPORT_SEC сообщает ядру своё состояние (POST
# /api/voice/wake/report). Пока сообщения свежие, слушатель ядра молчит,
# GET /api/voice/wake показывает слушатель панели, а пауза голосового чата
# веба (POST /api/voice/wake/hold) уходит ему в ответе на сообщение.

REPORT_SEC = 1.0
REMOTE_FRESH_SEC = 5.0
_REMOTE_KEYS = ("running", "state", "persona", "held", "stage", "names", "error", "issue", "last_heard",
                "langs", "lang", "lang_error")
_remote: dict = {}
_remote_at = 0.0
_remote_hold_until = 0.0


def report_remote(status: dict) -> float:
    """Сообщение слушателя панели. → сколько секунд ему ещё молчать."""
    global _remote, _remote_at
    _remote = {k: status[k] for k in _REMOTE_KEYS if k in status}
    _remote_at = time.monotonic()
    return round(max(0.0, _remote_hold_until - time.monotonic()), 1)


def remote_active() -> bool:
    return _remote_at > 0 and time.monotonic() - _remote_at < REMOTE_FRESH_SEC


def remote_status() -> dict:
    return dict(_remote)


def hold_all(seconds: float) -> bool:
    """Пауза из веба (голосовой чат открыт): слушателю ядра — сразу,
    слушателю панели — в ответе на его сообщение. True — есть кому молчать."""
    global _remote_hold_until
    _remote_hold_until = time.monotonic() + max(0.0, min(float(seconds), HOLD_MAX_SEC))
    held = False
    if _listener is not None:
        _listener.hold(seconds)
        held = _listener.held
    return held or (seconds > 0 and remote_active())


# ===== Отдельный процесс (панель в трее) =====

STATUS_PREFIX = "WAKE_STATUS "
# Код выхода «нет модулей»: перезапуск не поможет, панель его не повторяет
EXIT_SETUP = 2
STATUS_EVERY_SEC = 10.0


def _report_loop(lst: WakeListener, stop: threading.Event) -> None:
    """Сообщать ядру о себе и брать у него паузу голосового чата веба."""
    import httpx
    while not stop.is_set():
        try:
            st = lst.status()
            # Что вы говорите портрету — ядру ни к чему
            st.pop("talk", None)
            r = lst.client.report(st)
            hold = float(r.get("hold") or 0)
            if hold > 0 or lst.held:
                lst.hold(hold)
            chats = r.get("chat")
            lst.set_chats(chats if isinstance(chats, list) else [])
        except httpx.HTTPStatusError:
            pass  # ядро старее слушателя (нет /report) — слушаем без паузы
        except Exception:
            pass  # ядро не запущено — пауза истечёт сама
        stop.wait(REPORT_SEC)


def _print_status(st: dict) -> None:
    print(STATUS_PREFIX + json.dumps(st, ensure_ascii=False), flush=True)


def _commands(lst: WakeListener) -> None:
    """Команды панели строками в stdin: end — закончить разговор у портрета
    (меню портрета), leave <персона> — её портрет убрали, разговор дальше
    без неё; calibrate — «Запомнить мой голос», forget_voice — забыть его.
    stdin закрыт или это /dev/null — просто выходим."""
    try:
        for line in sys.stdin:
            cmd, _, arg = line.strip().partition(" ")
            if cmd == "end":
                lst.end_talk()
            elif cmd == "leave" and arg:
                lst.leave_talk(arg.strip())
            elif cmd == "calibrate":
                lst.calibrate()
            elif cmd == "forget_voice":
                lst.forget_voice()
    except Exception:
        pass


def main(argv: Optional[list[str]] = None) -> int:
    """Слушатель отдельно от ядра — так его держит панель в трее:
    python -m app.voice.wake_listener [--core URL] [--status] [--portrait]."""
    import argparse
    import signal
    from app.core.envfile import load_env_file
    ap = argparse.ArgumentParser(prog="python -m app.voice.wake_listener",
                                 description="Голосовая активация: слушать имя персоны отдельно от ядра")
    ap.add_argument("--core", help="адрес API ядра (по умолчанию — из API_HOST и API_PORT)")
    ap.add_argument("--status", action="store_true",
                    help=f"печатать состояние строками «{STATUS_PREFIX}{{json}}» (для панели)")
    ap.add_argument("--portrait", action="store_true",
                    help="разговор у портрета на рабочем столе: после имени — без имени, "
                         "пока не замолчат (портрет рисует панель по полю talk состояния)")
    args = ap.parse_args(argv)
    load_env_file(".env")
    load_env_file(".env.config")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    # Отчёт ядру — раз в секунду: без этого каждый запрос строкой в логе
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    base = args.core
    if not base:
        host = os.getenv("API_HOST", "127.0.0.1")
        if host in ("", "0.0.0.0", "::", "localhost"):
            host = "127.0.0.1"
        base = f"http://{host}:{os.getenv('API_PORT', '8000')}"
    lst, err = create_listener(base, os.getenv("API_TOKEN") or "")
    if lst is None:
        logger.error(f"[Wake] Голосовая активация не запущена — {err}")
        if args.status:
            _print_status({"running": False, "stage": ST_MODEL, "error": err, "issue": ISSUE_MODULES})
        return EXIT_SETUP
    lst.with_portrait = args.portrait
    stop = threading.Event()

    def on_term(*_):
        stop.set()
        lst.changed.set()

    # Панель останавливает слушатель SIGTERM-ом: закрыть микрофон штатно
    signal.signal(signal.SIGTERM, on_term)
    lst.start()
    threading.Thread(target=_report_loop, args=(lst, stop), name="wake-report", daemon=True).start()
    if args.portrait:
        threading.Thread(target=_commands, args=(lst,), name="wake-commands", daemon=True).start()
    logger.info(f"[Wake] Слушатель запущен отдельно от ядра, ядро — {base}")
    # Панель упала или её убили — не держать микрофон сиротой (unix: сирота
    # переходит к launchd/init, getppid меняется)
    parent = os.getppid()
    last: Optional[dict] = None
    last_at = 0.0
    try:
        while lst.running and not stop.is_set():
            if args.status and sys.platform != "win32" and os.getppid() != parent:
                logger.warning("[Wake] Панель закрылась — слушатель выходит")
                stop.set()
                break
            # Сначала сброс, потом снимок: изменение после снимка разбудит снова
            lst.changed.clear()
            st = lst.status()
            if args.status and (st != last or time.monotonic() - last_at > STATUS_EVERY_SEC):
                _print_status(st)
                last, last_at = st, time.monotonic()
            # Разговор у портрета (что вы говорите) — сразу, остальное — по таймеру
            lst.changed.wait(0.3)
    except KeyboardInterrupt:
        pass
    failed = not stop.is_set() and not lst.running
    lst.stop(wait=STOP_WAIT_SEC)
    stop.set()
    if args.status:
        _print_status(lst.status())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
