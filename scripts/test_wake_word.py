"""Голосовая активация на компьютере ядра: «Коннор, какая погода?».

  A. имя в распознанном тексте (app/voice/wake_match): общие примеры
     scripts/fixtures/wake_match_cases.json (их же гоняет WakeMatchTest.java),
     список имён из /api/personas, кириллица для подсказки распознавателю;
  B. автомат слушателя (app/voice/wake_listener) с поддельными ядром и
     озвучкой: имя с просьбой, имя без просьбы → ожидание, таймаут, ответ
     с вопросом → ответ без имени, сбои ядра, микрофон глушится на ответ;
     разговор у портрета (слушатель панели): портрет на имени, если чат
     персоны не открыт, текст фразы по ходу речи, дальше без имени до
     тишины, «хватит», «пока», шум, другая персона, просьба панели закончить;
  B4. пауза, чтобы подумать: фраза уходит после WAKE_TALK_PAUSE_SEC тишины,
     продолжение дописывается, «хватит» — сразу, «подожди» — ждём дальше;
  B3. несколько персон в разговоре (обе на столе, слушает названная), имя
     по ходу фразы, убрали портрет; перебить: её голос в микрофоне узнаётся
     по словам ответа, ваши поверх — замолкает, «подожди», «хватит»;
  C. текст для озвучки, подсказка имён, выбор микрофона;
  D. настоящий распознаватель (sherpa-onnx + модель в data/models/wake-ru)
     на фразах, синтезированных say: от звука до отправленной просьбы.
     Нет модуля, модели или say — пропуск;
  D2. перебить на настоящем распознавателе: её ответ «из колонок» (полоса,
     отражения, шум) — сам себя не перебивает; ваша фраза поверх — перебивает;
  E. ядро: без WAKE_LISTEN слушатель не стартует, GET /api/voice/wake,
     голосовая просьба, которую уже взял другой слушатель, — duplicate;
  F. весь цикл слушателя с поддельным микрофоном (sounddevice подменён):
     сценарий из нескольких фраз → просьбы в ядро → озвучка, свой ответ
     «Коннор, …» из колонок не будит, перебить посреди ответа (её голос и ваш
     в микрофоне одновременно), нули запрета macOS, микрофон пропал;
  H. фильтр фона «кто ближе к микрофону» (app/voice/near_gate): тихое —
     тишиной, начало слова и хвост целы, щелчок не открывает, мягче до
     имени; «Запомнить мой голос», громкость по имени и по фразам, на
     микрофон; команды панели. H2 — весь цикл на настоящем распознавателе:
     толпа на 15 дБ тише вас — без фильтра её слова во фразе, с фильтром
     нет, фраза кончается, когда замолчали вы;
  G. слушатель панели в трее (отдельный процесс, main()): сообщает ядру о
     себе и берёт паузу голосового чата веба, печатает состояние для панели,
     останавливается по SIGTERM; слушатель ядра уступает ему микрофон;
     без модулей — код 2 и причина.

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
# Цикл слушателя с двумя языками качал бы английскую модель во временную
# папку — по умолчанию только русский; английский — в своих проверках (I)
os.environ["WAKE_LANG"] = "ru"

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
        self.kw = []
        self.cancels = []

    def personas(self):
        return CASES["personas"]

    def chat(self, persona, message, **kw):
        self.sent.append((persona, message))
        self.kw.append(kw)
        if self.error:
            raise self.error
        return self.reply

    def cancel(self, persona):
        self.cancels.append(persona)
        return 1


class FakeVoice:
    def __init__(self):
        self.said = []

    def speak(self, text, alive=None):
        if alive is None or alive():
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


def talk_of(lst):
    t = lst.status()["talk"]
    return t and (t["persona"], t["phase"], t["text"], t["sent"])


def test_talk():
    section("B2. Разговор у портрета на рабочем столе")
    # Здесь — только автомат: хвоста её голоса в микрофоне нет (поддельная
    # озвучка звучит мгновенно); перебивание — в B3
    saved = wl.ECHO_TAIL_SEC, wl.TALK_PAUSE_SEC
    wl.ECHO_TAIL_SEC = 0.0
    # Фраза — сразу, без паузы на «подумать» (её проверяет B4)
    wl.TALK_PAUSE_SEC = 0.0
    try:
        _test_talk()
    finally:
        wl.ECHO_TAIL_SEC, wl.TALK_PAUSE_SEC = saved


def _test_talk():
    chimes.clear()
    lst = make()
    lst.on_partial("коннор какая")
    check("слушатель ядра (без портрета) — разговора нет", lst.status()["talk"] is None)

    lst = make()
    lst.with_portrait = True
    lst.on_partial("коннор")
    check("имя в промежуточном тексте — разговор сразу, текст фразы виден",
          talk_of(lst) == ("connor", wl.TALK_HEAR, "коннор", ""))
    lst.changed.clear()
    lst.on_partial("коннор какая сегодня")
    check("…текст растёт по ходу речи", talk_of(lst)[2] == "коннор какая сегодня" and lst.changed.is_set())
    todo = lst.on_final("коннор какая сегодня погода")
    check("конец фразы — просьба без имени, на портрете — фраза целиком",
          todo == ("connor", "какая сегодня погода")
          and talk_of(lst) == ("connor", wl.TALK_THINK, "коннор какая сегодня погода", "какая сегодня погода"))
    phases = []
    lst.voice.speak = lambda text, alive=None: phases.append(lst.status()["talk"]["phase"])
    lst.handle(*todo)
    check("ответ озвучивается в фазе «говорит»", phases == [wl.TALK_SPEAK])
    check("после ответа — без имени, долго (CONVERSE_SEC)",
          lst.state == wl.AWAIT and lst._deadline - time.monotonic() > wl.CONVERSE_SEC - 2
          and talk_of(lst) == ("connor", wl.TALK_LISTEN, "", ""))
    lst.on_partial("а завтра")
    check("следующая фраза без имени — видна по ходу речи", talk_of(lst) == ("connor", wl.TALK_HEAR, "а завтра", ""))
    todo = lst.on_final("а завтра")
    check("…и уходит той же персоне", todo == ("connor", "а завтра") and talk_of(lst)[1] == wl.TALK_THINK)
    lst.handle(*todo)
    check("ответ без вопроса — разговор всё равно идёт", lst.state == wl.AWAIT and lst.talking)
    lst.on_partial("м")
    check("«м» — шум, не реплика", lst.on_final("м") is None and lst.state == wl.AWAIT
          and talk_of(lst) == ("connor", wl.TALK_LISTEN, "", ""))
    check("пустая фраза в разговоре ничего не шлёт", lst.on_final("") is None and lst.talking)
    todo = lst.on_final("родис расскажи сказку")
    check("назвали другую персону — разговор с ней", todo == ("arrodes", "расскажи сказку")
          and talk_of(lst)[0] == "arrodes")
    lst.handle(*todo)
    lst.on_final("коннор")
    check("снова только имя — разговор с ним, без отправки",
          lst.state == wl.AWAIT and talk_of(lst) == ("connor", wl.TALK_LISTEN, "", "")
          and lst.client.sent[-1] == ("arrodes", "расскажи сказку"))
    chimes.clear()
    lst._deadline = time.monotonic() + 0.5
    lst.on_partial("ну")
    check("пока говорите — разговор не кончается", lst._deadline > time.monotonic() + 1.5)
    lst.on_final("")
    lst._deadline = time.monotonic() - 1
    lst.on_tick()
    check("тишина CONVERSE_SEC — разговор кончился, снова ждём имя",
          lst.state == wl.IDLE and not lst.talking and chimes == ["timeout"])

    lst = make()
    lst.with_portrait = True
    run_final(lst, "коннор")
    check("имя без просьбы — разговор и ожидание CONVERSE_SEC",
          lst.talking and lst._deadline - time.monotonic() > wl.CONVERSE_SEC - 2)
    sent = len(lst.client.sent)
    chimes.clear()
    check("«ладно, хватит» — конец без отправки", run_final(lst, "ладно хватит") is None
          and not lst.talking and lst.state == wl.IDLE and len(lst.client.sent) == sent and chimes == ["timeout"])

    lst = make()
    lst.with_portrait = True
    run_final(lst, "коннор привет")
    todo = run_final(lst, "ну пока")
    check("«ну пока» уходит персоне — пусть попрощается", todo == ("connor", "ну пока"))
    check("…и после её ответа разговор кончился", lst.state == wl.IDLE and not lst.talking)

    lst = make()
    lst.with_portrait = True
    lst.set_chats(["connor"])
    run_final(lst, "коннор привет")
    check("чат персоны открыт в вебе — без портрета, как раньше", lst.status()["talk"] is None
          and lst.state == wl.IDLE)
    run_final(lst, "родис привет")
    check("…чат другой персоны — у неё портрет", lst.talking and talk_of(lst)[0] == "arrodes")
    lst._chats_at -= wl.CHATS_FRESH_SEC + 1
    check("устаревший список чатов — не в счёт", not lst.chat_open("connor"))

    lst = make()
    lst.with_portrait = True
    lst.on_partial("коннер")
    check("имя послышалось в промежуточном тексте…", lst.talking)
    lst.on_final("конечно")
    check("…а фраза без имени — портрет не нужен", not lst.talking and lst.state == wl.IDLE)

    lst = make()
    lst.with_portrait = True
    run_final(lst, "коннор")
    lst.end_talk()
    check("просьба панели — выполняет поток слушателя", lst.talking and lst._talk_end_req)
    lst.on_tick()
    check("…разговор кончился, ждём имя", not lst.talking and lst.state == wl.IDLE)
    lst.end_talk()
    check("без разговора просьба ничего не оставляет", not lst._talk_end_req)

    lst = make()
    lst.with_portrait = True
    todo = lst.on_final("коннор какая погода")
    stopped = []
    lst.voice.stop = lambda: stopped.append(1)
    real_chat = lst.client.chat

    def chat_then_end(persona, message):
        lst.end_talk()  # портрет убрали, пока персона думала
        return real_chat(persona, message)

    lst.client.chat = chat_then_end
    lst.handle(*todo)
    check("портрет убрали во время ответа — не договаривает, разговор кончился",
          lst.voice.said == [] and stopped and not lst.talking and lst.state == wl.IDLE)

    lst = make(FakeClient(reply="А тебе зачем?"))
    lst.with_portrait = True
    run_final(lst, "коннор какая погода")
    check("ответ с вопросом в разговоре — тоже CONVERSE_SEC",
          lst._deadline - time.monotonic() > wl.CONVERSE_SEC - 2)

    lst = make(FakeClient(reply=None))
    lst.with_portrait = True
    run_final(lst, "коннор привет")
    check("фразу взял телефон — разговор у портрета кончился", not lst.talking and lst.state == wl.IDLE)

    import httpx
    lst = make(FakeClient(error=httpx.ConnectError("refused")))
    lst.with_portrait = True
    run_final(lst, "коннор привет")
    check("ядро не отвечает — сказано, разговор кончился",
          lst.voice.said == ["Ядро не отвечает."] and not lst.talking and lst.state == wl.IDLE)

    lst = make()
    lst.with_portrait = True
    lst.on_partial("коннор какая")
    lst.hold(60)
    check("голосовой режим веба посреди фразы — разговор кончился", not lst.talking)
    lst = make()
    lst.with_portrait = True
    run_final(lst, "коннор")
    lst.hold(60)
    check("…и в ожидании фразы", not lst.talking and lst.state == wl.IDLE)

    saved = wl.CONVERSE_SEC
    wl.CONVERSE_SEC = 0
    try:
        lst = make()
        lst.with_portrait = True
        run_final(lst, "коннор")
        check("WAKE_CONVERSE_SEC=0 — без разговора у портрета", not lst.talking
              and lst._deadline - time.monotonic() <= wl.AWAIT_SEC + 0.1)
    finally:
        wl.CONVERSE_SEC = saved

    for text, want in [("хватит", wl.TALK_STOP), ("Всё, спасибо!", wl.TALK_STOP), ("ну ладно хватит", wl.TALK_STOP),
                       ("пока", wl.TALK_BYE), ("до свидания", wl.TALK_BYE), ("спокойной ночи", wl.TALK_BYE),
                       ("пока не знаю", None), ("хватит ли денег", None), ("хорошо", None), ("спасибо", None)]:
        check(f"«{text}» → {want}", wl.talk_end(text) == want)
    st = make().status()
    check("состояние без разговора — talk: None", "talk" in st and st["talk"] is None)


def personas_of(lst):
    t = lst.status()["talk"]
    return t and (t["persona"], t["personas"])


class SpeakingVoice:
    """Озвучка, которая звучит, пока её не оборвут (stop) или не отпустят
    (done): ответ идёт в потоке хода, автомат в это время слушает."""

    def __init__(self):
        import threading
        self.said = []
        self.stopped = 0
        self.go = threading.Event()
        self.speaking = threading.Event()

    def speak(self, text, alive=None):
        if alive is not None and not alive():
            return
        self.said.append(text)
        self.go.clear()
        self.speaking.set()
        self.go.wait(5)
        self.speaking.clear()

    def stop(self):
        self.stopped += 1
        self.go.set()

    def done(self):
        self.go.set()


def speaking_turn(lst, text):
    """Фраза → ход в своём потоке (как process в разговоре) → она говорит."""
    import threading
    todo = lst.on_final(text)
    assert todo, text
    lst.state, lst.persona = wl.BUSY, todo[0]
    th = threading.Thread(target=lst.handle, args=todo, daemon=True)
    th.start()
    lst.voice.speaking.wait(3)
    return th


def test_barge():
    section("B3. Разговор с несколькими персонами и перебивание")
    saved_tail, saved_pause = wl.ECHO_TAIL_SEC, wl.TALK_PAUSE_SEC
    wl.ECHO_TAIL_SEC = 0.0
    wl.TALK_PAUSE_SEC = 0.0
    try:
        lst = make()
        lst.with_portrait = True
        run_final(lst, "коннор какая погода")
        check("в разговоре одна персона", personas_of(lst) == ("connor", ["connor"]))
        todo = run_final(lst, "родис как провела вчера время")
        check("назвали другую — слушает она, и обе в разговоре (обе на столе)",
              todo == ("arrodes", "как провела вчера время") and personas_of(lst) == ("arrodes", ["connor", "arrodes"]))
        todo = run_final(lst, "тебе понравился подарок")
        check("дальше без имени — только ей", todo == ("arrodes", "тебе понравился подарок"))
        todo = run_final(lst, "коннор ты как вечер провела")
        check("снова назвали первую — слушает она, вторая на столе ждёт",
              todo == ("connor", "ты как вечер провела") and personas_of(lst) == ("connor", ["connor", "arrodes"]))
        todo = run_final(lst, "а что будешь делать")
        check("…и без имени — снова только ей", todo == ("connor", "а что будешь делать"))
        todo = run_final(lst, "коннор")
        check("имя той, что и так слушает, — без отправки", todo is None and lst.state == wl.AWAIT
              and personas_of(lst) == ("connor", ["connor", "arrodes"]))

        # Имя по ходу фразы: портрет — сразу; послышалось — как было
        lst.on_partial("родис")
        check("имя другой в начале фразы — её портрет сразу, до конца фразы",
              personas_of(lst) == ("arrodes", ["connor", "arrodes"]))
        lst.on_partial("родис расскажи")
        todo = lst.on_final("родис расскажи сказку")
        check("…фраза кончилась — ей", todo == ("arrodes", "расскажи сказку"))
        lst.handle(*todo)
        lst2 = make()
        lst2.with_portrait = True
        run_final(lst2, "коннор привет")
        lst2.on_partial("родис")
        check("имя третьей в начале фразы — встала", personas_of(lst2) == ("arrodes", ["connor", "arrodes"]))
        lst2.on_partial("рад видеть")
        check("распознаватель передумал — её портрет ушёл, слушает прежняя",
              personas_of(lst2) == ("connor", ["connor"]))
        lst2.on_partial("родис")
        todo = lst2.on_final("рады что пришли")
        check("…и если имени нет в конце фразы — тоже, фраза — прежней",
              todo == ("connor", "рады что пришли") and personas_of(lst2) == ("connor", ["connor"]))

        # Панель убрала портрет
        lst = make()
        lst.with_portrait = True
        run_final(lst, "коннор привет")
        run_final(lst, "родис привет")
        lst.leave_talk("connor")
        lst.on_tick()
        check("убрали ту, что ждёт, — разговор дальше с той, что слушает",
              personas_of(lst) == ("arrodes", ["arrodes"]) and lst.state == wl.AWAIT)
        run_final(lst, "коннор привет")
        lst.leave_talk("connor")
        lst.on_tick()
        check("убрали ту, что слушает, — слушает оставшаяся, без имени",
              personas_of(lst) == ("arrodes", ["arrodes"]) and lst.state == wl.AWAIT and lst.persona == "arrodes"
              and talk_of(lst)[1] == wl.TALK_LISTEN)
        lst.leave_talk("arrodes")
        lst.on_tick()
        check("убрали последнюю — разговор кончился", not lst.talking and lst.state == wl.IDLE)
        lst.leave_talk("arrodes")
        check("без разговора просьба ничего не оставляет", lst._talk_leave == [])

        # Перебить
        lst = make()
        lst.voice = SpeakingVoice()
        lst.with_portrait = True
        run_final(lst, "коннор")
        th = speaking_turn(lst, "какая погода")
        st = lst.status()["talk"]
        check("отвечает голосом в своём потоке — автомат свободен",
              st["phase"] == wl.TALK_SPEAK and lst.state == wl.BUSY and th.is_alive())
        lst.on_partial("солнечно плюс двинадцать")
        check("в микрофоне её же слова (с ошибкой распознавателя) — говорит дальше",
              lst.voice.stopped == 0 and talk_of(lst)[1] == wl.TALK_SPEAK and lst._seg_cut is None)
        check("…фраза кончилась — не ваша, ничего не ушло",
              lst.on_final("солнечно плюс двинадцать") is None and lst.state == wl.BUSY
              and talk_of(lst)[1] == wl.TALK_SPEAK)
        lst.on_partial("солнечно завтра")
        check("одно чужое слово — ещё не перебили", lst.voice.stopped == 0)
        lst.on_partial("солнечно а какая завтра погода")
        st = lst.status()["talk"]
        check(f"ваши слова поверх её голоса — замолчала и слушает ({st['phase']}, «{st['text']}»)",
              lst.voice.stopped == 1 and lst.state == wl.AWAIT and st["phase"] == wl.TALK_HEAR
              and st["text"] == "а какая завтра погода")
        check("…панели — её реплику оборвать", st["cut"] == 1 and st["cut_persona"] == "connor")
        th.join(3)
        check("ход, который перебили, дальше ничего не меняет", not th.is_alive() and lst.state == wl.AWAIT)
        todo = lst.on_final("солнечно а какая завтра погода")
        check(f"ушла только ваша часть фразы: {todo}", todo == ("connor", "а какая завтра погода"))

        # Перебить именем другой персоны
        th = speaking_turn(lst, "ну расскажи")
        lst.on_partial("солнечно родис")
        check("её перебили именем другой — та встала и слушает",
              lst.voice.stopped == 2 and personas_of(lst) == ("arrodes", ["connor", "arrodes"])
              and lst.status()["talk"]["cut_persona"] == "connor")
        todo = lst.on_final("солнечно родис как ты")
        check(f"…фраза — ей: {todo}", todo == ("arrodes", "как ты"))
        th.join(3)

        # «Подожди» и «хватит»
        th = speaking_turn(lst, "расскажи")
        lst.on_partial("солнечно подожди")
        check("«подожди» — замолчала", lst.voice.stopped == 3 and lst.state == wl.AWAIT)
        sent = len(lst.client.sent)
        check("…и ей ничего не ушло, слушает дальше",
              lst.on_final("солнечно подожди") is None and lst.state == wl.AWAIT and lst.talking
              and talk_of(lst)[1] == wl.TALK_LISTEN and len(lst.client.sent) == sent)
        th.join(3)
        th = speaking_turn(lst, "расскажи")
        lst.on_partial("солнечно хватит")
        lst.on_final("солнечно хватит")
        check("«хватит» поверх её голоса — разговор кончился", lst.voice.stopped == 4 and not lst.talking
              and lst.state == wl.IDLE)
        th.join(3)

        # Ответ по-английски: её голос русской моделью не сверить — не перебить
        lst = make(FakeClient(reply="Sunny and warm, twelve degrees."))
        lst.voice = SpeakingVoice()
        lst.with_portrait = True
        run_final(lst, "коннор")
        th = speaking_turn(lst, "какая погода")
        lst.on_partial("санни энд уорм твелв")
        check("ответ по-английски — её голос не перебивает (сверить не с чем)", lst.voice.stopped == 0)
        lst.voice.done()
        th.join(3)
        check("…договорила — слушает дальше", lst.state == wl.AWAIT and talk_of(lst)[1] == wl.TALK_LISTEN)

        # Договорила сама — дальше как обычно
        lst = make()
        lst.voice = SpeakingVoice()
        lst.with_portrait = True
        run_final(lst, "коннор")
        th = speaking_turn(lst, "какая погода")
        lst.voice.done()
        th.join(3)
        check("договорила сама — слушает вас без имени", lst.state == wl.AWAIT and talk_of(lst)[1] == wl.TALK_LISTEN
              and lst.status()["talk"]["cut"] == 0)

        # Хвост её голоса после конца озвучки
        wl.ECHO_TAIL_SEC = 0.5
        lst = make()
        lst.voice = SpeakingVoice()
        lst.with_portrait = True
        run_final(lst, "коннор")
        th = speaking_turn(lst, "какая погода")
        lst.voice.done()
        th.join(3)
        lst.on_partial("солнечно плюс")
        check("договорила, её хвост в микрофоне — не ваша фраза", talk_of(lst)[1] == wl.TALK_LISTEN and lst._seg_echo)

        class Dec:
            renewed = 0

            def renew(self, phrases):
                Dec.renewed += 1

        lst._echo_tail(Dec(), time.monotonic())
        check("…хвост ещё идёт — фразу не трогаем", Dec.renewed == 0)
        lst._echo_tail(Dec(), time.monotonic() + 1)
        check("…хвост прошёл — её фраза выброшена, дальше с чистого листа",
              Dec.renewed == 1 and not lst._seg_echo)
        time.sleep(wl.ECHO_TAIL_SEC + 0.05)
        lst.on_partial("да")
        check("ваше «да» сразу после — обычная фраза", talk_of(lst)[1:3] == (wl.TALK_HEAR, "да"))
        check("…и уходит ей", lst.on_final("да") == ("connor", "да"))

        # Перебили в тот миг, когда ход только собрался говорить, — не начинает
        lst = make()
        lst.with_portrait = True
        run_final(lst, "коннор")
        real_chat = lst.client.chat

        def chat_then_barge(persona, message):
            reply = real_chat(persona, message)
            lst._turn += 1  # перебили, пока ответ шёл к озвучке
            return reply

        lst.client.chat = chat_then_barge
        said = len(lst.voice.said)
        lst.handle("connor", "какая погода")
        check("перебили до начала озвучки — ответ не звучит", len(lst.voice.said) == said)

        # Убрали её портрет, пока говорит, — замолкает
        lst = make()
        lst.voice = SpeakingVoice()
        lst.with_portrait = True
        run_final(lst, "коннор")
        run_final(lst, "родис")
        th = speaking_turn(lst, "расскажи")
        lst.leave_talk("arrodes")
        lst.on_tick()
        th.join(3)
        check("портрет той, что говорит, убрали — замолчала, слушает оставшаяся",
              lst.voice.stopped >= 1 and personas_of(lst) == ("connor", ["connor"]) and lst.state == wl.AWAIT)

        # Значок микрофона, пока говорит, — всё
        lst = make()
        lst.voice = SpeakingVoice()
        lst.with_portrait = True
        run_final(lst, "коннор")
        th = speaking_turn(lst, "какая погода")
        lst.end_talk()
        lst.on_tick()
        th.join(3)
        check("закончили разговор, пока говорит, — замолчала, ждём имя",
              not lst.talking and lst.state == wl.IDLE and lst.voice.stopped >= 1)

        # Вне разговора у портрета её не перебить: микрофон глушится
        lst = make()
        lst.state = wl.BUSY
        import numpy as np
        lst._on_audio(np.zeros((1600, 1), np.float32), 1600, None, None)
        check("вне разговора у портрета, пока отвечает, — микрофон глушится", lst._audio.empty())
        lst = make()
        lst.with_portrait = True
        run_final(lst, "коннор")
        lst.state = wl.BUSY
        lst._talk_set(phase=wl.TALK_THINK)
        lst._on_audio(np.zeros((1600, 1), np.float32), 1600, None, None)
        # Раньше глушился и тут — пока ядро думало минуту-другую, ни имя, ни
        # новая фраза не доходили (B5)
        check("в разговоре, пока думает, — слушает (сказать новое)", lst._audio.qsize() == 1)
        lst._talk_set(phase=wl.TALK_SPEAK)
        lst._on_audio(np.zeros((1600, 1), np.float32), 1600, None, None)
        check("…и пока говорит — слушает (её можно перебить)", lst._audio.qsize() == 2)

        # Её слова в микрофоне
        ref = wl.EchoRef("Ясно, сеньор, ни облачка! Будет 12 градусов.")
        check("её слова с ошибками распознавателя — её",
              all(ref.has(w) for w in ["ясно", "синьор", "необлачка", "облачко", "двенадцать", "градуса"]))
        check("ваши — ваши", not any(ref.has(w) for w in ["завтра", "дождь", "погоди", "кино"]))
        check("«что-нибудь», «ещё» распознавателя — её, если в ответе так же",
              wl.EchoRef("Что-нибудь ещё?").mark("что-нибудь") is False and wl.EchoRef("Что-нибудь ещё?").mark("ещё") is False)
        enough = lambda tail, mine: mine >= 2  # noqa: E731
        words = "ясно синьор ни облачка а завтра дождь".split()
        check(f"ваша речь — после её последнего слова: {wl.barge_start(words, ref, enough)}",
              wl.barge_start(words, ref, enough) == 4)
        words = "ясно синьор кино облачка будет двенадцать".split()
        check("не узнанное слово посреди её речи — не ваша речь", wl.barge_start(words, ref, enough) is None)
        words = "ясно синон ни облачка подожди".split()
        check("одно ваше слово после её — ещё не перебили (если это не «подожди» и не имя)",
              wl.barge_start(words, ref, enough) is None)
        check("«подожди», «погоди секунду», «ну подожди» — замолчать и слушать",
              all(wl.talk_end(x) == wl.TALK_HUSH for x in ["подожди", "погоди секунду", "ну подожди", "стой стой"]))
        check("«подожди, я подумаю» — обычная реплика", wl.talk_end("подожди я подумаю") is None)
    finally:
        wl.ECHO_TAIL_SEC, wl.TALK_PAUSE_SEC = saved_tail, saved_pause


def test_pause():
    section("B4. Разговор у портрета: пауза, чтобы подумать и договорить")
    saved = wl.ECHO_TAIL_SEC
    wl.ECHO_TAIL_SEC = 0.0
    try:
        check(f"по умолчанию ждёт {wl.TALK_PAUSE_SEC:g} с", wl.TALK_PAUSE_SEC == 5.0)
        lst = make()
        lst.with_portrait = True
        lst.on_partial("коннор объясни")
        todo = lst.on_final("коннор объясни мне")
        check("фраза кончилась — ещё не ушла, ждёт продолжения",
              todo is None and lst.client.sent == [] and lst.state == wl.AWAIT
              and talk_of(lst) == ("connor", wl.TALK_HEAR, "коннор объясни мне", ""))
        check("…пока пауза не прошла — не уходит", lst._due() is None)
        lst._pending["until"] = time.monotonic() + 1  # прошло 4 с из 5
        lst.on_partial("как это")
        check("заговорили снова — продолжение видно вместе с началом, пауза заново",
              talk_of(lst)[2] == "коннор объясни мне как это"
              and lst._pending["until"] - time.monotonic() > wl.TALK_PAUSE_SEC - 0.5)
        check("…и не кончается, пока говорите", lst._due() is None)
        check("конец второй части — тоже ждёт", lst.on_final("как это делается в логике") is None)
        lst.on_partial("м")
        check("послышалось «м» — ждущая фраза видна как была",
              lst.on_final("м") is None and talk_of(lst)[1:3] == (wl.TALK_HEAR, "коннор объясни мне как это делается в логике"))
        lst._pending["until"] = time.monotonic() - 0.01
        todo = lst._due()
        check(f"помолчали {wl.TALK_PAUSE_SEC:g} с — ушла целиком, одной просьбой: {todo}",
              todo == ("connor", "объясни мне как это делается в логике")
              and talk_of(lst) == ("connor", wl.TALK_THINK, "коннор объясни мне как это делается в логике",
                                   "объясни мне как это делается в логике"))
        lst.handle(*todo)
        check("…ответила — дальше без имени", lst.state == wl.AWAIT and talk_of(lst)[1] == wl.TALK_LISTEN)

        lst.on_final("а завтра")
        lst.on_final("подожди")
        check("«подожди» — к фразе не дописывается, ждём дальше",
              lst._pending and lst._pending["message"] == "а завтра" and talk_of(lst)[2] == "а завтра")
        chimes.clear()
        todo = lst.on_final("хватит")
        check("«хватит» — сразу, без паузы, и недосказанное не уходит",
              todo is None and not lst.talking and lst.state == wl.IDLE and lst._pending is None
              and len(lst.client.sent) == 1)

        lst = make()
        lst.with_portrait = True
        run_final(lst, "коннор")
        lst.on_final("как дела")
        todo = lst.on_final("родис а ты как")
        check(f"назвали другую, пока фраза ждала, — прежняя уходит своей сейчас: {todo}",
              todo == ("connor", "как дела") and lst._pending and lst._pending["persona"] == "arrodes")
        lst.handle(*todo)
        check("…ответила — слушает названная, её фраза видна и ждёт паузы",
              lst.state == wl.AWAIT and talk_of(lst)[:3] == ("arrodes", wl.TALK_HEAR, "родис а ты как")
              and personas_of(lst) == ("arrodes", ["connor", "arrodes"]))
        lst._pending["until"] = time.monotonic() - 0.01
        check("…и уходит ей", lst._due() == ("arrodes", "а ты как"))

        lst = make()
        lst.with_portrait = True
        run_final(lst, "коннор")
        lst.on_final("расскажи")
        lst.leave_talk("connor")
        lst.on_tick()
        check("портрет убрали — недосказанное ей не уходит", lst._pending is None and not lst.talking)

        lst = make()
        lst.with_portrait = True
        lst.set_chats(["connor"])
        check("чат открыт в вебе (без портрета) — как раньше, сразу",
              lst.on_final("коннор какая погода") == ("connor", "какая погода"))
        saved_pause = wl.TALK_PAUSE_SEC
        wl.TALK_PAUSE_SEC = 0.0
        try:
            lst = make()
            lst.with_portrait = True
            check("WAKE_TALK_PAUSE_SEC=0 — сразу", lst.on_final("коннор какая погода") == ("connor", "какая погода"))
        finally:
            wl.TALK_PAUSE_SEC = saved_pause
    finally:
        wl.ECHO_TAIL_SEC = saved


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


def test_real_barge():
    section("D2. Перебить персону: её голос «из колонок» и ваш поверх него")
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
    import threading
    rec = wl.make_recognizer(model)
    tmp = Path(tempfile.mkdtemp(prefix="wake_barge_"))
    saved_pause = wl.TALK_PAUSE_SEC
    wl.TALK_PAUSE_SEC = 0.0

    def clip(text):
        wav = tmp / f"c{len(list(tmp.iterdir()))}.wav"
        subprocess.run(["say", "-v", "Milena", "-o", str(wav), "--data-format=LEI16@16000", text], check=True)
        with wave.open(str(wav)) as f:
            return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768

    def echo(x, seed):
        # Колонки ноутбука и комната: полоса 250–5000 Гц, тише, отражения, шум
        spec = np.fft.rfft(x)
        f = np.fft.rfftfreq(len(x), 1 / 16000)
        spec[(f < 250) | (f > 5000)] = 0
        y = np.fft.irfft(spec, len(x)).astype(np.float32) * 0.7
        out = y.copy()
        for d, a in [(0.012, 0.5), (0.035, 0.3), (0.08, 0.15)]:
            k = int(d * 16000)
            out[k:] += y[:-k] * a
        return out + np.random.default_rng(seed).standard_normal(len(out)).astype(np.float32) * 0.003

    def lower(x):
        # Другой голос: ниже тоном
        n = int(len(x) * 1.18)
        return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)

    class Voice:
        def __init__(self):
            self.stopped = 0

        def speak(self, text, alive=None):
            pass

        def stop(self):
            self.stopped += 1

    def run(reply, her, you=None, at=0):
        lst = wl.WakeListener(FakeClient(), voice=Voice())
        lst.phrases = list(PHRASES)
        lst._phrases_at = time.monotonic()
        lst.with_portrait = True
        lst._talk = {"persona": "connor", "phase": wl.TALK_SPEAK, "text": "", "sent": "",
                     "personas": ["connor"], "cut": 0, "cut_persona": ""}
        lst.state, lst.persona = wl.BUSY, "connor"
        lst._echo, lst._echo_until = wl.EchoRef(reply), float("inf")
        sent = []
        real_start = threading.Thread.start

        def start(th):
            # Ход после вашей фразы — сразу и здесь (ядро поддельное)
            if th.name == "wake-turn":
                sent.append(th._args)
            else:
                real_start(th)

        threading.Thread.start = start
        try:
            dec = wl.Decoder(rec, lst.phrases)
            n = max(len(her), at + (len(you) if you is not None else 0)) + 24000
            stop_at = None
            for k in range(0, n, 1600):
                chunk = np.full(1600, 1e-4, np.float32)
                h = her[k:k + 1600]
                if lst.voice.stopped and stop_at is None:
                    stop_at = k + 960  # её голос обрывается (буфер звука ~60 мс)
                if stop_at is not None:
                    h = h.copy()
                    h[max(0, stop_at - k):] = 0
                chunk[:len(h)] += h
                if you is not None and k + 1600 > at:
                    a, b = max(at, k), min(at + len(you), k + 1600)
                    if a < b:
                        chunk[a - k:b - k] += you[a - at:b - at]
                lst.process(dec, 16000, chunk)
        finally:
            threading.Thread.start = real_start
        return lst, sent

    replies = [
        "Ясно, сеньор, ни облачка. Будет двенадцать градусов, а к вечеру подует ветер с моря.",
        "Хорошо, записала: купить хлеб, молоко, яйца и батарейки для пульта. Что-нибудь ещё добавить?",
        "В двадцать три сорок пять начнётся фильм. До него полтора часа, успеешь поужинать.",
        "Знаешь, Родион вчера рассказывал смешную историю про кота, который открывал холодильник.",
    ]
    for i, r in enumerate(replies):
        lst, sent = run(r, echo(clip(r), i))
        check(f"только её голос в микрофоне — не перебивает сама себя («{r[:30]}…»)",
              lst.voice.stopped == 0 and not sent)
    for i, (r, you, key) in enumerate([
        (replies[0], "подожди а что будет завтра", "завтра"),
        (replies[1], "нет расскажи лучше про погоду на выходные", "выходные"),
    ]):
        her = echo(clip(r), 10 + i)
        lst, sent = run(r, her, lower(clip(you)), int(len(her) * 0.4))
        check(f"«{you}» поверх её голоса — замолчала, ушло: {sent}",
              lst.voice.stopped == 1 and len(sent) == 1 and key in sent[0][1])
    wl.TALK_PAUSE_SEC = saved_pause
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
            chunk = None
            if sd.stall_after is not None and sd.sent >= sd.stall_after:
                time.sleep(0.05)
                continue
            if sd.mix and sd.inject and sd.script:
                # Её голос из колонок и вы — в микрофоне одновременно
                buf = None
                chunk = sd.inject.pop(0) + sd.script.pop(0)
            elif sd.inject:
                buf = sd.inject
            elif sd.script:
                buf = sd.script
            else:
                buf = None
            if buf:
                chunk = buf.pop(0)
            elif chunk is not None:
                pass
            elif sd.zeros:
                chunk = np.zeros(self.block, np.float32)
            else:
                chunk = (rng.standard_normal(self.block) * 1e-4).astype(np.float32)
            sd.sent += len(chunk)
            self.callback(chunk.reshape(-1, 1), len(chunk), None, None)
            time.sleep(len(chunk) / 16000 / sd.speed)


class FakeSD:
    def __init__(self, script=None, zeros=False, stall_after=None, mix=False, speed=2.0):
        self.script = script or []
        self.inject = []
        self.mix = mix
        self.speed = speed
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
        def speak(self, text, alive=None):
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

        # Разговор у портрета: пауза, чтобы подумать, — фраза с паузой
        # посередине уходит одной просьбой, когда человек замолчал
        script = np.concatenate([
            gap(0.5), clip("Коннор, объясни мне"), gap(3.0),
            clip("как это делается в логике"), gap(14.0),
        ])
        sd = FakeSD(_chunks(script))
        sys.modules["sounddevice"] = sd
        lst = wl.WakeListener(Client(), voice=FakeVoice())
        lst.with_portrait = True
        lst.start()
        t0 = time.time()
        while sd.script and time.time() - t0 < 60:
            time.sleep(0.2)
        time.sleep(0.5)
        sent = list(lst.client.sent)
        check(f"пауза посередине фразы — ушла одной просьбой, когда замолчали: {sent}",
              len(sent) == 1 and "объясни" in sent[0][1] and "логике" in sent[0][1])
        lst.stop()
        lst._thread.join(5)

        # Дальше — фраза сразу (паузу проверили выше)
        saved_pause = wl.TALK_PAUSE_SEC
        wl.TALK_PAUSE_SEC = 0.0

        # Разговор у портрета (слушатель панели): после имени — без имени;
        # что говорите, видно по ходу речи; «хватит» — конец, дальше снова имя
        script = np.concatenate([
            gap(0.5), clip("Коннор, какая сегодня погода?"), gap(2.5),
            clip("А что будет завтра?"), gap(2.5),
            clip("Расскажи анекдот"), gap(2.5),
            clip("Хватит"), gap(2.5),
            clip("Мы вчера ходили в кино"), gap(2.5),
        ])
        sd = FakeSD(_chunks(script))
        sys.modules["sounddevice"] = sd
        lst = wl.WakeListener(Client(), voice=FakeVoice())
        lst.with_portrait = True
        seen = []
        import threading

        class Changed(threading.Event):
            # Каждое изменение разговора — в тот же миг (поддельное ядро
            # отвечает мгновенно: опросом «думает» не поймать)
            def set(self):
                t = lst.status()["talk"]
                if not seen or seen[-1] != t:
                    seen.append(t)
                super().set()

        lst.changed = Changed()
        lst.start()
        t0 = time.time()
        while sd.script and time.time() - t0 < 60:
            time.sleep(0.2)
        time.sleep(1.5)
        sent = list(lst.client.sent)
        check(f"в разговоре фразы без имени — той же персоне, «хватит» не ушло: {sent}",
              [p for p, _ in sent] == ["connor"] * 3 and "завтра" in sent[1][1] and "анекдот" in sent[2][1])
        heard = [t["text"] for t in seen if t and t["phase"] == wl.TALK_HEAR]
        thought = [t for t in seen if t and t["phase"] == wl.TALK_THINK]
        first = thought[0]["text"] if thought else ""
        check(f"фраза видна по ходу речи, частями: {heard[:4]}…",
              len({h for h in heard if first.startswith(h) and h != first}) >= 2)
        check(f"ушла фраза целиком, без имени: «{first}» → «{thought[0]['sent'] if thought else ''}»",
              len(thought) == 3 and first.startswith("коннор") and thought[0]["sent"] == sent[0][1])
        check("портрет — с первым же «коннор», до конца фразы",
              bool(heard) and heard[0].startswith("коннор") and seen[0] and seen[0]["phase"] in (wl.TALK_LISTEN, wl.TALK_HEAR))
        check("после «хватит» разговора нет, слушатель ждёт имя",
              seen[-1] is None and lst.state == wl.IDLE and lst.running)
        lst.stop()
        lst._thread.join(5)

        # Перебить: она отвечает голосом (её голос — в микрофоне), вы говорите
        # поверх — замолкает, ваша фраза уходит ей; её голос сам себя не перебивает
        long_reply = ("Жил-был рыцарь печального образа. Он читал старые книги о странствующих рыцарях, "
                      "а потом сам отправился в путь вместе с верным оруженосцем и старой лошадью.")
        her = clip(long_reply)
        spec = np.fft.rfft(her)
        f = np.fft.rfftfreq(len(her), 1 / 16000)
        spec[(f < 250) | (f > 5000)] = 0
        her = np.fft.irfft(spec, len(her)).astype(np.float32) * 0.5
        you = clip("подожди а что будет завтра")
        you = np.interp(np.linspace(0, len(you) - 1, int(len(you) * 1.18)), np.arange(len(you)), you).astype(np.float32)
        script = np.concatenate([
            gap(0.5), clip("Коннор, расскажи сказку"), gap(3.2), you, gap(3.0),
        ])

        class TalkClient(FakeClient):
            def chat(self, persona, message):
                self.sent.append((persona, message))
                return long_reply if "сказку" in message else "Солнечно."

        class SpeakerVoice(FakeVoice):
            stopped = 0

            def speak(self, text, alive=None):
                self.said.append(text)
                sd.inject = _chunks((her if text == long_reply else gap(0.3)).copy())
                while sd.inject:
                    time.sleep(0.02)

            def stop(self):
                SpeakerVoice.stopped += 1
                sd.inject = []

        sd = FakeSD(_chunks(script), mix=True)
        sys.modules["sounddevice"] = sd
        lst = wl.WakeListener(TalkClient(), voice=SpeakerVoice())
        lst.with_portrait = True
        lst.start()
        t0 = time.time()
        while sd.script and time.time() - t0 < 60:
            time.sleep(0.2)
        time.sleep(1.5)
        sent = list(lst.client.sent)
        talk = lst.status()["talk"]
        check(f"перебили посреди ответа — замолчала, ваша фраза ушла ей: {sent}",
              SpeakerVoice.stopped >= 1 and len(sent) == 2 and sent[1][0] == "connor" and "завтра" in sent[1][1])
        check("…панели сказано оборвать её реплику", talk is not None and talk["cut"] == 1
              and talk["cut_persona"] == "connor")
        lst.stop()
        lst._thread.join(5)

        # Персона долго думает (микрофон заглушён, кадры не берутся) — это не
        # «микрофон замолчал»: разговор идёт дальше, микрофон не переоткрыт
        old_stall = wl.STALL_SEC
        wl.STALL_SEC = 0.5

        class SlowClient(FakeClient):
            def chat(self, persona, message):
                self.sent.append((persona, message))
                time.sleep(2.0)
                return "Подумала."

        sd = FakeSD(_chunks(np.concatenate([gap(0.5), clip("Коннор, какая сегодня погода?"), gap(8.0)])))
        sys.modules["sounddevice"] = sd
        lst = wl.WakeListener(SlowClient(), voice=FakeVoice())
        lst.with_portrait = True
        lst.start()
        t0 = time.time()
        while sd.script and time.time() - t0 < 30:
            time.sleep(0.2)
        talk = lst.status()["talk"]
        check(f"думала дольше STALL_SEC — разговор идёт, микрофон не переоткрыт (открыт {sd.opened} раз)",
              lst.client.sent and lst.voice.said == ["Подумала."] and talk is not None
              and talk["phase"] == wl.TALK_LISTEN and sd.opened == 1)
        lst.stop()
        lst._thread.join(5)
        wl.STALL_SEC = old_stall
        wl.TALK_PAUSE_SEC = saved_pause

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
        # Дубль — только с другого устройства: новая фраза со своего, пока
        # прежняя в работе (сказали ещё, пока персона думала), идёт в очередь чата
        pc = {**body, "client_id": wl.VOICE_CLIENT_ID}
        phone = {**body, "client": "app"}
        server._voice_turns["connor"] = [1, 0.0, [wl.VOICE_CLIENT_ID], ""]
        n = len(calls)
        r = c.post("/api/chat", json=pc, headers=hdr)
        check("своё устройство, прежняя просьба ещё в работе — не дубль", r.json()["duplicate"] is False and len(calls) == n + 1)
        r = c.post("/api/chat", json=pc, headers=hdr)
        check("…и сразу после своей — тоже", r.json()["duplicate"] is False and len(calls) == n + 2)
        r = c.post("/api/chat", json=phone, headers=hdr)
        check("телефон сразу после компьютера (< VOICE_DUP_SEC) — дубль", r.json()["duplicate"] is True and len(calls) == n + 2)
        for inflight, req_body, who in [([wl.VOICE_CLIENT_ID], phone, "телефон, пока в работе просьба компьютера"),
                                        (["app"], pc, "компьютер, пока в работе просьба телефона"),
                                        ([""], pc, "компьютер, пока в работе просьба неизвестно откуда")]:
            server._voice_turns["connor"] = [1, 0.0, list(inflight), ""]
            r = c.post("/api/chat", json=req_body, headers=hdr)
            check(f"{who} — дубль", r.json()["duplicate"] is True)
        server._voice_turns["connor"] = [1, 0.0]
        r = c.post("/api/chat", json=pc, headers=hdr)
        check("старая запись без устройств — дубль, как раньше", r.json()["duplicate"] is True)
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
        check("слушатель ядра — where: core", c.get("/api/voice/wake", headers=hdr).json()["where"] == "core")
        wl._listener = None

        # Слушатель панели: сообщает о себе, пауза веба приходит ему в ответе
        st = {**make().status(), "running": True, "stage": wl.ST_LISTEN, "junk": "x"}
        r = c.post("/api/voice/wake/report", json=st, headers=hdr)
        check("report слушателя панели — пауза 0, открытых чатов нет",
              r.status_code == 200 and r.json() == {"hold": 0, "chat": []})
        w = c.get("/api/voice/wake", headers=hdr).json()
        check("GET /api/voice/wake — слушает панель, с именами", w["where"] == "panel" and w["enabled"]
              and w["running"] and "коннор" in w["names"] and "junk" not in w)
        r = c.post("/api/voice/wake/hold", json={"seconds": 30}, headers=hdr)
        check("пауза из веба при слушателе панели — held", r.json() == {"held": True})
        r = c.post("/api/voice/wake/report", json=st, headers=hdr)
        check("…и уходит ему в ответе на report", 29 <= r.json()["hold"] <= 30)
        c.post("/api/voice/wake/hold", json={"seconds": 0}, headers=hdr)
        r = c.post("/api/voice/wake/report", json=st, headers=hdr)
        check("снять паузу — в ответе 0", r.json()["hold"] == 0)

        # Чат открыт в вебе на этом компьютере — слушатель панели узнаёт об
        # этом в ответе на report (туда портрет не зовёт); телефон — не в счёт
        from app.core.presence import local_web_presence, web_context, web_presence
        local_web_presence.clear()
        web_presence.clear()
        here = TestClient(server.app, base_url="http://127.0.0.1:8000")
        r = here.post("/api/presence", json={"active": True, "persona": "connor"}, headers=hdr)
        check("вкладка чата на этом компьютере — в списке открытых",
              r.status_code == 200 and c.post("/api/voice/wake/report", json=st, headers=hdr).json()["chat"] == ["connor"])
        here.post("/api/presence", json={"active": False, "persona": "connor"}, headers=hdr)
        check("…вкладку закрыли — нет", c.post("/api/voice/wake/report", json=st, headers=hdr).json()["chat"] == [])
        phone = TestClient(server.app, base_url="http://192.168.1.5:8000")
        phone.post("/api/presence", json={"active": True, "persona": "connor"}, headers=hdr)
        check("чат открыт на телефоне — не в списке (фон бота он всё равно гасит)",
              c.post("/api/voice/wake/report", json=st, headers=hdr).json()["chat"] == []
              and web_presence.is_active(web_context("connor"), "web_user"))
        web_presence.clear()
        link = {**hdr, "x-vpc-link": "1"}
        here.post("/api/presence", json={"active": True, "persona": "connor"}, headers=link)
        check("…и по VPC Link — тоже", c.post("/api/voice/wake/report", json=st, headers=hdr).json()["chat"] == [])
        local_web_presence.note(web_context("connor"), "chat2", True)
        check("другой чат персоны — не web_user, не в счёт",
              c.post("/api/voice/wake/report", json=st, headers=hdr).json()["chat"] == [])
        local_web_presence.clear()
        web_presence.clear()
        check("report без тела — 422", c.post("/api/voice/wake/report", headers=hdr).status_code == 422)
        wl._remote_at -= wl.REMOTE_FRESH_SEC + 1
        check("панель замолчала — GET снова без слушателя",
              c.get("/api/voice/wake", headers=hdr).json() == {"enabled": False, "running": False})
        r = c.post("/api/voice/wake/hold", json={"seconds": 30}, headers=hdr)
        check("…и пауза никому", r.json() == {"held": False})
        c.post("/api/voice/wake/hold", json={"seconds": 0}, headers=hdr)
        wl._remote_at = 0.0
    finally:
        server._chat_turn, server._get_bot = orig
        server._voice_turns.clear()
    ssrc = (ROOT / "app" / "api" / "server.py").read_text("utf-8")
    check("оба эндпоинта чата: голосом — spoken в process_message",
          ssrc.count('**({"spoken": True} if req.voice else {})') == 2)
    test_spoken_turn()
    src = (ROOT / "app" / "main.py").read_text("utf-8")
    check("run_api поднимает и гасит слушатель",
          "wake_listener.start_with_core(host, port)" in src and "wake_listener.stop_with_core()" in src)


def test_spoken_turn():
    """Ход голосом: заметка «коротко, вслух» доходит до промпта (настоящий
    _process_message_impl на заглушках), END-строка — с провайдером."""
    import logging
    from types import SimpleNamespace
    from app.bot_instance import BotInstance, SPOKEN_REPLY_NOTE
    from app.features.conversation_style import ConversationStyleConfig
    from app.features.list_offers import ListOffers
    seen = []

    class Persona:
        settings, persona_data = {}, {"name": "Rodion"}

        def prepare_messages(self, *a, **kw):
            seen.append(list(kw.get("addon_blocks") or []))
            return [{"role": "system", "content": "SYS"}]

        def get_settings(self):
            return {"max_tokens": 500}

    class Memory:
        class _STM:
            def get_last(self, *a, **kw):
                return []

        class _LTM:
            def get_facts_by_category(self, *a, **kw):
                return []

            def save_facts(self, *a, **kw):
                pass

        def __init__(self):
            self.stm, self.ltm = self._STM(), self._LTM()

        def add_message(self, *a, **kw):
            pass

        def get_context(self, *a, **kw):
            return [], [], []

        def get_chat_facts_block(self, *a, **kw):
            return ""

    class Router:
        answer_provider, _last_provider = None, "mimo"

        def is_local_primary(self):
            return False

        def supports_vision(self):
            return False

        def get_response(self, messages, **kw):
            return "Коротко."

    b = BotInstance.__new__(BotInstance)
    b.persona_name, b.context, b.owner = "rodion", f"spoken_{time.time_ns()}", "web_user"
    b.web_single_user, b._cc_allowed_users = True, set()
    b.features = {"file_access": False}
    b.trigger_words = ["родион"]
    b.intellect = SimpleNamespace(active=False)
    b.conversation_style = ConversationStyleConfig(None)
    b._control_mode = set()
    b.computer_control = b.scenario_manager = b.task_agent = b.proactive = None
    b.addons, b.self_memory, b.living = [], None, None
    b.todo_manager = b.inventory_manager = b.reminder_manager = None
    b.learning_manager = b.file_db = None
    b._punish_enabled = b._moderation_enabled = False
    b._web_search_enabled, b._web_search_disabled_chats = False, set()
    b._pending_list_messages, b._pending_split_messages = {}, {}
    b._pending_photos, b._pending_question_kind, b._pending_more_photos = {}, {}, {}
    b.persona, b.memory, b.router, b.list_offers = Persona(), Memory(), Router(), ListOffers()
    lines = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    log = logging.getLogger("app.bot_instance")
    h, old_level = Grab(), log.level
    log.addHandler(h)
    log.setLevel(logging.INFO)
    try:
        reply = b.process_message("почему они меня не слушают", user_id="web_user", chat_id="web_user", spoken=True)
        voiced = seen[-1] if seen else []
        b.process_message("почему они меня не слушают", user_id="web_user", chat_id="web_user")
        typed = seen[-1] if seen else []
    finally:
        log.removeHandler(h)
        log.setLevel(old_level)
    check(f"голосом — в промпте заметка «коротко, вслух» (ответ: {reply!r})", SPOKEN_REPLY_NOTE in voiced)
    check("текстом — без неё", len(seen) == 2 and SPOKEN_REPLY_NOTE not in typed)
    ends = [x for x in lines if "process_message END" in x]
    check(f"строка END — с провайдером и пометкой «голосом»: {ends[:1]}",
          len(ends) == 2 and "| mimo | голосом |" in ends[0] and "голосом" not in ends[1])


def test_waiting():
    section("B5. Пока персона думает: микрофон слышит, новое прерывает ход")
    import threading
    import httpx
    import numpy as np
    saved_pause = wl.TALK_PAUSE_SEC
    wl.TALK_PAUSE_SEC = 0.0

    def started(client=None):
        lst = make(client)
        lst.with_portrait = True
        todo = lst.on_final("коннор какое сегодня число")
        lst.state, lst.persona = wl.BUSY, todo[0]  # как process(): ждём ответ с этой же фразы
        return lst, todo

    try:
        lst, todo = started()
        check("фраза ушла — она думает", todo == ("connor", "какое сегодня число") and lst._waiting()
              and lst._talk["phase"] == wl.TALK_THINK)
        lst._on_audio(np.zeros((160, 1), np.float32), 160, None, None)
        check("пока она думает, микрофон не глушится (раньше — до самого ответа)", lst._audio.qsize() == 1)
        other = make()
        other.state = wl.BUSY
        other._on_audio(np.zeros((160, 1), np.float32), 160, None, None)
        check("без портрета, пока ждём ответ, — глушится, как раньше", other._audio.qsize() == 0)
        turn = lst._turn
        lst.on_partial("а ты")
        check("говорите, пока она думает, — облачко по ходу речи", lst._talk["phase"] == wl.TALK_HEAR
              and lst._talk["text"] == "а ты" and lst.state == wl.BUSY)

        def thinks():
            t = lst._talk
            return (lst.state == wl.BUSY and lst._turn == turn and t["phase"] == wl.TALK_THINK
                    and t["text"] == "коннор какое сегодня число" and t["sent"] == "какое сегодня число")

        check("шум — снова думает над прежней фразой", lst.on_final("м") is None and thinks())
        check("«humph» — тоже", lst.on_final("humph") is None and thinks())
        chimes.clear()
        check("её имя без просьбы — сигнал «слышу», думает дальше",
              lst.on_final("коннор") is None and chimes == ["wake"] and thinks())
        check("«подожди» — думает дальше", lst.on_final("подожди") is None and thinks())
        todo = lst.on_final("а ты вообще меня слышишь")
        check(f"новая фраза — прежний ход прерван, новая уходит ей: {todo}",
              todo == ("connor", "а ты вообще меня слышишь") and lst._turn == turn + 1
              and lst._talk["phase"] == wl.TALK_THINK and lst._talk["sent"] == "а ты вообще меня слышишь")

        lst, _ = started()
        todo = lst.on_final("арродес расскажи сказку")
        check(f"другая персона, пока эта думает, — слушает она: {todo}", todo == ("arrodes", "расскажи сказку")
              and lst._talk["persona"] == "arrodes" and "connor" in lst._talk["personas"])
        lst, _ = started()
        turn = lst._turn
        check("«хватит», пока думает, — разговор всё", lst.on_final("хватит") is None and lst._talk is None
              and lst.state == wl.IDLE and lst._turn == turn + 1)

        # Весь ход: прежний ответ приходит после новой фразы — не звучит
        class Slow(FakeClient):
            def __init__(self, fail=False):
                super().__init__()
                self.go, self.fail = threading.Event(), fail

            def chat(self, persona, message, **kw):
                self.sent.append((persona, message))
                self.kw.append(kw)
                if len(self.sent) == 1:
                    self.go.wait(5)
                    if self.fail:
                        raise httpx.ReadTimeout("долго")
                return f"Ответ на «{message}»."

        for fail in (False, True):
            chimes.clear()
            lst, todo = started(Slow(fail))
            th = threading.Thread(target=lst.handle, args=todo)
            th.start()
            t0 = time.time()
            while len(lst.client.sent) < 1 and time.time() - t0 < 3:
                time.sleep(0.01)
            todo2 = lst.on_final("ну ты где")
            lst.state = wl.BUSY
            lst.handle(*todo2)
            lst.client.go.set()
            th.join(5)
            what = "сбой прежнего хода" if fail else "прежний ответ"
            check(f"{what} пришёл после новой фразы — не прозвучал: {lst.voice.said}",
                  lst.voice.said == ["Ответ на «ну ты где»."] and "error" not in chimes)
            check("…разговор идёт дальше (ждёт следующую фразу)", lst.state == wl.AWAIT and lst._talk is not None
                  and lst._talk["phase"] == wl.TALK_LISTEN)

        def wait_cancels(lst, n=1):
            t0 = time.time()
            while len(lst.client.cancels) < n and time.time() - t0 < 2:
                time.sleep(0.01)
            return lst.client.cancels

        # Ядру — оборвать прежний ход, а не ждать, пока модель допишет
        lst, _ = started()
        lst.on_final("а ты вообще меня слышишь")
        check("новая фраза ей же, пока думает, — ядру «оборви прежний ход» сразу",
              wait_cancels(lst) == ["connor"] and "connor" in lst._supersede)
        lst.handle("connor", "а ты вообще меня слышишь")
        check("…и следующая просьба ей — с supersede (на случай, если отмена не дошла)",
              lst.client.kw[-1] == {"supersede": True} and "connor" not in lst._supersede)
        lst.handle("connor", "ещё")
        check("…а дальше — уже без него", lst.client.kw[-1] == {})
        lst, _ = started()
        lst.on_final("арродес расскажи сказку")
        time.sleep(0.1)
        check("назвали другую — прежнюю не обрываем: её ответ ляжет в её чат",
              lst.client.cancels == [] and "connor" not in lst._supersede)
        lst, _ = started()
        lst.on_final("хватит")
        check("«хватит», пока думает, — ход в ядре тоже оборван", wait_cancels(lst) == ["connor"])
        lst, _ = started()
        lst.end_talk()
        lst.on_tick()
        check("портрет убрали, пока думает, — ход в ядре оборван", wait_cancels(lst) == ["connor"]
              and lst._talk is None)
        lst, _ = started()
        lst.leave_talk("connor")
        lst.on_tick()
        check("её портрет убрали из разговора, пока думает, — тоже", wait_cancels(lst) == ["connor"])
        lst, _ = started()
        lst._talk_set(phase=wl.TALK_SPEAK)
        lst.end_talk()
        lst.on_tick()
        time.sleep(0.1)
        check("убрали, пока говорит (ответ уже есть), — отменять в ядре нечего", lst.client.cancels == [])

        seen = []
        real = httpx.post

        def fake_post(url, headers=None, timeout=None, json=None):
            seen.append((url, json or {}))
            if url.endswith("/api/chat/cancel"):
                return httpx.Response(200, json={"cancelled": 1}, request=httpx.Request("POST", url))
            reply = {"reply": "", "cancelled": True} if (json or {}).get("message") == "долго" else {"reply": "ок"}
            return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

        httpx.post = fake_post
        try:
            cl = wl.CoreClient("http://core")
            cl.chat("connor", "привет")
            check("ядру — своё устройство (client_id): новая фраза, пока прежняя в работе, не дубль",
                  seen[-1][1].get("client_id") == wl.VOICE_CLIENT_ID and seen[-1][1].get("voice") is True
                  and "supersede" not in seen[-1][1])
            cl.chat("connor", "привет", supersede=True)
            check("supersede — в теле запроса", seen[-1][1].get("supersede") is True)
            check("ход отменён (cancelled) — пустой ответ, не «дубль»", cl.chat("connor", "долго") == "")
            n = cl.cancel("connor")
            check("отмена — POST /api/chat/cancel, тот же чат web_user",
                  n == 1 and seen[-1] == ("http://core/api/chat/cancel",
                                          {"persona": "connor", "user_id": "web_user", "chat_id": "web_user"}))
        finally:
            httpx.post = real
    finally:
        wl.TALK_PAUSE_SEC = saved_pause


# ===== H =====

def _tone(sec, db, rate=16000, f=220.0):
    import numpy as np
    a = 10 ** (db / 20) * np.sqrt(2)
    t = np.arange(int(rate * sec)) / rate
    return (a * np.sin(2 * np.pi * f * t)).astype(np.float32)


def _gate_run(g, x, strict=True, rate=16000, at0=0.0):
    """Прогнать сигнал кусками по 100 мс → выход и сдвиг (сколько держит задержка)."""
    import numpy as np
    out, at, block = [], at0, rate // 10
    for k in range(0, len(x), block):
        ch = x[k:k + block]
        at += len(ch) / rate
        out.append(g.feed(rate, ch, at, strict))
    return np.concatenate(out), at


def _rms_db(x):
    import numpy as np
    return 10 * np.log10(np.mean(np.square(x, dtype=np.float64)) + 1e-20)


def test_near():
    section("H. Фильтр фона: кто ближе к микрофону")
    try:
        import numpy as np
    except Exception:
        print("  (пропуск — нет numpy)")
        return
    from app.voice import near_gate as ng
    R = 16000
    F = int(R * ng.FRAME_SEC)

    # Вы — −12 дБ, фон — −30: тихое тишиной, ваше как есть
    g = ng.NearGate("mic")
    g.level = -12.0
    x = np.concatenate([_tone(1.0, -30), _tone(1.0, -12), _tone(1.0, -30)])
    y, _ = _gate_run(g, x)
    look = round(ng.LOOK_SEC / ng.FRAME_SEC) * F
    check(f"задержка — LOOK_SEC: наружу на {look} отсчётов меньше ({len(x) - len(y)})", len(x) - len(y) == look)
    seg = lambda a, b: y[int(a * R):int(b * R)]
    check(f"фон до вашей речи — тишиной ({_rms_db(seg(0.1, 0.6)):.0f} дБ)", _rms_db(seg(0.1, 0.6)) < -70)
    check("ваша речь — как есть", np.allclose(seg(1.05, 1.95), x[int(1.05 * R):int(1.95 * R)]))
    pre = seg(1.0 - ng.LOOK_SEC + 0.03, 1.0)
    check(f"начало слова (LOOK_SEC до громкого) — цело ({_rms_db(pre):.0f} дБ)", abs(_rms_db(pre) + 30) < 1)
    tail = seg(2.0, 2.0 + ng.HANG_SEC - 0.03)
    check(f"хвост (HANG_SEC после) — цел ({_rms_db(tail):.0f} дБ)", abs(_rms_db(tail) + 30) < 1)
    check(f"дальше фон — тишиной ({_rms_db(seg(2.5, 2.7)):.0f} дБ)", _rms_db(seg(2.5, 2.7)) < -70)
    edge = seg(0.5, 1.0 - ng.LOOK_SEC)
    check("стык плавный: без скачка больше самого сигнала", float(np.max(np.abs(np.diff(y)))) < 0.05)
    check("до начала слова — тишина", _rms_db(edge) < -70)

    # Щелчок одним кадром не открывает; два громких кадра — открывают
    g = ng.NearGate("mic")
    g.level = -12.0
    x = np.concatenate([_tone(0.5, -30), _tone(ng.FRAME_SEC, -12), _tone(0.5, -30)])
    y, _ = _gate_run(g, x)
    check(f"щелчок в один кадр — не открыл ({_rms_db(y[int(0.3 * R):]):.0f} дБ)", _rms_db(y[int(0.3 * R):]) < -60)

    # Мягче, пока ждём имя: на 12 дБ тише — до имени слышно (запас
    # margin + IDLE_EXTRA_DB), в разговоре — нет (margin)
    x = np.concatenate([_tone(0.5, -24), _tone(0.5, -24)])
    g = ng.NearGate("mic")
    g.level = -12.0
    y_idle, _ = _gate_run(g, x, strict=False)
    g = ng.NearGate("mic")
    g.level = -12.0
    y_strict, _ = _gate_run(g, x, strict=True)
    check(f"на 12 дБ тише: до имени слышно ({_rms_db(y_idle[R // 4:]):.0f}), "
          f"в разговоре — нет ({_rms_db(y_strict[R // 4:]):.0f})",
          abs(_rms_db(y_idle[R // 4:]) + 24) < 1 and _rms_db(y_strict[R // 4:]) < -60)

    # Громкость ещё не знаем или фильтр выключен — пропускает всё
    g = ng.NearGate("mic")
    y, _ = _gate_run(g, _tone(1.0, -40))
    check("уровня нет — пропускает всё", abs(_rms_db(y[R // 4:]) + 40) < 1)
    g = ng.NearGate("mic", on=False)
    g.level = -12.0
    y, _ = _gate_run(g, _tone(1.0, -40))
    check("WAKE_NEAR=0 — пропускает всё", abs(_rms_db(y[R // 4:]) + 40) < 1 and g.threshold(True) is None)

    # Другая частота микрофона — кадры по 20 мс и там
    g = ng.NearGate("mic")
    g.level = -12.0
    x48 = np.concatenate([_tone(0.5, -30, 48000), _tone(0.5, -12, 48000)])
    y, _ = _gate_run(g, x48, rate=48000)
    check("48 кГц: фон тишиной, речь как есть",
          _rms_db(y[4800:9600]) < -70 and np.allclose(y[int(0.55 * 48000):int(0.6 * 48000)],
                                                     x48[int(0.55 * 48000):int(0.6 * 48000)]))

    # «Запомнить мой голос»: CALIB_SEC речи с паузами → громкость, на микрофон
    path = Path(tempfile.mkdtemp(prefix="near_")) / "near.json"
    g = ng.NearGate("Микрофон MacBook", path)
    g.calibrate(0.0)
    check("идёт запись", g.calibrating and g.status()["calibrating"])
    speech = np.concatenate([_tone(0.6, -14), _tone(0.3, -60), _tone(0.6, -14), _tone(0.3, -60)] * 4)
    room = (np.random.default_rng(1).standard_normal(R * 2) * 1e-3).astype(np.float32)
    _gate_run(g, np.concatenate([room, speech]), strict=False)
    st = g.status()
    check(f"записано: {st}", not g.calibrating and st["calib_result"] == "ok" and st["source"] == "panel"
          and abs(st["level"] + 14) < 1.5)
    saved = json.loads(path.read_text("utf-8"))
    check(f"сохранено на микрофон: {saved}", "Микрофон MacBook" in saved)
    check("тот же микрофон потом — помнит", ng.NearGate("Микрофон MacBook", path).level == st["level"])
    check("другой микрофон — своя громкость (нет)", ng.NearGate("AirPods", path).level is None)
    # Молчали всю запись — громкость прежняя
    g.calibrate(100.0)
    _gate_run(g, (np.random.default_rng(2).standard_normal(int(R * 7)) * 1e-3).astype(np.float32), at0=100.0)
    check(f"молчали — «речи не услышал», громкость прежняя ({g.status()})",
          g.status()["calib_result"] == "no_speech" and round(g.level, 1) == st["level"])

    # По имени: громкость того, кто его сказал (последние NAME_WINDOW_SEC)
    g = ng.NearGate("mic2", path)
    x = np.concatenate([room, _tone(1.0, -20) + _tone(1.0, -38, f=330.0)])
    _, at = _gate_run(g, x, strict=False)
    lv = g.learn_name(at)
    check(f"по имени: {lv and round(lv, 1)} дБ (ваша речь −20, фон −38)", lv is not None and abs(lv + 20) < 1.5
          and g.source == "name")
    # Ваша фраза в разговоре: громкость понемногу к ней
    g.level = -20.0
    x = _tone(1.5, -14)
    _, at2 = _gate_run(g, x, at0=at)
    lv2 = g.learn_phrase(at)
    check(f"по фразе — понемногу: −20 → {lv2 and round(lv2, 1)}", lv2 is not None and -19 < lv2 < -17)
    # Записанная в панели громкость по имени двигается понемногу, а не заменяется
    g.level, g.source = -14.0, "panel"
    _, at3 = _gate_run(g, _tone(1.6, -24), strict=False, at0=at2)
    lv3 = g.learn_name(at3)
    check(f"записано в панели — по имени понемногу: −14 → {lv3 and round(lv3, 1)}",
          lv3 is not None and -18 < lv3 < -15 and g.source == "panel")
    g.forget()
    check("«забыть» — громкости нет, и на диске тоже", g.level is None and "mic2" not in json.loads(path.read_text("utf-8")))

    # Слушатель: состояние для панели, команды stdin
    import io
    lst = make()
    st = lst.status()["near"]
    check(f"состояние «near» без микрофона: {st}", st["on"] is True and st["level"] is None and not st["calibrating"])
    old_stdin = sys.stdin
    sys.stdin = io.StringIO("calibrate\n")
    try:
        wl._commands(lst)
    finally:
        sys.stdin = old_stdin
    st = lst.status()["near"]
    check(f"stdin «calibrate» — запись ждёт поток микрофона: {st}", lst._calib_req and st["calibrating"])
    lst._gate = ng.NearGate("mic3", path)
    lst._gate.level = -10.0
    sys.stdin = io.StringIO("forget_voice\n")
    try:
        wl._commands(lst)
    finally:
        sys.stdin = old_stdin
    check("stdin «forget_voice» — громкость забыта", lst._gate.level is None)


def test_real_near():
    section("H2. Фильтр фона на настоящем распознавателе: толпа на фоне")
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
    from app.voice import near_gate as ng
    os.environ["WAKE_MODEL_DIR"] = str(model)
    tmp = Path(tempfile.mkdtemp(prefix="wake_near_"))

    def clip(text, rate=None):
        wav = tmp / f"c{len(list(tmp.iterdir()))}.wav"
        cmd = ["say", "-v", "Milena", "-o", str(wav), "--data-format=LEI16@16000"] + (["-r", str(rate)] if rate else [])
        subprocess.run(cmd + [text], check=True)
        with wave.open(str(wav)) as f:
            return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768

    def gap(sec):
        return np.full(int(16000 * sec), 1e-4, np.float32)

    # Толпа: тот же голос (фильтр смотрит только на громкость), свои слова,
    # без пауз — на 15 дБ тише вас, всё время
    crowd = np.concatenate([
        clip("Слушай, а ты видел вчера матч, там такое было, судья вообще ничего не видел, "
             "а потом ещё пошли в кафе и взяли пиццу с грибами, она была холодная", 200),
        clip("Ну и что ты ему сказала? А он что? Да ладно, не может быть, я бы так не смогла "
             "никогда в жизни, честное слово, а когда у нас собрание, в четверг или в пятницу", 215),
    ] * 3)

    def scene(parts, drop=15.0):
        x = np.concatenate(parts)
        c = np.resize(crowd, len(x)) * 10 ** (-drop / 20)
        return (x + c).astype(np.float32)

    def talk(script, near_on=True, calibrate=False, speed=2.0):
        sd = FakeSD(_chunks(script), speed=speed)
        sys.modules["sounddevice"] = sd
        os.environ["WAKE_NEAR"] = "1" if near_on else "0"
        lst = wl.WakeListener(FakeClient(reply="Ясно."), voice=FakeVoice())
        lst.with_portrait = True
        if calibrate:
            lst.calibrate()
        ends = []
        orig = lst.on_final

        def on_final(text):
            if text.strip():
                ends.append((sd.sent / 16000, text.strip()))
            return orig(text)

        lst.on_final = on_final
        lst.start()
        t0 = time.time()
        while sd.script and time.time() - t0 < 90:
            time.sleep(0.2)
        time.sleep(1.0)
        lst.stop()
        lst._thread.join(5)
        return lst, ends

    saved_pause, saved_calib = wl.TALK_PAUSE_SEC, ng.CALIB_SEC
    wl.TALK_PAUSE_SEC = 0.0
    old_sd = sys.modules.get("sounddevice")
    mine = {"какая", "сегодня", "погода", "а", "что", "будет", "завтра", "привет"}

    def clean(sent, want):
        got = [set(m.split()) for _, m in sent]
        return len(sent) == len(want) and all(w <= g <= mine for g, w in zip(got, want))

    def end_after(ends, word, at):
        t = next((t for t, txt in ends if word in txt), None)
        return t, t is not None and 0 < t - at < 2.0

    try:
        u0, u1, u2 = clip("Коннор, привет"), clip("Коннор, какая сегодня погода?"), clip("А что будет завтра?")
        script = scene([gap(1.5), u1, gap(3.0), u2, gap(4.0)])

        # Без фильтра: толпа сливается с вами в одну фразу, имя тонет
        ng.levels_path().unlink(missing_ok=True)
        lst, ends = talk(script, near_on=False)
        raw = list(lst.client.sent)
        check(f"без фильтра — имя тонет в толпе или её слова во фразе (иначе проверять нечего): {raw}; "
              f"слышал: {[t for _, t in ends]}", not clean(raw, [{"погода"}, {"завтра"}]))

        # «Запомнить мой голос» прямо в толпе: читаете текст — имя в нём не
        # будит; дальше разговор — только ваши слова, фраза кончается с вами
        ng.levels_path().unlink(missing_ok=True)
        reading = clip("Коннор, это проверка, я говорю как обычно")
        # Запись меряется по часам, а поддельный микрофон играет вдвое быстрее
        # жизни: CALIB_SEC часов — вдвое больше звука
        # Фраза для записи длится ~3 с; калибровка должна включать её целиком
        # и закончиться до следующих контрольных реплик.
        ng.CALIB_SEC = 4.5
        lead = 0.5 + ng.CALIB_SEC + 1.0
        # В этой проверке имя надо уверенно распознать после калибровки;
        # фон остаётся непрерывным, но делаем его на 20 дБ тише. Сложные 15 дБ
        # отдельно проверяются в сценарии ниже, где имя сказано в тишине.
        script = scene([gap(0.5), reading, gap(lead - 0.5 - len(reading) / 16000), u1, gap(3.0), u2, gap(4.0)],
                       drop=20.0)
        # Калибровка отсчитывается wall clock; проигрываем этот сценарий в
        # реальном времени, чтобы её граница не зависела от очереди fake mic.
        lst, ends = talk(script, calibrate=True, speed=1.0)
        st = lst.status()["near"]
        sent = list(lst.client.sent)
        check(f"записано в панели прямо в толпе: {st}", st["source"] == "panel" and st["calib_result"] == "ok")
        heard = [txt for _, txt in ends]
        extra = [w for txt in heard for w in txt.split() if w not in mine]
        # Имя под толпой бывает искажено («конунер»): толпа звучала поверх
        # него самого — это фильтру не убрать (он убирает фон в паузах);
        # чужих слов, кроме такого имени, нет
        check(f"имя во время записи не разбудило; дальше слышно только ваше: {heard} → ушло {sent}",
              len(extra) <= 1 and any("погод" in h for h in heard) and any("завтра" in h for h in heard)
              and all("провер" not in m for _, m in sent))
        t, ok_ = end_after(ends, "погод", lead + len(u1) / 16000)
        check(f"фраза кончилась вскоре после вас: вы замолчали {lead + len(u1) / 16000:.1f} с, "
              f"конец {t and round(t, 1)} с", ok_)

        # Сами по имени: первый раз — в тишине, громкость по нему; дальше
        # толпа — в разговоре только ваши слова
        ng.levels_path().unlink(missing_ok=True)
        quiet = np.concatenate([gap(0.5), u0, gap(3.0)])
        rest = scene([clip("Какая сегодня погода?"), gap(3.0), u2, gap(4.0)])
        lst, ends = talk(np.concatenate([quiet, rest]))
        st = lst.status()["near"]
        sent = list(lst.client.sent)
        check(f"громкость — по фразе с именем: {st}", st["source"] == "name" and st["level"] is not None)
        check(f"дальше в толпе — только ваши слова: {sent}", clean(sent, [{"привет"}, {"погода"}, {"завтра"}]))
    finally:
        wl.TALK_PAUSE_SEC, ng.CALIB_SEC = saved_pause, saved_calib
        os.environ.pop("WAKE_NEAR", None)
        if old_sd is not None:
            sys.modules["sounddevice"] = old_sd
        else:
            sys.modules.pop("sounddevice", None)


# ===== G =====

class FakeCore:
    """Поддельное ядро по HTTP: имена, report (пауза — hold), chat."""

    def __init__(self):
        import http.server
        import threading
        core = self
        self.reports = []
        self.chats = []
        self.hold = 0.0

        class H(http.server.BaseHTTPRequestHandler):
            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/personas":
                    return self._json(CASES["personas"])
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/voice/wake/report":
                    core.reports.append(data)
                    return self._json({"hold": core.hold})
                if self.path == "/api/chat":
                    core.chats.append(data)
                    return self._json({"reply": "Готово.", "duplicate": False})
                self.send_response(404)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _wait(cond, sec):
    t0 = time.time()
    while not cond() and time.time() - t0 < sec:
        time.sleep(0.05)
    return cond()


def test_panel():
    section("G. Слушатель панели в трее")
    import contextlib
    import io
    import logging
    import signal
    import threading

    # Без модулей: код 2 (панель не перезапускает) и причина строкой состояния
    saved = sys.modules.get("sherpa_onnx")
    sys.modules["sherpa_onnx"] = None
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = wl.main(["--status", "--core", "http://127.0.0.1:9"])
    finally:
        if saved is not None:
            sys.modules["sherpa_onnx"] = saved
        else:
            sys.modules.pop("sherpa_onnx", None)
    lines = [json.loads(l[len(wl.STATUS_PREFIX):]) for l in out.getvalue().splitlines()
             if l.startswith(wl.STATUS_PREFIX)]
    check("нет модулей — код 2", rc == wl.EXIT_SETUP)
    check("…и состояние с причиной для панели", len(lines) == 1 and lines[0]["issue"] == wl.ISSUE_MODULES
          and "pip install" in lines[0]["error"])

    try:
        import numpy as np  # noqa: F401
        import sherpa_onnx  # noqa: F401
    except Exception:
        print("  (дальше пропуск — нет sherpa-onnx)")
        return
    model = Path(os.getenv("WAKE_MODEL_DIR") or ROOT / "data" / "models" / "wake-ru")
    if not all((model / f).is_file() for f in wl.MODEL_FILES):
        print("  (дальше пропуск — нет модели)")
        return
    os.environ["WAKE_MODEL_DIR"] = str(model)
    old_sd = sys.modules.get("sounddevice")
    core = FakeCore()
    try:
        # Слушатель ядра (WAKE_LISTEN=1, ядро из терминала) уступает микрофон
        # слушателю панели, пока тот сообщает о себе
        sd = FakeSD()
        sys.modules["sounddevice"] = sd
        lst = wl.WakeListener(FakeClient(), voice=FakeVoice())
        lst.start()
        check("слушатель ядра открыл микрофон", _wait(lambda: lst.stage == wl.ST_LISTEN and sd.opened == 1, 20))
        alive = threading.Event()
        alive.set()

        def reporting():
            while alive.is_set():
                wl.report_remote({"running": True, "stage": wl.ST_LISTEN})
                time.sleep(0.3)

        threading.Thread(target=reporting, daemon=True).start()
        check("панель сообщила о себе — ядро закрыло микрофон",
              _wait(lambda: lst.stage == wl.ST_YIELD, 5) and lst.running)
        opened = sd.opened
        time.sleep(1.5)
        check("…и не открывает его, пока панель сообщает", sd.opened == opened)
        alive.clear()
        wl._remote_at -= wl.REMOTE_FRESH_SEC + 1
        check("панель замолчала — ядро снова слушает",
              _wait(lambda: lst.stage == wl.ST_LISTEN and sd.opened == opened + 1, 10))
        lst.stop(wait=5)
        wl._remote_at = 0.0

        # Весь процесс панели: main() с поддельным ядром и микрофоном
        sd = FakeSD()
        sys.modules["sounddevice"] = sd
        out = io.StringIO()
        old_term = signal.getsignal(signal.SIGTERM)

        def driver():
            listening = lambda: any(r.get("stage") == wl.ST_LISTEN and r.get("running") for r in core.reports)
            if _wait(listening, 30):
                core.hold = 30
                _wait(lambda: core.reports[-1].get("held") is True, 10)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=driver, daemon=True).start()
        # Лог процесса — как у панели: всё от INFO
        records: list[logging.LogRecord] = []
        catch = logging.Handler(logging.INFO)
        catch.emit = records.append
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(catch)
        root.setLevel(logging.INFO)
        t0 = time.time()
        try:
            with contextlib.redirect_stdout(out):
                rc = wl.main(["--status", "--core", core.url])
        finally:
            signal.signal(signal.SIGTERM, old_term)
            root.removeHandler(catch)
            root.setLevel(old_level)
        lines = [json.loads(l[len(wl.STATUS_PREFIX):]) for l in out.getvalue().splitlines()
                 if l.startswith(wl.STATUS_PREFIX)]
        check("main(): слушает имена из ядра и сообщает ему о себе", any(
            r.get("stage") == wl.ST_LISTEN and "коннор" in r.get("names", []) for r in core.reports))
        check("…пауза веба из ответа ядра дошла до слушателя", any(r.get("held") for r in core.reports))
        check("…состояние для панели — строками WAKE_STATUS",
              any(l["stage"] == wl.ST_LISTEN and l["running"] for l in lines)
              and any(l["held"] for l in lines))
        check("SIGTERM — код 0, микрофон закрыт, последнее состояние — не слушает",
              rc == 0 and lines and lines[-1]["running"] is False and time.time() - t0 < 60)
        check("в чат ничего не ушло (имя не звучало)", core.chats == [])
        check("отчёты ядру раз в секунду не пишутся в лог",
              len(core.reports) > 2 and not any(r.name.startswith(("httpx", "httpcore")) for r in records))
    finally:
        core.close()
        if old_sd is not None:
            sys.modules["sounddevice"] = old_sd
        else:
            sys.modules.pop("sounddevice", None)


# ===== I =====

class FakeRes:
    def __init__(self, text, probs):
        self.text, self.ys_probs = text, probs


class FakeRec:
    """Модель на сценарии: каждый кусок звука — следующий шаг (текст,
    вероятности токенов, конец фразы); последний шаг повторяется."""

    class Stream:
        def __init__(self, rec):
            self.rec = rec

        def accept_waveform(self, rate, chunk):
            self.rec.i = min(self.rec.i + 1, len(self.rec.script) - 1)

    def __init__(self, *script):
        self.script = list(script) or [("", [], False)]
        self.i = -1
        self.resets = 0
        self.hotwords = []

    def step(self):
        return self.script[max(self.i, 0)]

    def create_stream(self, hotwords=""):
        self.hotwords.append(hotwords)
        return FakeRec.Stream(self)

    def is_ready(self, s):
        return False

    def decode_stream(self, s):
        pass

    def get_result(self, s):
        return self.step()[0]

    def get_result_all(self, s):
        text, probs, _ = self.step()
        return FakeRes(text, probs)

    def is_endpoint(self, s):
        return self.step()[2]

    def reset(self, s):
        self.resets += 1


def _bpe_proto(pieces):
    """Маленький bpe.model: ModelProto с кусками и посторонним полем."""
    import struct

    def varint(n):
        out = b""
        while True:
            b, n = n & 0x7F, n >> 7
            out += bytes([b | (0x80 if n else 0)])
            if not n:
                return out

    def field(num, wt, payload):
        return varint(num << 3 | wt) + payload

    def lp(num, data):
        return field(num, 2, varint(len(data)) + data)

    body = lp(2, lp(1, b"trainer spec"))  # trainer_spec — мимо
    for piece, score, typ in pieces:
        sub = lp(1, piece.encode()) + field(2, 5, struct.pack("<f", score)) + field(3, 0, varint(typ))
        body += lp(1, sub)
    return body + lp(3, b"normalizer")


def test_english():
    section("I. Английский: две модели, язык фразы, английские слова")
    from app.voice.wake_match import to_latin
    np = None
    try:
        import numpy as np
    except Exception:
        pass

    # --- языки, папки, словарь кусков ---
    saved = os.environ.get("WAKE_LANG")
    for raw, want in [("", ("ru", "en")), ("auto", ("ru", "en")), ("both", ("ru", "en")), ("ru", ("ru",)),
                      ("en", ("en",)), ("en,ru", ("en", "ru")), ("RU + EN", ("ru", "en")), ("fr", ("ru", "en"))]:
        os.environ["WAKE_LANG"] = raw
        check(f"WAKE_LANG «{raw}» → {want}", wl.wake_langs() == want)
    os.environ["WAKE_LANG"] = saved or "ru"
    os.environ["WAKE_MODEL_DIR_EN"] = "/tmp/x-en"
    check("папка английской модели — WAKE_MODEL_DIR_EN", wl.model_dir("en") == Path("/tmp/x-en"))
    os.environ.pop("WAKE_MODEL_DIR_EN")
    check("…иначе data/models/wake-en", wl.model_dir("en").parts[-2:] == ("models", "wake-en"))
    tmp = Path(tempfile.mkdtemp(prefix="wake_en_"))
    (tmp / "bpe.model").write_bytes(_bpe_proto([("<unk>", 0.0, 2), ("▁THE", -3.39114, 1), ("S", -3.23764, 1)]))
    wl.spm_vocab(tmp / "bpe.model", tmp / "bpe.vocab")
    check("словарь кусков из bpe.model (protobuf руками)",
          (tmp / "bpe.vocab").read_text("utf-8") == "<unk>\t0\n▁THE\t-3.39114\nS\t-3.23764\n")
    real = ROOT / "data" / "models" / "wake-en" / "bpe.model"
    if real.is_file():
        wl.spm_vocab(real, tmp / "real.vocab")
        lines = (tmp / "real.vocab").read_text("utf-8").splitlines()
        check(f"словарь настоящей модели: {len(lines)} кусков, «▁THE» на месте",
              len(lines) == 500 and lines[0] == "<blk>\t0" and "▁THE\t-3.39114" in lines)

    # --- подсказка имён латиницей ---
    by = {p.phrase: p for p in PHRASES}
    check("у «коннор» своё написание латиницей — connor", by["коннор"].latin == "connor")
    check("у «жабка» латиницы нет", by["жабка"].latin == "")
    check("транслит: «Жабка» → ZHABKA, «Дон Кихот» → DON KIKHOT, «Connor's» → CONNOR'S",
          to_latin("Жабка") == "ZHABKA" and to_latin("Дон Кихот") == "DON KIKHOT" and to_latin("Connor's") == "CONNOR'S")
    hw = wl.hotwords_for(PHRASES, "en")
    check(f"подсказка английской модели — заглавной латиницей: {hw}",
          all(f"{w} :{wl.HOTWORD_SCORE}" in hw.split("/") for w in ("CONNOR", "ZHABKA", "ARRODES", "MIRROR", "ZERKALO", "AXEL"))
          and "KONNOR" not in hw)
    check("подсказка русской модели — как раньше", wl.hotwords_for(PHRASES, "ru") == wl.hotwords_for(PHRASES))

    # --- выбор языка (поддельные модели) ---
    def dec(ru_steps, en_steps, lang=None):
        ru, en = FakeRec(*ru_steps), FakeRec(*en_steps)
        return wl.Decoder({"ru": ru, "en": en}, PHRASES, lang=lang), ru, en

    d, ru, en = dec([("кана в адства", [-1.35] * 4, False)], [("CONNOR WHAT'S THE WEATHER", [-0.64] * 5, False)])
    check("каждая модель — со своей подсказкой имён",
          "коннор" in ru.hotwords[0] and "CONNOR" in en.hotwords[0] and "CONNOR" not in ru.hotwords[0])
    text, end = d.feed(16000, [0.0])
    check(f"английская речь — текст английской модели строчными: «{text}»",
          text == "connor what's the weather" and d.heard_lang == "en" and not end and d.alts == {"ru": "кана в адства"})
    d, *_ = dec([("коннор какая сегодня погода", [-0.76] * 4, False)], [("GUNNER CAGAISI WAGNA PAGODA", [-1.0] * 6, False)])
    check("русская речь — русская модель", d.feed(16000, [0.0])[0] == "коннор какая сегодня погода" and d.heard_lang == "ru")
    yes_ru, yes_en = [("яс", [-0.43] * 2, True)], [("YES", [-0.41] * 2, True)]
    d, *_ = dec(yes_ru, yes_en, lang="ru")
    check("почти поровну («yes» / «яс») — язык разговора: русский", d.feed(16000, [0.0])[0] == "яс")
    d, *_ = dec(yes_ru, yes_en, lang="en")
    check("…и английский", d.feed(16000, [0.0])[0] == "yes")
    d, ru, en = dec([("", [], False)], [("AND", [-3.7], True)])
    text, end = d.feed(16000, [0.0])
    check("«and» английской модели на ровном звуке (уверенность −3.7) — не речь",
          text == "" and d.alts == {} and not end)
    d, ru, en = dec([("привет", [-0.5], False), ("привет как дела", [-0.4] * 3, True)],
                    [("PRIVET", [-1.4] * 2, True), ("PRIVET KAK DELA", [-1.2] * 4, True)])
    text, end = d.feed(16000, [0.0])
    check("конец фразы — по модели выбранного языка: у английской пауза, у русской ещё нет",
          text == "привет" and not end and ru.resets == en.resets == 0)
    text, end = d.feed(16000, [0.0])
    check("…у русской — фраза кончилась, сброшены обе", text == "привет как дела" and end and ru.resets == en.resets == 1)
    d, ru, en = dec([("", [], True)], [("", [], False)], lang="ru")
    check("ничего не слышно — конец по языку разговора (пауза 2.4 с)", d.feed(16000, [0.0]) == ("", True))
    d, *_ = dec([("кана", [-1.7] * 2, True)], [("CONNOR", [-1.4] * 2, True)], lang="ru")
    d.feed(16000, [0.0])
    check("одно слово на другом языке язык разговора не меняет", d.lang == "ru")
    d, *_ = dec([("ваат тайм", [-1.2] * 3, True)], [("WHAT TIME IS IT", [-0.4] * 4, True)], lang="ru")
    d.feed(16000, [0.0])
    check("фраза на другом языке — меняет", d.lang == "en")
    d, *_ = dec([("ясно сеньор", [-0.3] * 3, False)], [("SUNNY", [-0.9], False)])
    d.lock = "en"
    check("lock (звучит её английский ответ) — только английская модель", d.feed(16000, [0.0])[0] == "sunny")
    single = FakeRec(("коннор привет ", [-0.5], True))
    d1 = wl.Decoder(single, PHRASES)
    check("один язык — как раньше (текст модели как есть, свой конец)",
          d1.langs == ("ru",) and d1.feed(16000, [0.0]) == ("коннор привет ", True) and single.resets == 1)

    # --- слушатель ---
    lst = make()
    lst._alts = {"en": "connor what's the weather like"}
    todo = run_final(lst, "кана в адства")
    check(f"имя расслышала только английская модель — просьба её текстом: {todo}",
          todo == ("connor", "what's the weather like") and lst._heard_lang == "en")
    lst = make()
    run_final(lst, "коннор")
    lst._alts = {"en": "axel hello there"}
    todo = run_final(lst, "привет")
    check("в ожидании просьбы имя в тексте другой модели не ищется", todo == ("connor", "привет"))
    lst = make()
    run_final(lst, "коннор")
    check("«humph» на вздох — не просьба, ждём дальше", run_final(lst, "humph") is None and lst.state == wl.AWAIT)
    check("«hmm» и «and» — тоже", run_final(lst, "hmm") is None and run_final(lst, "and") is None and lst.client.sent == [])
    check("а «hmm okay» — уже реплика", run_final(lst, "hmm okay") == ("connor", "hmm okay"))
    for phrase, want in [("stop", wl.TALK_STOP), ("that's enough", wl.TALK_STOP), ("okay that's all", wl.TALK_STOP),
                         ("never mind", wl.TALK_STOP), ("goodbye", wl.TALK_BYE), ("okay bye", wl.TALK_BYE),
                         ("see you later", wl.TALK_BYE), ("good night", wl.TALK_BYE), ("wait", wl.TALK_HUSH),
                         ("hold on", wl.TALK_HUSH), ("one second", wl.TALK_HUSH), ("wait let me say", wl.TALK_HUSH),
                         ("wait what time is it", None), ("stop the music", None), ("done", None), ("later", None),
                         ("хватит", wl.TALK_STOP), ("ну пока", wl.TALK_BYE)]:
        check(f"«{phrase}» → {want}", wl.talk_end(phrase) == want)
    import httpx
    lst = make(FakeClient(error=httpx.ConnectError("refused")))
    lst._alts = {"en": "connor hello"}
    run_final(lst, "кана хэлоу")
    check("сбой — сказано на языке просьбы", lst.voice.said == ["The core isn't responding."])
    check("…по-русски — как было", wl._error_phrase(httpx.ConnectError("x")) == "Ядро не отвечает.")

    ref = wl.EchoRef("It's sunny, 12 degrees, and the wind will pick up tonight.")
    check("её английский ответ: язык en, свои слова узнаются, ваши — нет",
          ref.lang == "en" and ref.mark("sunny") is False and ref.mark("tonight") is False and ref.mark("tomorrow") is True)
    check("…цифры она читает словами — «twelve» её", ref.mark("twelve") is False)
    words = "it s sunny twelve degrees and the what about tomorrow".split()
    k = wl.barge_start(words, ref, lambda tail, mine: mine >= 2)
    check(f"поверх её английского — ваши слова с «what»: {k}", k is not None and words[k] == "what")
    lst = make()
    lst._echo, lst._echo_until = ref, float("inf")
    check("один русский язык — её английский ответ не сверить (как раньше)",
          lst._through_echo("it s sunny what about tomorrow") is None)
    lst.langs = ("ru", "en")
    lst._seg_cut = None
    check("с английской моделью — сверить можно", lst._through_echo("it s sunny what about tomorrow") == "what about tomorrow")

    if np is not None:
        lst = make(FakeClient(reply="Sunny, twelve degrees."))
        d, *_ = dec([("", [], False)], [("", [], False)], lang="ru")
        run_final(lst, "коннор какая погода")
        lst.process(d, 16000, np.zeros(1600, np.float32))
        check("ответила по-английски — разговор английский (перевес короткому «yes»)", d.lang == "en" and lst.lang == "en")
        lst._talk = {"persona": "connor", "phase": wl.TALK_SPEAK, "text": "", "sent": "", "personas": ["connor"],
                     "cut": 0, "cut_persona": ""}
        lst._echo, lst._echo_until = wl.EchoRef("Sunny, twelve degrees."), float("inf")
        lst.process(d, 16000, np.zeros(1600, np.float32))
        check("пока звучит её английский ответ — слушаем английской моделью", d.lock == "en")
        lst._echo_until = time.monotonic() - 1
        lst.process(d, 16000, np.zeros(1600, np.float32))
        check("…договорила — снова обе", d.lock is None)
        lst = make()
        d1 = wl.Decoder(FakeRec(("", [], False)), PHRASES)
        lst._echo, lst._echo_until = wl.EchoRef("Sunny."), float("inf")
        lst.process(d1, 16000, np.zeros(1600, np.float32))
        check("один язык — lock не ставится", d1.lock is None and lst.langs == ("ru",))

    # --- модели: одна не загрузилась ---
    real_ensure, real_make = wl.ensure_model, wl.make_recognizer

    def ensure(d, lang="ru"):
        if lang == "en":
            raise OSError("нет сети")

    wl.ensure_model, wl.make_recognizer = ensure, lambda d, lang="ru": FakeRec()
    try:
        os.environ["WAKE_LANG"] = "ru,en"
        lst = make()
        recs = lst._load_models()
        st = lst.status()
        check(f"английская не скачалась — слушаем русской, причина в состоянии: {st['lang_error']}",
              list(recs) == ["ru"] and st["langs"] == ["ru"] and "en: нет сети" in st["lang_error"])
        os.environ["WAKE_LANG"] = "en"
        try:
            make()._load_models()
            failed = False
        except OSError:
            failed = True
        check("единственная модель не загрузилась — сбой, как раньше", failed)
        os.environ["WAKE_LANG"] = "ru,en"
        wl.ensure_model = lambda d, lang="ru": None
        lst = make()
        check("обе на месте — langs ru, en", list(lst._load_models()) == ["ru", "en"] and lst.status()["langs"] == ["ru", "en"])
    finally:
        wl.ensure_model, wl.make_recognizer = real_ensure, real_make
        os.environ["WAKE_LANG"] = saved or "ru"
    check("слушатель панели сообщает ядру языки", {"langs", "lang", "lang_error"} <= set(wl._REMOTE_KEYS))
    shutil.rmtree(tmp, ignore_errors=True)


def test_real_english():
    section("I2. Английский на настоящих моделях (say: Samantha, Daniel, Tessa; русский — Milena)")
    try:
        import numpy as np
        import sherpa_onnx  # noqa: F401
    except Exception:
        print("  (пропуск — нет sherpa-onnx)")
        return
    ru_dir = Path(os.getenv("WAKE_MODEL_DIR") or ROOT / "data" / "models" / "wake-ru")
    en_dir = Path(os.getenv("WAKE_MODEL_DIR_EN") or ROOT / "data" / "models" / "wake-en")
    if not wl.model_ready(ru_dir, "ru") or not wl.model_ready(en_dir, "en") or not shutil.which("say"):
        print(f"  (пропуск — нет моделей в {ru_dir} и {en_dir} или say)")
        return
    recs = {"ru": wl.make_recognizer(ru_dir, "ru"), "en": wl.make_recognizer(en_dir, "en")}
    tmp = Path(tempfile.mkdtemp(prefix="wake_en_wav_"))
    rng = np.random.default_rng(7)

    def clip(voice, text):
        wav = tmp / f"c{len(list(tmp.iterdir()))}.wav"
        subprocess.run(["say", "-v", voice, "-o", str(wav), "--data-format=LEI16@16000", text], check=True)
        with wave.open(str(wav)) as f:
            return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768

    def quiet(sec, dc=False):
        n = int(16000 * sec)
        return np.full(n, 1e-4, np.float32) if dc else (rng.standard_normal(n) * 1e-4).astype(np.float32)

    def feed(lst, dec, x):
        x = np.concatenate([quiet(0.3), x, quiet(2.0)])
        for k in range(0, len(x), 1600):
            lst.process(dec, 16000, x[k:k + 1600])

    def fresh(client=None, lang=None):
        lst = make(client)
        lst.handle = lambda p, m, lst=lst: lst.client.sent.append((p, m))
        return lst, wl.Decoder(recs, lst.phrases, lang=lang)

    cases = [
        ("Samantha", "Connor, what's the weather like today?", "connor", "weather"),
        ("Daniel", "Connor, open my notes please", "connor", "notes"),
        ("Moira", "Connor, remind me to call mom", "connor", "remind"),
        ("Tessa", "Axel, play some music", "axel", "music"),
        ("Samantha", "Rodion, are you there?", None, None),
        ("Samantha", "We went to the movies yesterday and it was boring", None, None),
    ]
    for voice, phrase, persona, key in cases:
        lst, dec = fresh()
        t = time.time()
        feed(lst, dec, clip(voice, phrase))
        sent = lst.client.sent
        if persona is None:
            check(f"[{voice}] «{phrase}» — никого не будит (слышно: «{lst.last_heard}»)", sent == [])
        else:
            check(f"[{voice}] «{phrase}» → {sent} ({time.time() - t:.2f} с)",
                  len(sent) == 1 and sent[0][0] == persona and key in sent[0][1])
    for phrase, want in SPOKEN:
        lst, dec = fresh()
        feed(lst, dec, clip("Milena", phrase))
        sent = lst.client.sent
        check(f"[Milena] «{phrase}» с двумя моделями → {sent[0] if sent else '—'}",
              sent == ([] if want is None else [want]))

    # Короткий ответ — на языке разговора (на нём ответила персона)
    for reply, voice, said, want in [("Do you want me to set it?", "Samantha", "Yes", "yes"),
                                     ("Поставить?", "Milena", "Да", "да"),
                                     ("Should I add it to the list?", "Samantha", "No thanks", "no thanks")]:
        lst, dec = fresh(FakeClient(reply=reply))
        lst.handle = wl.WakeListener.handle.__get__(lst)
        run_final(lst, "коннор поставь напоминание")
        lst.handle = lambda p, m, lst=lst: lst.client.sent.append((p, m))
        feed(lst, dec, clip(voice, said))
        check(f"ответила «{reply}» — «{said}» уходит как {lst.client.sent[1:]}",
              lst.client.sent[1:] == [("connor", want)])

    lst, dec = fresh()
    run_final(lst, "коннор")
    for _ in range(4):
        feed(lst, dec, quiet(2.0, dc=True))
    check("ровный звук (английская пишет на нём «and») — в ожидании просьбы ничего не ушло",
          lst.client.sent == [] and lst.state == wl.AWAIT)

    # Перебить её английский ответ: её голос «из колонок» себя не перебивает,
    # ваша английская фраза поверх — перебивает
    saved_pause = wl.TALK_PAUSE_SEC
    wl.TALK_PAUSE_SEC = 0.0

    class StopVoice(FakeVoice):
        def __init__(self):
            super().__init__()
            self.stopped = 0

        def stop(self):
            self.stopped += 1

    def echo(x):
        spec = np.fft.rfft(x)
        f = np.fft.rfftfreq(len(x), 1 / 16000)
        spec[(f < 250) | (f > 5000)] = 0
        y = np.fft.irfft(spec, len(x)).astype(np.float32) * 0.7
        out = y.copy()
        for dl, a in [(0.012, 0.5), (0.035, 0.3), (0.08, 0.15)]:
            k = int(dl * 16000)
            out[k:] += y[:-k] * a
        return out + (rng.standard_normal(len(out)) * 0.003).astype(np.float32)

    def barge(reply, you=None, voice="Karen", gain=5.0):
        import threading
        lst = wl.WakeListener(FakeClient(), voice=StopVoice())
        lst.phrases = list(PHRASES)
        lst._phrases_at = time.monotonic()
        lst.with_portrait = True
        lst._talk = {"persona": "connor", "phase": wl.TALK_SPEAK, "text": "", "sent": "",
                     "personas": ["connor"], "cut": 0, "cut_persona": ""}
        lst.state, lst.persona = wl.BUSY, "connor"
        lst._echo, lst._echo_until = wl.EchoRef(reply), float("inf")
        her = echo(clip("Samantha", reply))
        # Вы у микрофона, она — из колонок: ваш голос заметно громче (+14 дБ).
        # Вровень с ней английская модель держится за её фразу и ваших слов
        # не пишет вовсе (замер: Daniel ×1…×5) — перебить её тогда нельзя
        yours = clip(voice, you) * gain if you else None
        at = int(len(her) * 0.4)
        sent = []
        real_start = threading.Thread.start

        def start(th):
            if th.name == "wake-turn":
                sent.append(th._args)
            else:
                real_start(th)

        threading.Thread.start = start
        try:
            dec = wl.Decoder(recs, lst.phrases)
            n = max(len(her), at + (len(yours) if yours is not None else 0)) + 24000
            stop_at = None
            for k in range(0, n, 1600):
                chunk = (rng.standard_normal(1600) * 1e-4).astype(np.float32)
                h = her[k:k + 1600]
                if lst.voice.stopped and stop_at is None:
                    stop_at = k + 960
                if stop_at is not None:
                    h = h.copy()
                    h[max(0, stop_at - k):] = 0
                chunk[:len(h)] += h
                if yours is not None and k + 1600 > at:
                    a, b = max(at, k), min(at + len(yours), k + 1600)
                    if a < b:
                        chunk[a - k:b - k] += yours[a - at:b - at]
                lst.process(dec, 16000, chunk)
        finally:
            threading.Thread.start = real_start
        return lst, sent

    replies = ["It's sunny and twelve degrees, and the wind will pick up from the sea tonight.",
               "Okay, I added bread, milk, eggs and batteries for the remote. Anything else?"]
    for r in replies:
        lst, sent = barge(r)
        check(f"её английский голос из колонок — сама себя не перебивает («{r[:30]}…»)",
              lst.voice.stopped == 0 and not sent)
    lst, sent = barge(replies[0], "wait, what about tomorrow?")
    check(f"«wait, what about tomorrow» поверх неё (вы громче колонок) — замолчала, ушло: {sent}",
          lst.voice.stopped == 1 and len(sent) == 1 and "morrow" in sent[0][1])
    wl.TALK_PAUSE_SEC = saved_pause
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_match()
    test_machine()
    test_talk()
    test_barge()
    test_pause()
    test_waiting()
    test_helpers()
    test_real()
    test_real_barge()
    test_loop()
    test_near()
    test_real_near()
    test_english()
    test_real_english()
    test_core()
    test_panel()
    print(f"\n{ok - failures}/{ok} OK" + (f", FAIL: {failures}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
