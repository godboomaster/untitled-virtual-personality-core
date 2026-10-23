"""Тест устройств ядра и «что видит робот?» (app/core/devices.py,
app/features/device_look.py, маршруты /api/devices/* и _prepare_turn_input
в app/api/server.py):

  - реестр: hello, имена для обращения, файл devices.json переживает
    перезапуск, «на связи» — только пока устройство опрашивает;
  - команды: итог устройства доходит до вызывающего; неизвестное,
    не умеющее, не на связи, не ответившее и отказавшее устройство —
    DeviceError с кодом; опоздавший итог выбрасывается; команда из
    оборванного опроса возвращается в очередь;
  - фразы: просьбы посмотреть камерой устройства узнаются (RU/EN) по его
    именам, посторонние фразы и отрицания — нет;
  - ход веба: кадр — только локальной модели, персоне — описание, кадр —
    к ответу; не на связи — 503 с понятным текстом; из скина, в режиме
    управления и в ходе команды телефону — обычный ход;
  - маршруты: токен, проверка полей, опрос и итог по HTTP;
  - потоковый чат: кадр уходит событием photo, пока модель его смотрит,
    и ещё раз в финальном images.

Камера и модели не вызываются — всё подменено.
Запуск: PYTHONPATH=. python3 -m scripts.test_devices
"""

import asyncio
import base64
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["VPC_DATA_DIR"] = tempfile.mkdtemp(prefix="devices_")
os.environ["OLLAMA_URL"] = "http://127.0.0.1:9"

ok = 0
failures = 0


def check(name, cond):
    global ok, failures
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    ok += 1
    if not cond:
        failures += 1


def section(title):
    print(f"\n── {title} ──")


def reset():
    from app.core import devices
    with devices._lock:
        devices._devices = None
        devices._seen.clear()
        devices._queues.clear()
        devices._cmds.clear()
        devices._waiters.clear()
    try:
        (Path(os.environ["VPC_DATA_DIR"]) / "devices.json").unlink()
    except FileNotFoundError:
        pass


class LoopThread:
    """Event loop API в своём потоке: долгий опрос устройства идёт на нём,
    а command() ждёт итог в вызывающем потоке — как в ядре."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def __exit__(self, *a):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)


def test_registry():
    section("A. Реестр устройств")
    from app.core import devices
    reset()
    d = devices.register("vpc-pi", "Робот", ["робот", "Robot", "  робот  "], ["camera.snapshot"])
    check("hello: имя, имена в нижнем регистре без повторов",
          d["name"] == "Робот" and d["names"] == ["робот", "robot"])
    check("…сразу на связи", d["online"] and devices.online("vpc-pi"))
    check("…с камерой", [x["id"] for x in devices.with_capability("camera.snapshot")] == ["vpc-pi"])
    check("…без лампы", devices.with_capability("lamp.set") == [])

    path = Path(os.environ["VPC_DATA_DIR"]) / "devices.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    check("реестр записан в devices.json", saved["devices"][0]["id"] == "vpc-pi")

    with devices._lock:
        devices._devices = None
        devices._seen.clear()
    after = devices.list_devices()
    check("после перезапуска ядра устройство известно, но не на связи",
          len(after) == 1 and after[0]["names"] == ["робот", "robot"] and not after[0]["online"])

    devices.register("vpc-pi", "Робот", ["робот"], ["camera.snapshot"])
    with devices._lock:
        devices._seen["vpc-pi"]["seen"] -= devices.ONLINE_SEC + 1
    check("давно не опрашивало — не на связи", not devices.online("vpc-pi"))

    check("forget убирает устройство", devices.forget("vpc-pi") and devices.list_devices() == [])
    check("…второй раз — False", not devices.forget("vpc-pi"))


def test_commands():
    section("B. Команды и итоги")
    from app.core import devices
    reset()

    def err(fn):
        try:
            fn()
        except devices.DeviceError as e:
            return e
        return None

    e = err(lambda: devices.command("nope", "camera.snapshot", timeout=1))
    check("неизвестное устройство — unknown", e is not None and e.code == "unknown")

    devices.register("pi", "Робот", ["робот"], ["camera.snapshot"])
    e = err(lambda: devices.command("pi", "lamp.set", timeout=1))
    check("не умеет — unsupported", e is not None and e.code == "unsupported")

    with devices._lock:
        devices._seen["pi"]["seen"] -= devices.ONLINE_SEC + 1
    t0 = time.monotonic()
    e = err(lambda: devices.command("pi", "camera.snapshot", timeout=5))
    check("не на связи — offline сразу, без ожидания",
          e is not None and e.code == "offline" and time.monotonic() - t0 < 1)

    devices.register("pi", "Робот", ["робот"], ["camera.snapshot"])
    with LoopThread() as lt:
        async def device_side():
            cmds = await devices.next_commands("pi", 5)
            devices.complete("pi", cmds[0]["id"], True, image=b"JPEG", data={"w": 1600})
            return cmds

        fut = lt.run(device_side())
        res = devices.command("pi", "camera.snapshot", {"rotate": 180}, timeout=5)
        cmds = fut.result(timeout=5)
    check("долгий опрос получил команду с аргументами",
          cmds and cmds[0]["action"] == "camera.snapshot" and cmds[0]["args"] == {"rotate": 180})
    check("итог дошёл до вызывающего", res["ok"] and res["image"] == b"JPEG" and res["data"] == {"w": 1600})
    check("…и команда снята", not devices._cmds and not devices._queues.get("pi"))

    t0 = time.monotonic()
    e = err(lambda: devices.command("pi", "camera.snapshot", timeout=0.3))
    check("никто не взял — timeout, очередь пуста",
          e is not None and e.code == "timeout" and not devices._queues.get("pi") and not devices._cmds)

    with LoopThread() as lt:
        async def failing():
            cmds = await devices.next_commands("pi", 5)
            devices.complete("pi", cmds[0]["id"], False, error="камера занята")

        lt.run(failing())
        e = err(lambda: devices.command("pi", "camera.snapshot", timeout=5))
    check("устройство отказало — failed с его текстом",
          e is not None and e.code == "failed" and e.detail == "камера занята")

    with LoopThread() as lt:
        late = {}

        async def slow():
            cmds = await devices.next_commands("pi", 5)
            await asyncio.sleep(0.5)
            late["ok"] = devices.complete("pi", cmds[0]["id"], True, image=b"x")

        fut = lt.run(slow())
        e = err(lambda: devices.command("pi", "camera.snapshot", timeout=0.2))
        fut.result(timeout=5)
    check("итог после таймаута вызывающего выбрасывается (complete → False)",
          e is not None and e.code == "timeout" and late.get("ok") is False)

    check("итог чужой команды — False", not devices.complete("pi", "nonexistent", True))

    with LoopThread() as lt:
        gone_flag = {"v": False}

        async def gone():
            return gone_flag["v"]

        async def poll_then_vanish():
            return await devices.next_commands("pi", 5, gone=gone)

        def enqueue():
            try:
                devices.command("pi", "camera.snapshot", timeout=3)
            except devices.DeviceError:
                pass

        gone_flag["v"] = True
        worker = threading.Thread(target=enqueue)
        fut = lt.run(poll_then_vanish())
        time.sleep(0.1)
        worker.start()
        first = fut.result(timeout=5)
        second = lt.run(devices.next_commands("pi", 1)).result(timeout=5)
        for c in second:
            devices.complete("pi", c["id"], True, image=b"y")
        worker.join(timeout=5)
    check("опрос оборвался после взятия — команда вернулась в очередь",
          first == [] and len(second) == 1)

    check("опрос неизвестного устройства — None (нужен hello)",
          LoopThreadRun(devices.next_commands("ghost", 0)) is None)

    async def stop_wait():
        devices.set_stop_check(lambda: True)
        try:
            t = time.monotonic()
            out = await devices.next_commands("pi", 30)
            return out, time.monotonic() - t
        finally:
            devices.set_stop_check(None)

    out, took = LoopThreadRun(stop_wait())
    check("ядро останавливается — опрос отвечает сразу", out == [] and took < 3)


def LoopThreadRun(coro):
    with LoopThread() as lt:
        return lt.run(coro).result(timeout=10)


def test_phrases():
    section("C. Узнаём просьбу посмотреть камерой устройства")
    from app.features.device_look import is_device_request
    names = ["робот", "robot"]
    yes = [
        "что видит робот?",
        "Коннор, что сейчас видит робот?",
        "что робот видит",
        "что вокруг робота",
        "кто перед роботом?",
        "что на камере робота",
        "покажи камеру робота",
        "посмотри через робота",
        "смотри через робота, что там",
        "глянь камерой робота, что там",
        "посмотри, что в комнате, через робота",
        "сфоткай роботом",
        "сделай фото роботом",
        "сними на робота",
        "Робот, посмотри вокруг",
        "робот, что ты видишь?",
        "what does the robot see?",
        "what is around the robot",
        "look through the robot",
        "take a photo with the robot",
        "Robot, look around",
        "what's on the robot's camera",
    ]
    no = [
        "я собираю робота",
        "посмотри код робота",
        "сними робота с полки",
        "не смотри через робота",
        "не снимай робота",
        "робот сломался?",
        "посмотри на экран",
        "что видит кот",
        "как работает камера",
        "robot vacuum broke",
        "don't look through the robot",
        "",
    ]
    for t in yes:
        check(f"да: «{t}»", is_device_request(t, names))
    for t in no:
        check(f"нет: «{t}»", not is_device_request(t, names))
    check("другое имя устройства: «что видит тележка» — по имени «тележка»",
          is_device_request("что видит тележка", ["тележка"]))
    check("…а у робота — нет", not is_device_request("что видит тележка", names))
    check("имя из двух слов: «что видит малый робот»",
          is_device_request("что видит малый робот", ["малый робот"]))
    check("без имён — никогда", not is_device_request("что видит робот", []))


class FakeLocal:
    def __init__(self, answer="TEXT: нет\nDESCRIPTION: комната, светлый шкаф"):
        self.answer = answer
        self._available = True
        self.calls = []

    def ocr_image(self, image_bytes, question="", lang=None, subject="image"):
        self.calls.append({"bytes": image_bytes, "question": question, "subject": subject})
        return self.answer


def test_turn_input():
    section("D. Ход веба: «что видит робот?»")
    from fastapi import HTTPException
    from app.api import server
    from app.api.schemas import ChatRequest, PhoneActionReport
    from app.core import devices
    from app.features import device_look, screen_look
    reset()
    devices.register("pi", "Робот", ["робот", "robot"], ["camera.snapshot"])

    def make_bot(local=None, control=False):
        bot = mock.MagicMock()
        bot._local_router = local
        bot.control_mode_on.return_value = control
        bot.chat_user_language.return_value = "ru"
        return bot

    shot = {"ok": True, "image": b"\xff\xd8JPEG", "data": {}, "error": None}
    local = FakeLocal()
    bot = make_bot(local=local)
    photos = []
    with mock.patch.object(devices, "command", return_value=shot) as cmd:
        llm, shown = server._prepare_turn_input(
            bot, ChatRequest(persona="connor", message="что видит робот?"), photos)
    check("команда снимка ушла устройству", cmd.called and cmd.call_args.args[:2] == ("pi", "camera.snapshot"))
    check("кадр смотрит локальная модель", local.calls and local.calls[0]["bytes"] == shot["image"]
          and local.calls[0]["subject"] == "image")
    check("облачный vision не вызывается", not bot.describe_image.called)
    check("в ходе — пометка устройства с именем и описание",
          device_look.DEVICE_NOTE.format(name="Робот") in llm and "светлый шкаф" in llm)
    check("в истории «📷 …»", shown == "📷 что видит робот?")
    check("кадр — к ответу, с подписью-именем", photos == [(shot["image"], "Робот")])

    server._attach_photos(bot, "c1", photos)
    check("_attach_photos кладёт кадр в бакет ответа",
          bot.add_pending_photo.call_args.args == ("c1", shot["image"], "Робот"))

    from app.bot_instance import BotInstance
    fake = SimpleNamespace(_pending_photos={})
    BotInstance.add_pending_photo(fake, "c1", b"img", "Робот")
    check("BotInstance.add_pending_photo → pop_pending_photos отдаёт кадр",
          BotInstance.pop_pending_photos(fake, "c1") == [{"data": b"img", "caption": "Робот"}])

    with mock.patch.object(devices, "command", return_value=shot):
        llm, shown = server._prepare_turn_input(
            make_bot(local=FakeLocal()), ChatRequest(persona="connor", message="что видит робот?"))
    check("без списка photos (как зовут прежние тесты) — ход тот же",
          shown == "📷 что видит робот?" and "светлый шкаф" in llm)

    for code, expect in [("offline", device_look.OFFLINE.format(name="Робот")),
                         ("timeout", device_look.TIMEOUT.format(name="Робот")),
                         ("failed", device_look.FAILED.format(name="Робот", detail="камера занята"))]:
        with mock.patch.object(devices, "command",
                               side_effect=devices.DeviceError(code, "камера занята" if code == "failed" else "")):
            try:
                server._prepare_turn_input(make_bot(local=FakeLocal()),
                                           ChatRequest(persona="connor", message="что видит робот?"), [])
                e = None
            except HTTPException as ex:
                e = ex
        check(f"{code} — 503 с понятным текстом", e is not None and e.status_code == 503 and e.detail == expect)

    with mock.patch.object(devices, "command", return_value=shot):
        try:
            server._prepare_turn_input(make_bot(local=None),
                                       ChatRequest(persona="connor", message="что видит робот?"), [])
            e = None
        except HTTPException as ex:
            e = ex
    check("без локальной модели — 503 «Ollama недоступна», а не облако",
          e is not None and e.status_code == 503 and e.detail == screen_look.NO_LOCAL_MODEL)

    action = PhoneActionReport(kind="torch", status="ok")
    for req, bot, name in [
        (ChatRequest(persona="connor", message="что видит робот?", from_skin=True),
         make_bot(local=FakeLocal()), "из скина — обычный ход"),
        (ChatRequest(persona="connor", message="что видит робот?"),
         make_bot(local=FakeLocal(), control=True), "в режиме управления — обычный ход"),
        (ChatRequest(persona="connor", message="что видит робот?", phone_action=action),
         make_bot(local=FakeLocal()), "в ходе команды телефону — обычный ход"),
        (ChatRequest(persona="connor", message="что видит кот?"),
         make_bot(local=FakeLocal()), "не про устройство — обычный ход"),
    ]:
        photos = []
        with mock.patch.object(devices, "command", return_value=shot) as cmd:
            llm, shown = server._prepare_turn_input(bot, req, photos)
        check(name, not cmd.called and llm == req.message and shown is None and photos == [])

    devices.forget("pi")
    with mock.patch.object(devices, "command", return_value=shot) as cmd:
        llm, shown = server._prepare_turn_input(
            make_bot(local=FakeLocal()), ChatRequest(persona="connor", message="что видит робот?"), [])
    check("устройств с камерой нет — обычный ход", not cmd.called and shown is None)


def test_routes():
    section("E. Маршруты /api/devices")
    try:
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"  (пропущено — fastapi/httpx недоступны: {e})")
        return
    import app.api.server as server_mod
    from app.core import devices
    reset()

    orig = server_mod._api_token
    server_mod._api_token = "s3cr3t"
    try:
        client = TestClient(server_mod.app, base_url="http://127.0.0.1")
        auth = {"Authorization": "Bearer s3cr3t"}
        hello = {"id": "vpc-pi", "name": "Робот", "names": ["робот", "robot"],
                 "capabilities": ["camera.snapshot"]}

        r = client.post("/api/devices/hello", json=hello)
        check("hello без токена — 401", r.status_code == 401)
        r = client.post("/api/devices/hello", json={**hello, "id": "bad id!"}, headers=auth)
        check("hello: id с пробелом — 422", r.status_code == 422)
        r = client.post("/api/devices/hello", json={**hello, "capabilities": ["Camera Snapshot"]}, headers=auth)
        check("hello: возможность не по формату — 422", r.status_code == 422)
        r = client.post("/api/devices/hello", json={**hello, "names": ["x" * 33]}, headers=auth)
        check("hello: имя длиннее 32 — 422", r.status_code == 422)
        r = client.post("/api/devices/hello", json=hello, headers=auth)
        check("hello — 200, устройство на связи",
              r.status_code == 200 and r.json()["device"]["online"] is True)
        r = client.get("/api/devices", headers=auth)
        check("GET /api/devices — список", r.status_code == 200
              and [d["id"] for d in r.json()["devices"]] == ["vpc-pi"])

        r = client.get("/api/devices/ghost/commands", params={"wait": 0}, headers=auth)
        check("опрос без hello — 404", r.status_code == 404)
        r = client.get("/api/devices/vpc-pi/commands", params={"wait": 0}, headers=auth)
        check("опрос без команд, wait=0 — сразу пусто", r.status_code == 200 and r.json() == {"commands": []})

        out = {}

        def caller():
            try:
                out["res"] = devices.command("vpc-pi", "camera.snapshot", timeout=5)
            except devices.DeviceError as e:
                out["err"] = e

        th = threading.Thread(target=caller)
        th.start()
        for _ in range(50):
            if devices._queues.get("vpc-pi"):
                break
            time.sleep(0.02)
        r = client.get("/api/devices/vpc-pi/commands", params={"wait": 0}, headers=auth)
        cmds = r.json()["commands"]
        check("опрос по HTTP отдал команду", len(cmds) == 1 and cmds[0]["action"] == "camera.snapshot")
        r = client.post("/api/devices/vpc-pi/result",
                        json={"command_id": cmds[0]["id"], "ok": True, "image": "%%%"}, headers=auth)
        check("итог с битым base64 — 400", r.status_code == 400)
        img = base64.b64encode(b"\xff\xd8JPEG").decode()
        r = client.post("/api/devices/vpc-pi/result",
                        json={"command_id": cmds[0]["id"], "ok": True, "image": img}, headers=auth)
        th.join(timeout=5)
        check("итог по HTTP принят и дошёл до хода",
              r.status_code == 200 and r.json()["ok"] is True and out.get("res", {}).get("image") == b"\xff\xd8JPEG")
        r = client.post("/api/devices/vpc-pi/result",
                        json={"command_id": cmds[0]["id"], "ok": True, "image": img}, headers=auth)
        check("повторный итог — ok: false", r.status_code == 200 and r.json()["ok"] is False)

        r = client.delete("/api/devices/vpc-pi", headers=auth)
        check("DELETE — устройство забыто", r.status_code == 200 and client.get(
            "/api/devices", headers=auth).json()["devices"] == [])
        r = client.delete("/api/devices/vpc-pi", headers=auth)
        check("…второй DELETE — 404", r.status_code == 404)
    finally:
        server_mod._api_token = orig


class StreamBot:
    """Бот для /api/chat/stream: ровно то, что трогает потоковый ход."""
    features = {}
    router = SimpleNamespace(_last_provider=None, active_provider=None)

    def __init__(self, local):
        self._local_router = local
        self.stm = []
        self.photos = {}
        self.memory = SimpleNamespace(stm=SimpleNamespace(
            get_messages=lambda **kw: list(self.stm),
            pop_last_n=lambda n, key: [self.stm.pop() for _ in range(min(n, len(self.stm)))],
            add_message=lambda role, text, uid, key: self.stm.append(
                {"role": role, "content": text, "timestamp": time.time()})))

    def process_message(self, text, chat_id=None, **kw):
        self.photos[str(chat_id)] = []  # как настоящий: бакет обнуляется в начале хода
        reply = "Вижу комнату."
        self.stm = [{"role": "user", "content": text, "timestamp": time.time()},
                    {"role": "assistant", "content": reply, "timestamp": time.time()}]
        return reply

    def add_pending_photo(self, chat_id, data, caption=""):
        self.photos.setdefault(str(chat_id), []).append({"data": data, "caption": caption})

    def pop_pending_photos(self, chat_id):
        return self.photos.pop(str(chat_id), [])

    def _turn_stm_tail(self, key):
        return self.stm

    def chat_user_language(self, key):
        return "ru"

    def control_mode_on(self, key):
        return False

    def pop_pending_split_messages(self, chat_id):
        return []

    def pop_pending_list_messages(self, chat_id):
        return []

    def pop_pending_question_kind(self, chat_id):
        return None

    def pop_pending_files(self, chat_id):
        return []

    def pop_reply_history_text(self, chat_id):
        return None

    def cc_answer_options(self, *a):
        return None

    def cc_task_card(self, *a):
        return None


def test_stream_photo():
    section("F. Потоковый чат: кадр — сразу, до ответа модели")
    import app.api.server as server_mod
    from app.api.schemas import ChatRequest
    from app.core import devices
    reset()
    devices.register("pi", "Робот", ["робот"], ["camera.snapshot"])

    shot = b"\xff\xd8JPEG-STREAM"
    data_url = "data:image/jpeg;base64," + base64.b64encode(shot).decode()
    released = threading.Event()
    waited = {}

    class SlowLocal(FakeLocal):
        def ocr_image(self, image_bytes, question="", lang=None, subject="image"):
            # Модель «смотрит», пока пользователь не увидел кадр: дождались —
            # значит, кадр ушёл в поток раньше описания
            waited["released"] = released.wait(5)
            return super().ocr_image(image_bytes, question, lang, subject)

    bot = StreamBot(SlowLocal())

    async def fake_get_bot(persona):
        return bot

    async def fake_touch(persona):
        return None

    async def run(req):
        resp = await server_mod.chat_stream(req, SimpleNamespace(headers={"host": "127.0.0.1:8000"}))
        events = []
        async for chunk in resp.body_iterator:
            for frame in chunk.split("\n\n"):
                if frame.startswith("data: "):
                    ev = json.loads(frame[6:])
                    events.append(ev)
                    if "photo" in ev:
                        released.set()
        return events

    orig = server_mod._get_bot, server_mod._touch_persona
    server_mod._get_bot, server_mod._touch_persona = fake_get_bot, fake_touch
    try:
        with mock.patch.object(devices, "command",
                               return_value={"ok": True, "image": shot, "data": {}, "error": None}):
            events = asyncio.run(run(ChatRequest(persona="connor", message="что видит робот?",
                                                 chat_id="web_user")))
    finally:
        server_mod._get_bot, server_mod._touch_persona = orig

    kinds = [next(iter(e)) for e in events]
    photo_at = next((i for i, e in enumerate(events) if "photo" in e), None)
    first_token = next((i for i, e in enumerate(events) if "token" in e), None)
    done = events[-1] if events else {}
    check("событие photo с кадром (dataURL)", photo_at is not None and events[photo_at]["photo"] == data_url)
    check("…пришло, пока модель ещё смотрела кадр", waited.get("released") is True)
    check("…раньше текста ответа", photo_at is not None and first_token is not None and photo_at < first_token)
    check("финал — ответ, и тот же кадр в images (для старых клиентов)",
          done.get("done") and done.get("reply") == "Вижу комнату." and done.get("images") == [data_url])
    check("кадр один раз", kinds.count("photo") == 1)

    released.clear()
    bot2 = StreamBot(FakeLocal())

    async def fake_get_bot2(persona):
        return bot2

    server_mod._get_bot, server_mod._touch_persona = fake_get_bot2, fake_touch
    try:
        with mock.patch.object(devices, "command", return_value={"ok": True, "image": shot}):
            events = asyncio.run(run(ChatRequest(persona="connor", message="привет", chat_id="web_user")))
    finally:
        server_mod._get_bot, server_mod._touch_persona = orig
    check("обычная реплика — без события photo", not any("photo" in e for e in events)
          and events and events[-1].get("done"))

    bot3 = StreamBot(None)

    async def fake_get_bot3(persona):
        return bot3

    server_mod._get_bot, server_mod._touch_persona = fake_get_bot3, fake_touch
    try:
        with mock.patch.object(devices, "command", return_value={"ok": True, "image": shot}):
            events = asyncio.run(run(ChatRequest(persona="connor", message="что видит робот?",
                                                 chat_id="web_user")))
    finally:
        server_mod._get_bot, server_mod._touch_persona = orig
    check("модели нет — кадр всё равно показан, потом ошибка",
          [next(iter(e)) for e in events] == ["photo", "error"])


def main():
    test_registry()
    test_commands()
    test_phrases()
    test_turn_input()
    test_routes()
    test_stream_photo()
    print(f"\nИтого: {ok} проверок, {failures} провалов")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
