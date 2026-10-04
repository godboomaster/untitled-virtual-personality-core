"""Обработчики Telegram на НАСТОЯЩЕЙ библиотеке python-telegram-bot (не фейках).

Настоящие Application (concurrent_updates=True, как в main.py), Bot,
Update.de_json, PhotoSize/Document/File, CommandHandler/MessageHandler и
фильтры, сериализация запросов Bot API (включая multipart альбома). В сеть
не ходит: транспорт — OfflineRequest (подкласс telegram.request.BaseRequest),
он отвечает на sendMessage/getFile/скачивание файла и т. п. правдоподобным
JSON и записывает вызовы. Токен фиктивный.

Заглушено: слой персоны/LLM (_process_message_impl, router, persona) и
менеджеры фич. Настоящие: BotInstance.user_turn_async/process_message/
command_reply/_dispatch_command/_save_assistant_reply/pre_check, гейт хода
ChatTurnGate, register_handlers/create_handlers.

Проверяется: исключений в обработчиках нет (error handler), ход открыт на
время обработки и закрыт после (утечек нет, process_message подхватывает
ход обработчика, новой реплики не заводит), ответы доставлены, порядок STM,
фоновый коммит посреди доставки split-частей отложен.

Нужен python-telegram-bot (requirements.txt): без него — SKIP.
Запуск: PYTHONPATH=<каталог с telegram> python -m scripts.test_telegram_live
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import telegram
    from telegram import Update
    from telegram.ext import Application
    from telegram.request import BaseRequest
except ImportError as _e:  # pragma: no cover
    print(f"SKIP: python-telegram-bot не импортируется ({_e}). Поставь его "
          "(pip install python-telegram-bot, можно --target <dir> + PYTHONPATH) "
          "и запусти снова.")
    sys.exit(0)


TOKEN = "123456:OFFLINE-TEST-TOKEN"   # фиктивный, в сеть ничего не уходит
BOT_ID = 123456
BOT_USERNAME = "persona_test_bot"
IMAGE_BYTES = b"\xff\xd8\xff\xe0FAKEJPEG-big"
DOC_BYTES = "Список покупок: хлеб, молоко.\n".encode("utf-8")


# ─── офлайн-транспорт Bot API ────────────────────────────────

class OfflineRequest(BaseRequest):
    # Отвечает вместо api.telegram.org; calls — список (метод, параметры)

    def __init__(self):
        self.calls = []
        self.fail = {}          # метод → сколько раз подряд отказать (400)
        self.on_call = None     # хук (метод, параметры) — синхронный
        self._mid = 1000
        self.files = {}         # file_path → bytes

    @property
    def read_timeout(self):
        return None

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    def _msg(self, chat_id, **extra):
        self._mid += 1
        m = {"message_id": self._mid, "date": int(time.time()),
             "chat": {"id": int(chat_id), "type": "private"},
             "from": {"id": BOT_ID, "is_bot": True, "first_name": "Persona",
                      "username": BOT_USERNAME}}
        m.update(extra)
        return m

    async def do_request(self, url, method, request_data=None, read_timeout=None,
                         write_timeout=None, connect_timeout=None, pool_timeout=None):
        if "/file/bot" in url:   # File.download_as_bytearray
            path = url.split("/file/bot", 1)[1].split("/", 1)[1]
            self.calls.append(("download", {"path": path}))
            data = self.files.get(path)
            return (200, data) if data is not None else (404, b"")
        api = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        if request_data is not None and request_data.multipart_data:
            params["_multipart"] = sorted(request_data.multipart_data)
        self.calls.append((api, params))
        if self.on_call is not None:
            self.on_call(api, params)
        if self.fail.get(api):
            self.fail[api] -= 1
            return 400, json.dumps({"ok": False, "error_code": 400,
                                    "description": f"Bad Request: test {api}"}).encode()
        chat_id = params.get("chat_id", 1)
        if api == "getMe":
            res = {"id": BOT_ID, "is_bot": True, "first_name": "Persona",
                   "username": BOT_USERNAME, "can_join_groups": True,
                   "can_read_all_group_messages": False,
                   "supports_inline_queries": False}
        elif api == "sendMessage":
            res = self._msg(chat_id, text=params.get("text", ""))
        elif api == "sendPhoto":
            res = self._msg(chat_id, caption=params.get("caption"), photo=[
                {"file_id": "out", "file_unique_id": "out", "width": 10, "height": 10}])
        elif api == "sendMediaGroup":
            res = [self._msg(chat_id, photo=[{"file_id": f"o{i}", "file_unique_id": f"o{i}",
                                              "width": 10, "height": 10}])
                   for i, _ in enumerate(params.get("media") or [])]
        elif api == "sendDocument":
            res = self._msg(chat_id, document={"file_id": "d", "file_unique_id": "d"})
        elif api == "getFile":
            fid = params["file_id"]
            path = f"files/{fid}"
            res = {"file_id": fid, "file_unique_id": f"u_{fid}",
                   "file_size": len(self.files.get(path, b"")), "file_path": path}
        else:   # sendChatAction, setMyCommands, ...
            res = True
        return 200, json.dumps({"ok": True, "result": res}).encode()

    def api_calls(self, *names):
        return [(n, p) for n, p in self.calls if not names or n in names]


# ─── заглушки памяти/персоны/LLM/менеджеров ──────────────────

class FakeStm:
    def __init__(self):
        self.buffers = {}
        self._lock = threading.RLock()

    def _buf(self, chat_id):
        return self.buffers.setdefault(str(chat_id), [])

    def add_message(self, role, content, user_id="default", chat_id=None, user_name=None):
        with self._lock:
            self._buf(chat_id if chat_id is not None else user_id).append(
                {"role": role, "content": content, "timestamp": time.time()})

    def get_messages(self, user_id=None, chat_id=None):
        with self._lock:
            return list(self._buf(chat_id if chat_id is not None else user_id))


class FakeMemory:
    def __init__(self):
        self.stm = FakeStm()

    def add_message(self, role, content, user_id="default", chat_id=None,
                    user_name=None, light_mode=None):
        self.stm.add_message(role, content, user_id, chat_id, user_name)

    def get_context(self, user_id, chat_id, ltm_query=None):
        return self.stm.get_messages(chat_id=chat_id), [], []

    def get_chat_facts_block(self, chat_id, exclude_user_id=None):
        return None


class Recorder:
    # Менеджер-заглушка: любой метод пишет вызов в calls

    def __init__(self, name, returns=None):
        self._name = name
        self.calls = []
        self._returns = returns or {}

    def __getattr__(self, item):
        if item.startswith("_"):
            raise AttributeError(item)

        def _m(*a, **kw):
            self.calls.append((item, a, kw))
            r = self._returns.get(item)
            return r(*a, **kw) if callable(r) else r
        return _m

    def called(self, item):
        return [c for c in self.calls if c[0] == item]


def main():
    tmp = tempfile.mkdtemp(prefix="tg_live_")
    os.environ["DATA_DIR"] = tmp
    os.environ.setdefault("OWNER_USER_ID", "")
    os.chdir(tmp)

    ok = 0
    failures = 0

    def check(name, cond):
        nonlocal ok, failures
        print(f"  [{'OK' if cond else 'FAIL'}] {name}")
        ok += 1
        if not cond:
            failures += 1

    print(f"python-telegram-bot {telegram.__version__}")

    from app import telegram_bot as tb
    from app.bot_instance import BotInstance
    from app.core import file_reader

    tb._send_delay = lambda part: 0.05   # паузы «набора» короткие, но есть

    # ── BotInstance без __init__: настоящие ход/process_message/команды ──
    mem = FakeMemory()
    bot = BotInstance.__new__(BotInstance)
    bot.persona_name = "tester"
    bot.context = "tg_live"
    bot.features = {}
    bot.trigger_words = {"тестер"}
    bot.owner = ""
    bot.web_single_user = False
    bot.blocked_users = {"666"}
    bot.allowed_dm_users = []
    bot._punish_enabled = bot._rate_limit_enabled = bot._moderation_enabled = False
    bot._cc_allowed_users = set()
    bot._web_search_enabled = False
    bot._activity_tracker = None
    bot.rhythm = None
    bot.proactive = None
    bot.self_memory = None
    bot._local_router = None
    bot.memory = mem
    bot._pending_list_messages = {}
    bot._pending_question_kind = {}
    bot._pending_split_messages = {}
    bot._pending_photos = {}
    bot.max_file_size = 20 * 1024 * 1024
    bot.file_db = Recorder("file_db", {"get_loaded_files": lambda uid: ["notes.txt"]})
    bot.file_db.max_docs = 5
    bot.todo_manager = Recorder("todo")
    bot.inventory_manager = Recorder("inventory", {"add_item": "Предмет добавлен."})
    bot.reminder_manager = Recorder("reminder", {"format_delay": lambda d: f"{int(d // 60)} мин"})
    bot.learning_manager = Recorder("learning")
    bot.setup_rhythm = lambda sender: None
    bot.setup_learning = lambda sender: None
    bot._reformulate_task = lambda t: t
    bot._enrich_inventory_item = lambda name, desc="", expires=None, lang=None: (desc or "обычный", None)
    gate = bot._get_turn_gate()

    # Учёт ходов: каждый открытый кадр должен быть закрыт
    opened = []
    real_begin = gate.begin_turn

    def spy_begin(chat_id):
        frame = real_begin(chat_id)
        opened.append(frame)
        return frame

    gate.begin_turn = spy_begin

    def no_leaks():
        return (all(f["closed"] and f["refs"] == 0 for f in opened)
                and not gate._turns)

    split_on = {"v": False}
    bot.persona = SimpleNamespace(
        persona_data={"name": "Тестер"},
        settings=type("S", (), {"get": lambda self, k, d=None:
                                split_on["v"] if k == "split_messages" else d})(),
        prepare_messages=lambda **kw: [{"role": "user", "content": kw["user_message"]}],
        get_settings=lambda: {},
        is_muted=lambda: False,
    )

    # «LLM» и видимость хода изнутри рабочего потока
    seen = []   # (где, чат, кадр хода в контексте, busy)

    def note(where, chat):
        seen.append((where, chat, gate.current_frame(chat), gate.busy(chat)))

    llm = {"answer": "Ответ персоны.", "raise": None, "photos": None}

    def fake_impl(user_input, user_id="default", chat_id=None, user_name=None,
                  reply_context=None, reply_to_bot_message_id=None,
                  on_token=None, raw_user_text=None, from_skin=False):
        note("impl", str(chat_id))
        llm.setdefault("inputs", []).append(
            {"input": user_input, "raw": raw_user_text, "reply_ctx": reply_context,
             "user_name": user_name})
        mem.add_message("user", user_input, user_id, chat_id, user_name)
        if llm["raise"]:
            raise llm["raise"]
        if llm["photos"]:
            bot._pending_photos.setdefault(str(chat_id), []).extend(llm["photos"])
        return bot._save_assistant_reply(llm["answer"], user_id, chat_id)

    bot._process_message_impl = fake_impl

    def fake_get_response(messages, **kw):
        note("cmd_llm", seen_chat["v"])
        return llm["answer"]

    seen_chat = {"v": None}
    img_calls = []

    def fake_image(prompt, image_bytes):
        note("vision", seen_chat["v"])
        img_calls.append((prompt, image_bytes))
        return "TEXT: no text\nDESCRIPTION: кот на диване"

    bot.router = SimpleNamespace(
        get_response=fake_get_response, answer_provider=None,
        get_provider_model_info=lambda: "stub/model",
        supports_vision=lambda: True, get_response_with_image=fake_image)

    file_reader.extract_text = lambda b, name: b.decode("utf-8")

    # ── настоящее Application, как в main.py ──
    req = OfflineRequest()
    req.files["files/ph_big"] = IMAGE_BYTES
    req.files["files/doc1"] = DOC_BYTES
    app = (Application.builder().token(TOKEN).request(req)
           .get_updates_request(OfflineRequest())
           .concurrent_updates(True).updater(None).build())
    tb.register_handlers(app, bot)
    errors = []

    async def on_error(update, context):
        errors.append(context.error)

    app.add_error_handler(on_error)

    uid_seq = iter(range(1, 10_000))

    def user(uid=42):
        return {"id": uid, "is_bot": False, "first_name": "Аня", "username": "anya"}

    def chat(cid=42, kind="private"):
        c = {"id": cid, "type": kind}
        if kind != "private":
            c["title"] = "Группа"
        return c

    def msg_update(*, cid=42, uid=42, kind="private", text=None, caption=None,
                   photo=None, document=None, reply_to=None, field="message",
                   thread_id=None):
        # +1: дата в Bot API — целые секунды, а drop_stale отбрасывает всё
        # старше момента регистрации handlers (с долями секунды)
        m = {"message_id": next(uid_seq), "date": int(time.time()) + 1,
             "chat": chat(cid, kind), "from": user(uid)}
        if field == "channel_post":
            m.pop("from")
            m["chat"] = {"id": cid, "type": "channel", "title": "Канал"}
        if text is not None:
            m["text"] = text
            if text.startswith("/"):
                cmd = text.split(" ", 1)[0]
                m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
        if caption is not None:
            m["caption"] = caption
        if photo is not None:
            m["photo"] = photo
        if document is not None:
            m["document"] = document
        if reply_to is not None:
            m["reply_to_message"] = reply_to
        if thread_id is not None:
            m["message_thread_id"] = thread_id
            m["is_topic_message"] = True
        if field == "edited_message":
            m["edit_date"] = int(time.time())
        return Update.de_json({"update_id": next(uid_seq), field: m}, app.bot)

    def stm(cid):
        return [(m["role"], m["content"]) for m in mem.stm.get_messages(chat_id=str(cid))]

    def sent_texts(since=0):
        return [p.get("text") for n, p in req.calls[since:] if n == "sendMessage"]

    def turn_seen(where, cid, since):
        # Каждый вызов where в чате cid шёл внутри хода (кадр в контексте, busy)
        hits = [s for s in seen[since:] if s[0] == where and s[1] == str(cid)]
        return bool(hits) and all(s[2] is not None and s[3] for s in hits)

    async def run():
        await app.initialize()
        try:
            await scenarios()
        finally:
            await app.shutdown()

    async def process(update):
        await app.process_update(update)

    async def scenarios():
        # ── 1. Текст с триггером в ЛС ──
        print("\n── 1. Текст ──")
        n0, s0, o0 = len(req.calls), len(seen), len(opened)
        llm["answer"] = "Привет, Аня."
        await process(msg_update(text="Тестер, привет"))
        check("текст: без исключений", not errors)
        check("текст: «печатает…» и ответ в blockquote HTML",
              req.api_calls("sendChatAction")
              and any("Привет, Аня." in (t or "") and "<blockquote expandable>" in t
                      for t in sent_texts(n0))
              and req.api_calls("sendMessage")[-1][1].get("parse_mode") == "HTML")
        check("текст: STM user → ответ, триггер снят",
              stm(42)[-2:] == [("user", "привет"), ("assistant", "Привет, Аня.")])
        check("текст: process_message внутри хода обработчика, ход один, закрыт",
              turn_seen("impl", 42, s0) and len(opened) - o0 == 1
              and seen[-1][2] is opened[-1] and no_leaks())

        # ── 2. Без триггера — не отвечает, хода нет ──
        n0, o0 = len(req.calls), len(opened)
        await process(msg_update(cid=-100, kind="group", text="просто болтаем"))
        check("группа без триггера: молчит, хода не открывали",
              not sent_texts(n0) and len(opened) == o0 and not errors)

        # ── 3. Reply на сообщение бота в группе + топик ──
        n0, s0 = len(req.calls), len(seen)
        llm["answer"] = "Да, помню."
        bot_msg = {"message_id": 7, "date": int(time.time()), "chat": chat(-100, "supergroup"),
                   "from": {"id": BOT_ID, "is_bot": True, "first_name": "Persona"},
                   "text": "Как прошёл день?"}
        await process(msg_update(cid=-100, kind="supergroup", text="хорошо",
                                 reply_to=bot_msg, thread_id=5))
        check("reply боту: ответ ушёл, контекст реплики передан, ход закрыт",
              "Да, помню." in " ".join(t or "" for t in sent_texts(n0))
              and llm["inputs"][-1]["reply_ctx"] == "[tester]: Как прошёл день?"
              and llm["inputs"][-1]["user_name"] == "Аня (42)"
              and turn_seen("impl", -100, s0) and no_leaks() and not errors)

        # ── 4. Split-ответ: части по порядку, фон посреди доставки отложен ──
        print("\n── 4. Split + фоновый коммит посреди доставки ──")
        split_on["v"] = True
        llm["answer"] = "Первая часть.\n\nВторая часть.\n\nТретья часть."
        commits = []

        def during(api, params):
            if api == "sendMessage" and str(params.get("chat_id")) == "42" and not commits:
                commits.append(gate.commit_message(mem, "42", "ИНИЦИАТИВА").status)

        req.on_call = during
        n0 = len(req.calls)
        await process(msg_update(text="тестер, расскажи"))
        req.on_call = None
        texts = sent_texts(n0)
        check("split: три части по порядку",
              [t for t in texts if t] and
              ["Первая" in texts[0], "Вторая" in texts[1], "Третья" in texts[2]] == [True] * 3)
        check("split: фоновый коммит во время доставки — BUSY (ход держится)",
              commits == ["busy"])
        after = gate.commit_message(mem, "42", "ИНИЦИАТИВА")
        check("split: после доставки ход закрыт, коммит проходит; порядок STM",
              after.status == "ok" and no_leaks() and stm(42)[-5:] == [
                  ("user", "расскажи"), ("assistant", "Первая часть."),
                  ("assistant", "Вторая часть."), ("assistant", "Третья часть."),
                  ("assistant", "ИНИЦИАТИВА")] and not errors)
        split_on["v"] = False

        # ── 5. Фото: скачан самый большой размер, vision и ответ внутри хода ──
        print("\n── 5. Фото ──")
        n0, s0, o0 = len(req.calls), len(seen), len(opened)
        seen_chat["v"] = "42"
        llm["answer"] = "Это кот."
        photo = [{"file_id": "ph_small", "file_unique_id": "us", "width": 90, "height": 90,
                  "file_size": 100},
                 {"file_id": "ph_big", "file_unique_id": "ub", "width": 1280, "height": 960,
                  "file_size": len(IMAGE_BYTES)}]
        await process(msg_update(caption="Тестер, что тут?", photo=photo))
        gf = req.api_calls("getFile")
        check("фото: без исключений", not errors)
        check("фото: getFile по самому большому PhotoSize, байты скачаны и отданы vision",
              gf and gf[-1][1]["file_id"] == "ph_big"
              and img_calls and img_calls[-1][1] == IMAGE_BYTES
              and "что тут?" in img_calls[-1][0])
        check("фото: в vision-промпте — язык пользователя (по подписи)",
              img_calls and "The user speaks Russian" in img_calls[-1][0])
        texts = sent_texts(n0)
        check("фото: «Смотрю…» и ответ доставлены по порядку",
              len(texts) == 2 and texts[0] == "Смотрю на изображение..."
              and "Это кот." in texts[1])
        check("фото: vision и process_message — внутри одного хода, он закрыт",
              turn_seen("vision", 42, s0) and turn_seen("impl", 42, s0)
              and len(opened) - o0 == 1 and no_leaks())
        check("фото: raw_user_text — только подпись, STM user(OCR) → ответ",
              llm["inputs"][-1]["raw"] == "что тут?"
              and stm(42)[-2][0] == "user" and "кот на диване" in stm(42)[-2][1]
              and stm(42)[-1] == ("assistant", "Это кот."))

        # ── 5б. ЛС без триггера — отвечает (как обещает /help); группа — нет ──
        print("\n── 5б. ЛС без триггера ──")
        n0, o0 = len(req.calls), len(opened)
        llm["answer"] = "Слушаю."
        await process(msg_update(text="как дела"))
        check("ЛС без триггера: текст — ответ доставлен, STM user как есть",
              any("Слушаю." in (t or "") for t in sent_texts(n0))
              and stm(42)[-2:] == [("user", "как дела"), ("assistant", "Слушаю.")]
              and len(opened) - o0 == 1 and no_leaks() and not errors)
        n0, o0 = len(req.calls), len(opened)
        llm["answer"] = "Снова кот."
        await process(msg_update(photo=photo))
        check("ЛС без подписи: фото — обработано, ответ доставлен",
              any("Снова кот." in (t or "") for t in sent_texts(n0))
              and len(opened) - o0 == 1 and no_leaks() and not errors)
        n0, o0 = len(req.calls), len(opened)
        await process(msg_update(cid=-100, kind="group", photo=photo))
        check("группа без триггера: фото — молчит, хода не открывали",
              not req.calls[n0:] and len(opened) == o0 and not errors)

        # ── 6. Фото-ответ со скриншотами: один кадр и альбом ──
        n0 = len(req.calls)
        llm["photos"] = [{"data": b"shot1", "caption": "Так выглядит страница (x.com)"}]
        llm["answer"] = "Открыл страницу."
        await process(msg_update(text="тестер, открой x.com"))
        sp = req.api_calls("sendPhoto")
        check("скриншот: одним sendPhoto с ответом в подписи (HTML), текста отдельно нет",
              sp and "Открыл страницу." in (sp[-1][1].get("caption") or "")
              and sp[-1][1].get("parse_mode") == "HTML"
              and not sent_texts(n0) and not errors)
        n0 = len(req.calls)
        llm["photos"] = [{"data": b"shot1"}, {"data": b"shot2", "caption": "низ страницы"}]
        await process(msg_update(text="тестер, покажи всю страницу"))
        mg = req.api_calls("sendMediaGroup")
        media = mg[-1][1].get("media") if mg else []
        check("скриншоты: альбом sendMediaGroup (multipart), ответ — подпись первого, второй со своей",
              len(media) == 2 and "Открыл страницу." in (media[0].get("caption") or "")
              and media[0].get("parse_mode") == "HTML"
              and media[1].get("caption") == "низ страницы"
              and all(str(m.get("media", "")).startswith("attach://") for m in media)
              and len(mg[-1][1].get("_multipart") or []) == 2
              and not errors and no_leaks())
        llm["photos"] = None

        # ── 7. Документ ──
        print("\n── 7. Документ ──")
        n0, s0, o0 = len(req.calls), len(seen), len(opened)
        llm["answer"] = "Прочитал список."
        doc = {"file_id": "doc1", "file_unique_id": "ud1", "file_name": "notes.txt",
               "mime_type": "text/plain", "file_size": len(DOC_BYTES)}
        await process(msg_update(caption="Тестер, прочитай", document=doc))
        texts = sent_texts(n0)
        check("документ: без исключений, «Читаю файл…» → ответ",
              not errors and len(texts) == 2 and texts[0] == "Читаю файл..."
              and "Прочитал список." in texts[1])
        check("документ: содержимое скачано и в file_db, raw_user_text — подпись",
              bot.file_db.called("add_file")
              and bot.file_db.called("add_file")[-1][1][2] == DOC_BYTES.decode()
              and llm["inputs"][-1]["raw"] == "прочитай")
        check("документ: process_message внутри хода, ход один и закрыт",
              turn_seen("impl", 42, s0) and len(opened) - o0 == 1 and no_leaks())

        # ── 8. Команды через _run_command ──
        print("\n── 8. Команды ──")
        split_on["v"] = True
        cases = [
            ("/remind через 10 минут полить цветы", "reminder", "add_reminder",
             "/remind через 10 минут полить цветы"),
            ("/add_todo купить хлеб", "todo", "add_item", "/todo купить хлеб"),
            ("/add_inventory кофе: горячий", "inventory", "add_item", "/inventory кофе: горячий"),
            ("/learn испанский", "learning", "begin_setup", "/learn испанский"),
        ]
        mgr = {"reminder": bot.reminder_manager, "todo": bot.todo_manager,
               "inventory": bot.inventory_manager, "learning": bot.learning_manager}
        seen_chat["v"] = "42"
        for text, kind, method, stm_cmd in cases:
            n0, s0, o0 = len(req.calls), len(seen), len(opened)
            llm["answer"] = f"Готово ({kind}).\n\nЕщё строка."
            await process(msg_update(text=text))
            texts = sent_texts(n0)
            check(f"{text.split()[0]}: без исключений (NameError context), менеджер вызван, "
                  f"ответ + split-хвост доставлены",
                  not errors and mgr[kind].called(method)
                  and len(texts) == 2 and f"Готово ({kind})." in texts[0]
                  and "Ещё строка." in texts[1])
            check(f"{text.split()[0]}: STM команда → части ответа; LLM внутри хода; ход закрыт",
                  stm(42)[-3:] == [("user", stm_cmd), ("assistant", f"Готово ({kind})."),
                                   ("assistant", "Ещё строка.")]
                  and turn_seen("cmd_llm", 42, s0) and len(opened) - o0 == 1 and no_leaks())
        reg = bot.learning_manager.called("register_question_message")
        check("/learn: вопрос о частоте зарегистрирован по message_id обеих частей",
              len(reg) == 2 and all(r[1][0] == "42" and isinstance(r[1][1], int)
                                    for r in reg)
              and reg[0][1][1] != reg[1][1][1])
        split_on["v"] = False

        # Команда с @username бота в группе и без аргументов
        n0 = len(req.calls)
        seen_chat["v"] = "-100"
        llm["answer"] = "Записал."
        await process(msg_update(cid=-100, kind="group",
                                 text=f"/add_todo@{BOT_USERNAME} позвонить"))
        check("/add_todo@bot в группе: обработана, ответ ушёл",
              not errors and "Записал." in " ".join(t or "" for t in sent_texts(n0))
              and no_leaks())
        n0, o0 = len(req.calls), len(opened)
        await process(msg_update(text="/remind"))
        check("/remind без аргументов: подсказка, хода нет",
              sent_texts(n0) == ["Использование: /remind <что напомнить> [через N ...]"]
              and len(opened) == o0 and not errors)

        # ── 9. Правка сообщения и пост канала — не роняют обработчики ──
        print("\n── 9. Правки/каналы/блокировки/сбои ──")
        n0, o0 = len(req.calls), len(opened)
        await process(msg_update(text="Тестер, привет ещё раз", field="edited_message"))
        await process(msg_update(text="/remind через 5 минут чай", field="edited_message"))
        await process(msg_update(caption="Тестер, что тут?", photo=photo,
                                 field="edited_message"))
        await process(msg_update(cid=-200, text="Тестер, канал", field="channel_post"))
        check("правка сообщения/пост канала: без исключений, повторного ответа нет",
              not errors and not sent_texts(n0) and len(opened) == o0)
        errors.clear()

        # Заблокированный пользователь: ход открыт и закрыт, ответа нет
        n0 = len(req.calls)
        gf0 = len(req.api_calls("getFile"))
        await process(msg_update(cid=666, uid=666, text="Тестер, ответь"))
        await process(msg_update(cid=666, uid=666, caption="Тестер, глянь", photo=photo))
        check("заблокированный: ни ответа, ни скачивания, ход закрыт",
              not sent_texts(n0) and len(req.api_calls("getFile")) == gf0
              and no_leaks() and not errors)

        # Ошибка генерации → «Произошла ошибка», ход закрыт
        n0 = len(req.calls)
        llm["raise"] = RuntimeError("LLM down")
        await process(msg_update(text="Тестер, ну?"))
        llm["raise"] = None
        check("сбой генерации: пользователю сообщение об ошибке, ход закрыт",
              sent_texts(n0) == ["Произошла ошибка. Попробуйте позже."]
              and no_leaks() and not errors)

        # HTML не принят Telegram → фолбэк простым текстом
        n0 = len(req.calls)
        llm["answer"] = "Ответ <без> HTML."
        req.fail["sendMessage"] = 1
        await process(msg_update(text="Тестер, ещё"))
        texts = sent_texts(n0)
        check("HTML отвергнут: повтор простым текстом",
              len(texts) == 2 and texts[1] == "Ответ <без> HTML." and not errors)

        # getFile упал — ход закрыт, пользователь не остаётся без ответа
        n0 = len(req.calls)
        req.fail["getFile"] = 1
        await process(msg_update(caption="Тестер, что тут?", photo=photo))
        check("фото: сбой скачивания — ход закрыт, пользователю сказано",
              no_leaks() and not errors
              and any("изображени" in (t or "") for t in sent_texts(n0)))
        n0 = len(req.calls)
        req.fail["getFile"] = 1
        await process(msg_update(caption="Тестер, прочитай", document=doc))
        check("документ: сбой скачивания — ход закрыт, пользователю сказано",
              no_leaks() and not errors
              and any("файл" in (t or "") for t in sent_texts(n0)))

        # ── 10. Два сообщения одного чата конкурентно: порядок STM ──
        print("\n── 10. Конкурентность ──")
        real_impl = bot._process_message_impl
        answers = iter(["ответ-1", "ответ-2"])
        first = {"v": True}

        def impl_answers(user_input, **kw):
            llm["answer"] = next(answers)
            if first["v"]:          # первая генерация долгая — вторая ждёт лок чата
                first["v"] = False
                time.sleep(0.3)
            return real_impl(user_input, **kw)

        bot._process_message_impl = impl_answers
        e0 = len(errors)
        await asyncio.gather(
            process(msg_update(cid=78, uid=78, text="тестер, раз")),
            _later(0.05, process(msg_update(cid=78, uid=78, text="тестер, два"))))
        check("два сообщения подряд: STM раз → ответ-1 → два → ответ-2, ходы закрыты",
              stm(78) == [("user", "раз"), ("assistant", "ответ-1"),
                          ("user", "два"), ("assistant", "ответ-2")]
              and no_leaks() and not errors[e0:])
        bot._process_message_impl = real_impl

    async def _later(delay, coro):
        await asyncio.sleep(delay)
        return await coro

    asyncio.run(run())

    if errors:
        print("\nИсключения обработчиков:")
        for e in errors:
            print(f"  {type(e).__name__}: {e}")
    print(f"\nИтог: {ok} проверок, FAIL: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
