"""Голосовая активация на компьютере ядра: «Коннор, какая погода?».

  A. имя в распознанном тексте (app/voice/wake_match): общие примеры
     scripts/fixtures/wake_match_cases.json (их же гоняет WakeMatchTest.java),
     список имён из /api/personas, кириллица для подсказки распознавателю;
  B. автомат слушателя (app/voice/wake_listener) с поддельными ядром и
     озвучкой: имя с просьбой, имя без просьбы → ожидание, таймаут, ответ
     с вопросом → ответ без имени, сбои ядра, микрофон глушится на ответ;
  C. текст для озвучки, подсказка имён, выбор микрофона;
  D. настоящий распознаватель (sherpa-onnx + модель в data/models/wake-ru)
     на фразах, синтезированных say: от звука до отправленной просьбы.
     Нет модуля, модели или say — пропуск;
  E. ядро: без WAKE_LISTEN слушатель не стартует, GET /api/voice/wake,
     голосовая просьба, которую уже взял другой слушатель, — duplicate;
  F. весь цикл слушателя с поддельным микрофоном (sounddevice подменён):
     сценарий из нескольких фраз → просьбы в ядро → озвучка, свой ответ
     «Коннор, …» из колонок не будит, нули запрета macOS, микрофон пропал.

Микрофон, LLM и сеть не используются.
Запуск: PYTHONPATH=. python3 -m scripts.test_wake_word
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="wake_word_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"
os.environ.pop("WAKE_LISTEN", None)

from app.voice import wake_listener as wl  # noqa: E402
from app.voice.wake_match import (  # noqa: E402
    match_wake, phonetic_key, to_cyrillic, wake_phrases,
)

CASES = json.loads((ROOT / "scripts" / "fixtures" / "wake_match_cases.json").read_text("utf-8"))
PHRASES = wake_phrases(CASES["personas"])

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n{title}")


# ===== A =====

def test_match():
    section("A. Имя в распознанном тексте")
    for word, key in CASES["keys"]:
        check(f"ключ «{word}» → {key}", phonetic_key(word) == key)
    for c in CASES["cases"]:
        hit = match_wake(c["text"], PHRASES)
        if c["persona"] is None:
            check(f"«{c['text']}» — не имя", hit is None)
        else:
            check(f"«{c['text']}» → {c['persona']} / «{c['command']}»",
                  hit is not None and hit.persona == c["persona"] and hit.command == c["command"])
    ids = {p.persona for p in PHRASES}
    check("персона без trigger_words голосом не будится", "assistant" not in ids)
    check("одинаковое по звуку имя — первой персоне", "arrodes_master" not in ids)
    check("arrodes и арродес — одно имя", sum(p.persona == "arrodes" and p.key == "арадис" for p in PHRASES) == 1)
    only = wake_phrases(CASES["personas"], only=["axel"])
    check("WAKE_PERSONAS ограничивает список", {p.persona for p in only} == {"axel"})
    check("пустые и короткие слова пропускаются",
          wake_phrases([{"id": "x", "features": {"trigger_words": ["", " ", "ок", None]}}]) == [])
    check("кириллица для подсказки: Connor → коннор", to_cyrillic("Connor") == "коннор")
    check("кириллица для подсказки: «Великий Арродес!»", to_cyrillic("Великий Арродес!") == "великий арродес")
    check("пустой список имён — ничего", match_wake("коннор привет", []) is None)


# ===== B =====

class FakeClient:
    def __init__(self, reply="Солнечно, плюс двенадцать.", error=None):
        self.reply = reply
        self.error = error
        self.sent = []

    def personas(self):
        return CASES["personas"]

    def chat(self, persona, message):
        self.sent.append((persona, message))
        if self.error:
            raise self.error
        return self.reply


class FakeVoice:
    def __init__(self):
        self.said = []

    def speak(self, text):
        self.said.append(text)

    def stop(self):
        pass


chimes = []
wl._chime = lambda kind="wake": chimes.append(kind)


def make(client=None):
    lst = wl.WakeListener(client or FakeClient(), voice=FakeVoice())
    lst.phrases = list(PHRASES)
    lst._phrases_at = time.monotonic()
    return lst


def run_final(lst, text):
    todo = lst.on_final(text)
    if todo:
        lst.handle(*todo)
    return todo


def test_machine():
    section("B. Автомат слушателя")
    chimes.clear()
    lst = make()
    lst.on_partial("коннор какая")
    check("имя в промежуточном результате — сигнал сразу", chimes == ["wake"])
    lst.on_partial("коннор какая сегодня")
    todo = run_final(lst, "коннор какая сегодня погода")
    check("второго сигнала «слушаю» на ту же фразу нет", chimes.count("wake") == 1)
    check("имя с просьбой — просьба уходит персоне", todo == ("connor", "какая сегодня погода"))
    check("ответ озвучен", lst.voice.said == ["Солнечно, плюс двенадцать."])
    check("после ответа без вопроса — снова ждём имя", lst.state == wl.IDLE)

    chimes.clear()
    lst = make()
    check("имя без просьбы ничего не шлёт", run_final(lst, "коннер") is None)
    check("…сигнал, хоть промежуточного не было", chimes == ["wake"])
    check("…ждём просьбу", lst.state == wl.AWAIT and lst.persona == "connor")
    todo = run_final(lst, "поставь будильник на семь")
    check("следующая фраза — просьба той же персоне", todo == ("connor", "поставь будильник на семь"))

    lst = make()
    run_final(lst, "коннор")
    todo = run_final(lst, "родис расскажи сказку")
    check("в ожидании назвали другую персону — просьба ей", todo == ("arrodes", "расскажи сказку"))

    lst = make()
    run_final(lst, "коннор")
    run_final(lst, "жабка")
    check("в ожидании снова только имя — ждём дальше", lst.state == wl.AWAIT and lst.client.sent == [])

    chimes.clear()
    lst = make()
    run_final(lst, "коннор")
    lst._deadline = time.monotonic() - 1
    lst.on_tick()
    check("просьбы не дождались — к ожиданию имени", lst.state == wl.IDLE and lst.persona is None)
    check("…с сигналом", chimes[-1] == "timeout")

    lst = make()
    run_final(lst, "коннор")
    lst._deadline = time.monotonic() + 0.5
    lst.on_partial("поставь")
    check("человек ещё говорит — ожидание продлено", lst._deadline > time.monotonic() + 1.5)

    lst = make()
    check("обычная речь ничего не будит", run_final(lst, "мы вчера ходили в кино") is None
          and lst.state == wl.IDLE and lst.client.sent == [])
    check("пустая фраза ничего не делает", run_final(lst, "  ") is None)

    lst = make(FakeClient(reply="А тебе зачем?"))
    run_final(lst, "коннор какая погода")
    check("ответ с вопросом — ждём ответ без имени", lst.state == wl.AWAIT and lst.persona == "connor")
    todo = run_final(lst, "просто интересно")
    check("…и он уходит той же персоне", todo == ("connor", "просто интересно"))

    chimes.clear()
    lst = make(FakeClient(reply=None))
    run_final(lst, "коннор привет")
    check("дубль (фразу взял телефон) — молчим, ждём имя",
          lst.voice.said == [] and lst.state == wl.IDLE and "error" not in chimes)
    check("после отправки — сигнал «принято»", chimes == ["wake", "sent"])

    import httpx
    lst = make(FakeClient(error=httpx.ConnectError("refused")))
    run_final(lst, "коннор привет")
    check("ядро не отвечает — так и сказано", lst.voice.said == ["Ядро не отвечает."])
    check("…и снова ждём имя", lst.state == wl.IDLE and lst.error)
    resp = httpx.Response(409, request=httpx.Request("POST", "http://x/api/chat"))
    lst = make(FakeClient(error=httpx.HTTPStatusError("muted", request=resp.request, response=resp)))
    run_final(lst, "коннор привет")
    check("замороженная персона — понятная фраза", lst.voice.said == ["Персона сейчас заморожена и молчит."])
    resp = httpx.Response(401, request=httpx.Request("POST", "http://x/api/chat"))
    lst = make(FakeClient(error=httpx.HTTPStatusError("auth", request=resp.request, response=resp)))
    run_final(lst, "коннор привет")
    check("неверный токен — «нет доступа»", lst.voice.said == ["Нет доступа к ядру."])

    # Пока персона отвечает, кадры микрофона не копятся
    import numpy as np
    lst = make()
    lst.state = wl.BUSY
    lst._on_audio(np.zeros((1600, 1), np.float32), 1600, None, None)
    check("во время ответа микрофон глушится", lst._audio.empty())
    lst.state = wl.IDLE
    lst._on_audio(np.zeros((1600, 1), np.float32), 1600, None, None)
    check("в ожидании имени кадры идут в распознавание", lst._audio.qsize() == 1)
    lst._stop.set()
    lst._on_audio(np.zeros((1600, 1), np.float32), 1600, None, None)
    check("после стопа кадры не принимаются", lst._audio.qsize() == 1)

    lst = make()
    run_final(lst, "коннор")
    lst.hold(60)
    check("пауза (открыт голосовой режим чата): ожидание просьбы сброшено", lst.held and lst.state == wl.IDLE)
    lst._on_audio(np.zeros((1600, 1), np.float32), 1600, None, None)
    check("…кадры микрофона не идут в распознавание", lst._audio.empty())
    lst.hold(10_000)
    check("…не дольше HOLD_MAX_SEC", lst._held_until - time.monotonic() <= wl.HOLD_MAX_SEC + 0.1)
    lst.hold(0)
    check("пауза снята — снова слушаем", not lst.held)

    st = make().status()
    check("состояние для /api/voice/wake", st["state"] == wl.IDLE and "коннор" in st["names"])


# ===== C =====

def test_helpers():
    section("C. Озвучка, подсказка, микрофон")
    s = wl.speech_text("**Готово!** Ссылка: https://example.com/x?a=1 😀 `код` [тут]")
    check("ссылки, разметка, эмодзи не читаются вслух", s == "Готово! Ссылка: код тут")
    check("язык озвучки: латиница", wl._is_latin("Sunny, twelve degrees"))
    check("язык озвучки: кириллица", not wl._is_latin("Солнечно, plus 12"))
    hw = wl.hotwords_for(PHRASES)
    check("подсказка: имена кириллицей, через /", hw.split("/")[0] == "коннор :2.0" and "арродес :2.0" in hw
          and "миррор :2.0" in hw and "аксел :2.0" in hw)

    class SD:
        def __init__(self, devs):
            self.devs = devs

        def query_devices(self):
            return self.devs

    devs = [{"name": "BlackHole 2ch", "max_input_channels": 2},
            {"name": "Микрофон MacBook Pro", "max_input_channels": 1},
            {"name": "Soundcore Life Q20+", "max_input_channels": 1}]
    old = os.environ.pop("WAKE_DEVICE", None)
    check("по умолчанию — встроенный микрофон, не гарнитура", wl.pick_input_device(SD(devs)) == 1)
    check("встроенного нет — системный", wl.pick_input_device(SD(devs[::2])) is None)
    os.environ["WAKE_DEVICE"] = "Soundcore"
    check("WAKE_DEVICE — по части имени", wl.pick_input_device(SD(devs)) == "Soundcore")
    os.environ["WAKE_DEVICE"] = "2"
    check("WAKE_DEVICE — по номеру", wl.pick_input_device(SD(devs)) == 2)
    os.environ["WAKE_DEVICE"] = "default"
    check("WAKE_DEVICE=default — системный", wl.pick_input_device(SD(devs)) is None)
    os.environ.pop("WAKE_DEVICE", None)
    if old is not None:
        os.environ["WAKE_DEVICE"] = old


# ===== D =====

SPOKEN = [
    ("Коннор, какая сегодня погода?", ("connor", "какая сегодня погода")),
    ("Жабка, поставь напоминание на пять вечера", ("connor", "поставь напоминание на пять вечера")),
    ("Арродес, расскажи что-нибудь", ("arrodes", "расскажи что-нибудь")),
    ("Мы вчера ходили в кино, и было скучно", None),
]


def test_real():
    section("D. Настоящий распознаватель на синтезированной речи")
    try:
        import numpy as np
        import sherpa_onnx  # noqa: F401
    except Exception:
        print("  (пропуск — нет sherpa-onnx)")
        return
    model = Path(os.getenv("WAKE_MODEL_DIR") or ROOT / "data" / "models" / "wake-ru")
    if not all((model / f).is_file() for f in wl.MODEL_FILES):
        print(f"  (пропуск — нет модели в {model})")
        return
    if not shutil.which("say"):
        print("  (пропуск — нет say)")
        return
    rec = wl.make_recognizer(model)
    tmp = Path(tempfile.mkdtemp(prefix="wake_wav_"))
    for i, (phrase, want) in enumerate(SPOKEN):
        wav = tmp / f"p{i}.wav"
        subprocess.run(["say", "-v", "Milena", "-o", str(wav), "--data-format=LEI16@16000", phrase], check=True)
        with wave.open(str(wav)) as f:
            x = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        # Как с микрофона: куски по 100 мс, потом тишина до конца фразы
        x = np.concatenate([np.zeros(4800, np.float32), x, np.zeros(32000, np.float32)])
        lst = make()
        lst.handle = lambda p, m, lst=lst: lst.client.sent.append((p, m))
        dec = wl.Decoder(rec, lst.phrases)
        t = time.time()
        for k in range(0, len(x), 1600):
            lst.process(dec, 16000, x[k:k + 1600])
        took = time.time() - t
        sent = lst.client.sent
        if want is None:
            check(f"«{phrase}» — никого не будит (слышно: «{lst.last_heard}»)", sent == [])
        else:
            check(f"«{phrase}» → {sent[0] if sent else '—'} (за {took:.2f} с)", sent == [want])
    shutil.rmtree(tmp, ignore_errors=True)


# ===== F =====

class FakeStream:
    """Поддельный микрофон: отдаёт сценарий кусками по 100 мс в 2 раза быстрее
    жизни, потом — тихий шум (не нули). inject — то, что «услышал» микрофон
    из колонок (озвучка ответа)."""

    def __init__(self, sd, callback, blocksize):
        self.sd = sd
        self.callback = callback
        self.block = blocksize
        self.alive = False

    def __enter__(self):
        import threading
        self.alive = True
        self.thread = threading.Thread(target=self._play, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.alive = False
        self.thread.join(2)

    def _play(self):
        import numpy as np
        rng = np.random.default_rng(1)
        sd = self.sd
        while self.alive:
            if sd.stall_after is not None and sd.sent >= sd.stall_after:
                time.sleep(0.05)
                continue
            if sd.inject:
                buf = sd.inject
            elif sd.script:
                buf = sd.script
            else:
                buf = None
            if buf:
                chunk = buf.pop(0)
            elif sd.zeros:
                chunk = np.zeros(self.block, np.float32)
            else:
                chunk = (rng.standard_normal(self.block) * 1e-4).astype(np.float32)
            sd.sent += len(chunk)
            self.callback(chunk.reshape(-1, 1), len(chunk), None, None)
            time.sleep(len(chunk) / 16000 / 2)


class FakeSD:
    def __init__(self, script=None, zeros=False, stall_after=None):
        self.script = script or []
        self.inject = []
        self.zeros = zeros
        self.stall_after = stall_after
        self.sent = 0
        self.opened = 0

    def _terminate(self):
        pass

    def _initialize(self):
        pass

    def query_devices(self, device=None, kind=None):
        devs = [{"name": "Микрофон MacBook Pro", "max_input_channels": 1, "default_samplerate": 16000.0}]
        return devs if kind is None else devs[0]

    def InputStream(self, device, channels, samplerate, dtype, blocksize, callback):
        self.opened += 1
        if self.stall_after is not None and self.opened > 1:
            self.stall_after = None
        return FakeStream(self, callback, blocksize)


def _chunks(x, block=1600):
    return [x[k:k + block] for k in range(0, len(x), block)]


def test_loop():
    section("F. Весь цикл: поддельный микрофон → ядро → озвучка")
    try:
        import numpy as np
        import sherpa_onnx  # noqa: F401
    except Exception:
        print("  (пропуск — нет sherpa-onnx)")
        return
    model = Path(os.getenv("WAKE_MODEL_DIR") or ROOT / "data" / "models" / "wake-ru")
    if not all((model / f).is_file() for f in wl.MODEL_FILES) or not shutil.which("say"):
        print("  (пропуск — нет модели или say)")
        return
    os.environ["WAKE_MODEL_DIR"] = str(model)
    tmp = Path(tempfile.mkdtemp(prefix="wake_loop_"))

    def clip(text):
        wav = tmp / f"c{len(list(tmp.iterdir()))}.wav"
        subprocess.run(["say", "-v", "Milena", "-o", str(wav), "--data-format=LEI16@16000", text], check=True)
        with wave.open(str(wav)) as f:
            return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768

    def gap(sec):
        return np.full(int(16000 * sec), 1e-4, np.float32)

    script = np.concatenate([
        gap(0.5), clip("Коннор, поставь будильник на семь утра"), gap(2.5),
        clip("Мы вчера ходили в кино"), gap(2.0),
        clip("Арродес"), gap(1.2), clip("Расскажи сказку"), gap(2.5),
        clip("Коннор, какая сегодня погода?"), gap(2.5),
        clip("Просто интересно"), gap(2.5),
    ])
    echo = clip("Коннор, готово, будильник стоит")

    class Client(FakeClient):
        def chat(self, persona, message):
            self.sent.append((persona, message))
            return "А тебе зачем?" if "погода" in message else "Готово."

    sd = FakeSD(_chunks(script))

    class EchoVoice(FakeVoice):
        # Ответ «звучит из колонок» и попадает в микрофон: слушатель в это
        # время глушит кадры и не будит сам себя на «Коннор, …» в ответе
        def speak(self, text):
            self.said.append(text)
            sd.inject = _chunks(echo.copy())
            while sd.inject:
                time.sleep(0.02)

    old_sd = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = sd
    try:
        lst = wl.WakeListener(Client(), voice=EchoVoice())
        lst.start()
        t0 = time.time()
        while sd.script and time.time() - t0 < 60:
            time.sleep(0.2)
        time.sleep(1.5)
        sent = list(lst.client.sent)
        want = [
            ("connor", "поставь будильник на семь утра"),
            ("arrodes", "расскажи сказку"),
            ("connor", "какая сегодня погода"),
            ("connor", "просто интересно"),
        ]
        # Слова просьбы — все на месте; лишний союз распознавателя («расскажи
        # и сказку» на стыке тишины) — не ошибка слушателя
        check(f"просьбы дошли до ядра: {sent}", len(sent) == len(want) and all(
            gp == wp and set(wm.split()) <= set(gm.split()) for (gp, gm), (wp, wm) in zip(sent, want)))
        check("ответы озвучены", lst.voice.said == ["Готово.", "Готово.", "А тебе зачем?", "Готово."])
        check("свой ответ «Коннор, …» не разбудил слушатель", len(sent) == 4)
        check("слушатель жив и ждёт имя", lst.running and lst.state == wl.IDLE and not lst.error)
        wl._listener = lst
        wl.stop_with_core()
        check("остановка ядра ждёт, пока слушатель закроет микрофон (иначе bus error в CloseStream)",
              not lst.running)
        wl._listener = None

        sd = FakeSD(zeros=True)
        sys.modules["sounddevice"] = sd
        lst = wl.WakeListener(Client(), voice=FakeVoice())
        lst.start()
        t0 = time.time()
        while lst.error != wl.NO_MIC_ACCESS and time.time() - t0 < 10:
            time.sleep(0.1)
        check("ровные нули — подсказка про доступ к микрофону", lst.error == wl.NO_MIC_ACCESS)
        check("…видна в состоянии", lst.status()["error"] == wl.NO_MIC_ACCESS)
        lst.stop()
        lst._thread.join(5)

        old_stall, old_wait = wl.STALL_SEC, None
        wl.STALL_SEC = 0.5
        sd = FakeSD(stall_after=16000)
        sys.modules["sounddevice"] = sd
        lst = wl.WakeListener(Client(), voice=FakeVoice())
        lst._stop.wait = lambda t=None, ev=lst._stop: threading_wait(ev, min(t or 0, 0.2))
        lst.start()
        t0 = time.time()
        while sd.opened < 2 and time.time() - t0 < 10:
            time.sleep(0.1)
        check("микрофон замолчал — открыт заново", sd.opened >= 2 and lst.running)
        lst.stop()
        lst._thread.join(5)
        wl.STALL_SEC = old_stall
    finally:
        if old_sd is not None:
            sys.modules["sounddevice"] = old_sd
        else:
            sys.modules.pop("sounddevice", None)
        shutil.rmtree(tmp, ignore_errors=True)


def threading_wait(ev, t):
    import threading
    return threading.Event.wait(ev, t)


# ===== E =====

def test_core():
    section("E. Ядро")
    check("без WAKE_LISTEN слушатель не стартует", wl.start_with_core("127.0.0.1", 9) is None)
    # Ядро в другом venv, без модулей: причина — в /api/voice/wake, не только в логе
    os.environ["WAKE_LISTEN"] = "1"
    saved = sys.modules.get("sherpa_onnx")
    sys.modules["sherpa_onnx"] = None  # import → ImportError
    try:
        check("нет модулей — слушатель не стартует", wl.start_with_core("127.0.0.1", 9) is None)
        check("…и причина с командой установки", "pip install" in wl.start_error()
              and sys.executable in wl.start_error())
    finally:
        if saved is not None:
            sys.modules["sherpa_onnx"] = saved
        else:
            sys.modules.pop("sherpa_onnx", None)
        os.environ.pop("WAKE_LISTEN", None)
    os.environ["WAKE_LISTEN"] = "yes"
    check("WAKE_LISTEN=yes — включено", wl.enabled_by_env())
    os.environ.pop("WAKE_LISTEN")
    from fastapi.testclient import TestClient
    from app.api import server
    # Импорт сервера читает .env — там может стоять WAKE_LISTEN=1 (своё ядро)
    os.environ.pop("WAKE_LISTEN", None)
    c = TestClient(server.app)
    tok = getattr(server, "_api_token", "")
    r = c.get("/api/voice/wake", headers={"Authorization": f"Bearer {tok}"} if tok else {})
    check("GET /api/voice/wake без слушателя — с причиной", r.status_code == 200
          and r.json()["running"] is False and "pip install" in r.json().get("error", ""))
    wl._start_error = ""
    r = c.get("/api/voice/wake", headers={"Authorization": f"Bearer {tok}"} if tok else {})
    check("GET /api/voice/wake без слушателя", r.json() == {"enabled": False, "running": False})
    calls = []

    async def fake_turn(bot, req, request):
        calls.append(req.message)
        return server.ChatResponse(reply="ок", persona=req.persona, chat_id="web_user")

    class Bot:
        features = {}

        def control_mode_on(self, chat_id):
            return False

    async def fake_get_bot(name):
        return Bot()

    orig = server._chat_turn, server._get_bot
    server._chat_turn, server._get_bot = fake_turn, fake_get_bot
    hdr = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        body = {"persona": "connor", "message": "какая погода", "voice": True}
        server._voice_turns.clear()
        server._voice_turns["connor"] = [1, 0.0]  # просьба с ноутбука ещё в работе
        r = c.post("/api/chat", json=body, headers=hdr)
        check("голосовая просьба, пока идёт такая же, — duplicate", r.status_code == 200
              and r.json()["duplicate"] is True and r.json()["reply"] == "" and calls == [])
        server._voice_turns.clear()
        r = c.post("/api/chat", json=body, headers=hdr)
        check("первая голосовая просьба — обычный ответ", r.json()["duplicate"] is False and calls == ["какая погода"])
        r = c.post("/api/chat", json=body, headers=hdr)
        check("сразу после неё (< VOICE_DUP_SEC) — duplicate", r.json()["duplicate"] is True and len(calls) == 1)
        r = c.post("/api/chat", json={**body, "voice": False}, headers=hdr)
        check("не голосовая — без проверки", r.json()["duplicate"] is False and len(calls) == 2)
        server._voice_turns["connor"][1] -= server.VOICE_DUP_SEC + 1
        r = c.post("/api/chat", json=body, headers=hdr)
        check("через VOICE_DUP_SEC — снова обычная", r.json()["duplicate"] is False and len(calls) == 3)
        check("счётчик просьб в работе вернулся к нулю", server._voice_turns["connor"][0] == 0)
        r = c.post("/api/voice/wake/hold", json={"seconds": 30}, headers=hdr)
        check("пауза без слушателя — не ошибка", r.status_code == 200 and r.json() == {"held": False})
        lst = make()
        wl._listener = lst
        r = c.post("/api/voice/wake/hold", json={"seconds": 30}, headers=hdr)
        check("пауза слушателю через API", r.json() == {"held": True} and lst.held)
        check("пауза видна в /api/voice/wake", c.get("/api/voice/wake", headers=hdr).json()["held"] is True)
        c.post("/api/voice/wake/hold", json={"seconds": 0}, headers=hdr)
        check("снять паузу через API", not lst.held)
        r = c.post("/api/voice/wake/hold", json={"seconds": -1}, headers=hdr)
        check("отрицательная пауза — 422", r.status_code == 422)
        wl._listener = None
    finally:
        server._chat_turn, server._get_bot = orig
        server._voice_turns.clear()
    src = (ROOT / "app" / "main.py").read_text("utf-8")
    check("run_api поднимает и гасит слушатель",
          "wake_listener.start_with_core(host, port)" in src and "wake_listener.stop_with_core()" in src)


def main():
    test_match()
    test_machine()
    test_helpers()
    test_real()
    test_loop()
    test_core()
    print(f"\n{ok - failures}/{ok} OK" + (f", FAIL: {failures}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
