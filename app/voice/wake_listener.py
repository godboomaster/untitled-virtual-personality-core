"""Голосовая активация на компьютере ядра: «Коннор, какая погода?» — и
персона отвечает голосом, без открытого веба.

Где слушать. Есть панель в трее (desktop/) — слушает она: этот модуль
отдельным процессом панели (main(), python -m app.voice.wake_listener
--status), микрофон принадлежит панели, ядро для этого не нужно. Без
панели — в процессе ядра (WAKE_LISTEN=1 в .env, start_with_core); такой
слушатель уступает микрофон слушателю панели, пока тот о себе сообщает.

Микрофон слушается постоянно, речь распознаётся офлайн: потоковая модель sherpa-onnx (малая русская, ~28 МБ, качается
с HuggingFace при первом запуске в data/models/wake-ru). Имя ищет
app/voice/wake_match. Реплика уходит в ядро тем же путём, что из веба
(POST /api/chat, чат web_user) — обмен виден в открытом чате; ответ
озвучивает система (say на macOS).

Состояния:
  IDLE   — ждём имя. Имя в промежуточном результате — короткий сигнал сразу.
  AWAIT  — имя прозвучало без просьбы («Коннор…»): следующая фраза — просьба
           (AWAIT_SEC). После ответа с вопросом — так же, FOLLOWUP_SEC.
  BUSY   — ждём ответ ядра и озвучиваем его; микрофон глушится, чтобы
           персона не будила саму себя.

Тот же автомат — в службе телефона (WakeWordService.java).
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

from app.voice.wake_match import WakeHit, WakePhrase, match_wake, to_cyrillic, wake_phrases

logger = logging.getLogger(__name__)

MODEL_URL = "https://huggingface.co/alphacep/vosk-model-small-streaming-ru/resolve/main/"
MODEL_FILES = (
    "am-onnx/encoder.int8.onnx",
    "am-onnx/decoder.int8.onnx",
    "am-onnx/joiner.int8.onnx",
    "lang/tokens.txt",
    "lang/unigram_500.vocab",
)
AWAIT_SEC = 8.0
# Окно ответа без имени после реплики персоны с вопросом; 0 — выключено
FOLLOWUP_SEC = float(os.getenv("WAKE_FOLLOWUP_SEC", "6") or 0)
# Список имён перечитывается раз в столько секунд (персону могли добавить)
PHRASES_REFRESH_SEC = 600
HOTWORD_SCORE = 2.0
# Ответ модели бывает долгим (веб-чат, поиск, режим управления)
CHAT_TIMEOUT_SEC = 300
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
# Этап слушателя (status()["stage"]): грузит распознаватель, ждёт имена от
# ядра, слушает, микрофон недоступен (повтор), уступил микрофон панели
ST_MODEL, ST_NAMES, ST_LISTEN, ST_MIC, ST_YIELD = "model", "names", "listening", "mic", "yielded"
# Что не так (status()["issue"]) — панель переводит это сама; текст — в error
ISSUE_MIC_ACCESS, ISSUE_MIC, ISSUE_CORE, ISSUE_MODULES, ISSUE_FAILED = (
    "mic_access", "mic", "core", "modules", "failed")


def enabled_by_env() -> bool:
    return (os.getenv("WAKE_LISTEN") or "").strip().lower() in ("1", "true", "yes", "on")


def model_dir() -> Path:
    custom = (os.getenv("WAKE_MODEL_DIR") or "").strip()
    if custom:
        return Path(custom)
    from app.core.paths import data_dir
    return data_dir() / "models" / "wake-ru"


def ensure_model(d: Path) -> None:
    """Докачать недостающие файлы модели (частями в .part — обрыв не оставит
    битый файл под настоящим именем)."""
    import httpx
    for rel in MODEL_FILES:
        dst = d / rel
        if dst.is_file() and dst.stat().st_size > 0:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        part = dst.with_name(dst.name + ".part")
        logger.info(f"[Wake] Качаю модель распознавания: {rel}")
        with httpx.stream("GET", MODEL_URL + rel, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
        part.replace(dst)


def make_recognizer(d: Path):
    import sherpa_onnx
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        encoder=str(d / "am-onnx/encoder.int8.onnx"),
        decoder=str(d / "am-onnx/decoder.int8.onnx"),
        joiner=str(d / "am-onnx/joiner.int8.onnx"),
        tokens=str(d / "lang/tokens.txt"),
        num_threads=2,
        sample_rate=16000,
        # modified_beam_search — ради подсказки имён (hotwords): с ней
        # «коннер» распознаётся как «коннор», «жабко» — как «жабка»
        decoding_method="modified_beam_search",
        max_active_paths=4,
        modeling_unit="bpe",
        bpe_vocab=str(d / "lang/unigram_500.vocab"),
        hotwords_score=HOTWORD_SCORE,
        enable_endpoint_detection=True,
        # Конец фразы: 0.8 с тишины после речи (по умолчанию 1.2 — ответ
        # казался заторможенным), 2.4 с — если речи не было
        rule1_min_trailing_silence=2.4,
        rule2_min_trailing_silence=0.8,
        rule3_min_utterance_length=20,
    )


def hotwords_for(phrases: list[WakePhrase]) -> str:
    words = []
    for p in phrases:
        w = to_cyrillic(p.phrase)
        if w and w not in words:
            words.append(w)
    return "/".join(f"{w} :{HOTWORD_SCORE}" for w in words)


class Decoder:
    """Распознаватель и текущий поток с подсказкой имён."""

    def __init__(self, recognizer, phrases: list[WakePhrase]):
        self.recognizer = recognizer
        self.renew(phrases)

    def renew(self, phrases: list[WakePhrase]) -> None:
        self.stream = self.recognizer.create_stream(hotwords=hotwords_for(phrases))

    def feed(self, rate: int, chunk) -> tuple[str, bool]:
        """→ (текст фразы на сейчас, фраза кончилась)."""
        r = self.recognizer
        self.stream.accept_waveform(rate, chunk)
        while r.is_ready(self.stream):
            r.decode_stream(self.stream)
        text = r.get_result(self.stream)
        if r.is_endpoint(self.stream):
            r.reset(self.stream)
            return text, True
        return text, False


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

    def speak(self, text: str) -> None:
        text = speech_text(text)
        if not text:
            return
        cmd = self._command(text)
        if cmd is None:
            logger.warning("[Wake] Нечем озвучить ответ (нет say/PowerShell/espeak)")
            return
        try:
            with self._lock:
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

    def chat(self, persona: str, message: str) -> Optional[str]:
        """Ответ персоны; None — эту же фразу уже взял телефон (duplicate)."""
        import httpx
        r = httpx.post(f"{self.base_url}/api/chat", headers=self._headers(), timeout=CHAT_TIMEOUT_SEC, json={
            "persona": persona, "message": message,
            # Тот же чат, что у веба: обмен голосом виден в открытом чате
            "user_id": "web_user", "chat_id": "web_user", "voice": True,
        })
        r.raise_for_status()
        data = r.json()
        if data.get("duplicate"):
            return None
        parts = [data.get("reply") or ""] + [m for m in data.get("extra_messages") or [] if isinstance(m, str)]
        return " ".join(p.strip() for p in parts if p and p.strip())

    def report(self, status: dict) -> float:
        """Слушатель панели → ядро: своё состояние. Ответ — сколько секунд ещё
        молчать (открыт голосовой чат веба)."""
        import httpx
        r = httpx.post(f"{self.base_url}/api/voice/wake/report", headers=self._headers(),
                       timeout=3, json=status)
        r.raise_for_status()
        return float(r.json().get("hold") or 0)


def _error_phrase(e: Exception) -> str:
    import httpx
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code in (401, 403):
            return "Нет доступа к ядру."
        if code == 409:
            return "Персона сейчас заморожена и молчит."
        return "Ядро не смогло ответить."
    if isinstance(e, httpx.TimeoutException):
        return "Ядро слишком долго думает."
    return "Ядро не отвечает."


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

    @property
    def held(self) -> bool:
        return time.monotonic() < self._held_until

    def status(self) -> dict:
        return {
            "running": self.running, "state": self.state, "persona": self.persona,
            "held": self.held, "stage": self.stage,
            "names": [p.phrase for p in self.phrases], "error": self.error,
            "issue": self.issue, "last_heard": self.last_heard,
        }

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
        if text:
            self.last_heard = text
        if self.state == AWAIT and text:
            # Человек ещё говорит — не обрываем ожидание посреди фразы
            self._deadline = max(self._deadline, time.monotonic() + 2.0)
        if self.state == IDLE and not self._chimed and text:
            if match_wake(text, self.phrases) is not None:
                self._chimed = True
                _chime("wake")

    def on_final(self, text: str) -> Optional[tuple[str, str]]:
        """Конец фразы. Вернёт (персона, просьба), если пора отправлять."""
        chimed, self._chimed = self._chimed, False
        text = text.strip()
        if not text:
            return None
        self.last_heard = text
        hit: Optional[WakeHit] = match_wake(text, self.phrases)
        if self.state == IDLE:
            if hit is None:
                return None
            if not chimed:
                _chime("wake")
            if hit.command:
                return hit.persona, hit.command
            self._await(hit.persona, AWAIT_SEC)
            return None
        if self.state == AWAIT:
            if hit is not None:
                if hit.command:
                    return hit.persona, hit.command
                self._await(hit.persona, AWAIT_SEC)
                return None
            return self.persona or "", text
        return None

    def on_tick(self) -> None:
        if self.state == AWAIT and time.monotonic() > self._deadline:
            logger.info("[Wake] Просьбы не дождался")
            self.state, self.persona = IDLE, None
            _chime("timeout")

    def _await(self, persona: str, sec: float) -> None:
        self.state, self.persona = AWAIT, persona
        self._deadline = time.monotonic() + sec
        logger.info(f"[Wake] Слушаю просьбу к {persona}")

    def handle(self, persona: str, message: str) -> None:
        """Отправить просьбу и озвучить ответ (микрофон в это время глушится)."""
        self.state, self.persona = BUSY, persona
        logger.info(f"[Wake] {persona} ← «{message}»")
        # «Принято»: ответ модели бывает долгим — без сигнала тишина похожа на сбой
        _chime("sent")
        asks_back = False
        try:
            reply = self.client.chat(persona, message)
            if reply is None:
                logger.info(f"[Wake] {persona}: ту же просьбу уже взял другой слушатель")
            else:
                logger.info(f"[Wake] {persona} → «{reply[:120]}»")
                if not self._stop.is_set():
                    self.voice.speak(reply)
                asks_back = reply.rstrip().endswith("?")
            if self.issue == ISSUE_CORE:
                self._ok()
        except Exception as e:
            logger.warning(f"[Wake] Ответ не получен: {e}")
            self._fail(ISSUE_CORE, str(e)[:200])
            _chime("error")
            if not self._stop.is_set():
                self.voice.speak(_error_phrase(e))
        if asks_back and FOLLOWUP_SEC > 0:
            self._await(persona, FOLLOWUP_SEC)
        else:
            self.state, self.persona = IDLE, None

    # --- звук ---

    def _on_audio(self, indata, frames, time_info, status) -> None:
        # Поток PortAudio: только переложить кадры, без работы
        if self.state == BUSY or self.held or self._stop.is_set():
            return
        try:
            self._audio.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def _drain(self) -> None:
        while True:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                return

    def process(self, dec: "Decoder", rate: int, chunk) -> None:
        """Кусок звука: распознать, проверить имя, по концу фразы — действовать."""
        text, final = dec.feed(rate, chunk)
        self.on_partial(text)
        if final:
            todo = self.on_final(text)
            if todo is not None:
                self.handle(*todo)
                # Хвост собственной озвучки и всё, что накопилось, — мимо
                time.sleep(0.3)
                self._drain()
                dec.renew(self.phrases)
        self.on_tick()

    def _run(self) -> None:
        try:
            self._loop()
        except Exception as e:
            self._fail(ISSUE_FAILED, str(e))
            logger.error(f"[Wake] Слушатель остановлен: {e}")

    def _loop(self) -> None:
        self.stage = ST_MODEL
        d = model_dir()
        ensure_model(d)
        recognizer = make_recognizer(d)
        # Ядро поднимается вместе со слушателем — ждём, пока ответит
        self.stage = ST_NAMES
        while not self._stop.is_set() and not self.phrases:
            self.refresh_phrases(force=True)
            if not self.phrases:
                self._stop.wait(5)
        dec = Decoder(recognizer, self.phrases)
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
            self.state, self.persona = IDLE, None
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
        self._drain()
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
                    chunk = self._audio.get(timeout=0.3)
                except queue.Empty:
                    self.on_tick()
                    if self.held:
                        last = time.monotonic()
                    elif time.monotonic() - last > STALL_SEC:
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
                self.process(dec, rate, chunk)
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
_REMOTE_KEYS = ("running", "state", "persona", "held", "stage", "names", "error", "issue", "last_heard")
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
            hold = lst.client.report(lst.status())
            if hold > 0 or lst.held:
                lst.hold(hold)
        except httpx.HTTPStatusError:
            pass  # ядро старее слушателя (нет /report) — слушаем без паузы
        except Exception:
            pass  # ядро не запущено — пауза истечёт сама
        stop.wait(REPORT_SEC)


def _print_status(st: dict) -> None:
    print(STATUS_PREFIX + json.dumps(st, ensure_ascii=False), flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    """Слушатель отдельно от ядра — так его держит панель в трее:
    python -m app.voice.wake_listener [--core URL] [--status]."""
    import argparse
    import signal
    from app.core.envfile import load_env_file
    ap = argparse.ArgumentParser(prog="python -m app.voice.wake_listener",
                                 description="Голосовая активация: слушать имя персоны отдельно от ядра")
    ap.add_argument("--core", help="адрес API ядра (по умолчанию — из API_HOST и API_PORT)")
    ap.add_argument("--status", action="store_true",
                    help=f"печатать состояние строками «{STATUS_PREFIX}{{json}}» (для панели)")
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
    stop = threading.Event()
    # Панель останавливает слушатель SIGTERM-ом: закрыть микрофон штатно
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    lst.start()
    threading.Thread(target=_report_loop, args=(lst, stop), name="wake-report", daemon=True).start()
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
            st = lst.status()
            if args.status and (st != last or time.monotonic() - last_at > STATUS_EVERY_SEC):
                _print_status(st)
                last, last_at = st, time.monotonic()
            stop.wait(0.3)
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
