"""
Telegram-аккаунт пользователя (Telethon, MTProto): входящие сообщения в
личных чатах, упоминания и ответы в группах, по желанию — все сообщения
групп и посты каналов.

Это не бот персоны, а вход в аккаунт пользователя как ещё одно устройство
(«Virtual Persona Core» в списке активных сеансов Telegram — там же его
можно завершить). Нужны api_id и api_hash с my.telegram.org и вход по
коду, который Telegram пришлёт в приложение (и облачный пароль, если он
включён). Сессия хранится в secrets.json; сообщений аккаунт не
отправляет и прочитанными их не отмечает.

Что не становится событием: свои сообщения, боты (в том числе боты
персон — их ответы пользователь видит сам), служебный чат Telegram 777000
(коды входа), «Избранное».

Пачка подряд идущих сообщений одного чата ждёт DEBOUNCE секунд тишины и
уходит одним событием. Если за это время пользователь прочитал чат в
Telegram (на любом устройстве) или ответил в нём — событие не создаётся:
он уже в курсе. Прочитанное позже, пока уведомление ждёт тихих часов, —
gone() снимает его из очереди.

Пропущенное, пока ядро было выключено, Telethon не догоняет (catch_up
выключен): уведомлять о вчерашних сообщениях поздно.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from app.integrations import store
from app.integrations.feed import Event

logger = logging.getLogger(__name__)

DEBOUNCE = 40.0
MAX_HOLD = 180.0
MAX_MSGS = 20
LOGIN_TTL = 600
SERVICE_USER_ID = 777000
DEVICE = "Virtual Persona Core"
_MUTE_TTL = 600.0

_PLACEHOLDERS = {
    "ru": {"photo": "[фото]", "video": "[видео]", "voice": "[голосовое]", "audio": "[аудио]",
           "sticker": "[стикер]", "gif": "[GIF]", "file": "[файл]", "poll": "[опрос]",
           "geo": "[место]", "contact": "[контакт]", "other": "[вложение]"},
    "en": {"photo": "[photo]", "video": "[video]", "voice": "[voice message]", "audio": "[audio]",
           "sticker": "[sticker]", "gif": "[GIF]", "file": "[file]", "poll": "[poll]",
           "geo": "[location]", "contact": "[contact]", "other": "[attachment]"},
}


def available() -> bool:
    try:
        import telethon  # noqa: F401
        return True
    except ImportError:
        return False


def configured() -> bool:
    sec = store.load_secrets("telegram")
    return bool(sec.get("session") and sec.get("api_id") and sec.get("api_hash"))


def _client(session: str, api_id: int, api_hash: str):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session or ""), int(api_id), str(api_hash),
                          device_model=DEVICE, system_version="1.0", app_version="1.0",
                          lang_code="ru", system_lang_code="ru", receive_updates=True)


def display_name(entity) -> str:
    if entity is None:
        return ""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(p for p in parts if p)
    return name or (f"@{entity.username}" if getattr(entity, "username", None) else "")


def media_placeholder(message, lang: str = "ru") -> str:
    p = _PLACEHOLDERS.get(lang, _PLACEHOLDERS["ru"])
    if getattr(message, "photo", None):
        return p["photo"]
    if getattr(message, "voice", None) or getattr(message, "video_note", None):
        return p["voice"]
    if getattr(message, "gif", None):
        return p["gif"]
    if getattr(message, "video", None):
        return p["video"]
    if getattr(message, "audio", None):
        return p["audio"]
    if getattr(message, "sticker", None):
        return p["sticker"]
    if getattr(message, "poll", None):
        return p["poll"]
    if getattr(message, "geo", None):
        return p["geo"]
    if getattr(message, "contact", None):
        return p["contact"]
    doc = getattr(message, "document", None)
    if doc is not None:
        name = getattr(getattr(message, "file", None), "name", None)
        return f"{p['file'][:-1]}: {name}]" if name else p["file"]
    if getattr(message, "media", None):
        return p["other"]
    return ""


# ── Вход ───────────────────────────────────────────────────────────────

class Login:
    """Шаги входа на одном клиенте (ключ авторизации живёт в нём):
    start → code → (password). Корутины — на loop хаба."""

    def __init__(self):
        self.client = None
        self.phone = ""
        self.code_hash = ""
        self.api_id = 0
        self.api_hash = ""
        self.expires = 0.0

    async def _drop(self):
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def start(self, api_id: int, api_hash: str, phone: str) -> dict:
        await self._drop()
        self.api_id, self.api_hash, self.phone = int(api_id), str(api_hash), str(phone)
        self.client = _client("", self.api_id, self.api_hash)
        await self.client.connect()
        try:
            sent = await self.client.send_code_request(self.phone)
        except Exception as e:
            await self._drop()
            return {"ok": False, "error": _err(e)}
        self.code_hash = sent.phone_code_hash
        self.expires = time.time() + LOGIN_TTL
        return {"ok": True, "next": "code"}

    async def code(self, code: str) -> dict:
        from telethon.errors import SessionPasswordNeededError
        if self.client is None or time.time() > self.expires:
            return {"ok": False, "error": "вход устарел — начните заново"}
        try:
            await self.client.sign_in(phone=self.phone, code=str(code).strip(),
                                      phone_code_hash=self.code_hash)
        except SessionPasswordNeededError:
            return {"ok": True, "next": "password"}
        except Exception as e:
            return {"ok": False, "error": _err(e)}
        return await self._done()

    async def password(self, password: str) -> dict:
        if self.client is None or time.time() > self.expires:
            return {"ok": False, "error": "вход устарел — начните заново"}
        try:
            await self.client.sign_in(password=str(password))
        except Exception as e:
            return {"ok": False, "error": _err(e)}
        return await self._done()

    async def _done(self) -> dict:
        me = await self.client.get_me()
        session = self.client.session.save()
        user = {"id": me.id, "name": display_name(me), "username": me.username or ""}
        store.update_secrets("telegram", {"api_id": self.api_id, "api_hash": self.api_hash,
                                          "session": session, "phone": self.phone,
                                          "user": user, "connected_at": time.time()},
                             replace=True)
        store.update_state("telegram", {"auth_error": ""})
        await self._drop()
        return {"ok": True, "user": user}


def _err(e: Exception) -> str:
    name = type(e).__name__
    known = {
        "PhoneNumberInvalidError": "неверный номер телефона",
        "PhoneCodeInvalidError": "неверный код",
        "PhoneCodeExpiredError": "код устарел — запросите новый",
        "PasswordHashInvalidError": "неверный облачный пароль",
        "ApiIdInvalidError": "неверные api_id / api_hash",
        "FloodWaitError": f"Telegram просит подождать {getattr(e, 'seconds', '?')} с",
        "PhoneNumberBannedError": "номер заблокирован в Telegram",
    }
    return known.get(name, f"{name}: {e}")


# ── Наблюдатель ────────────────────────────────────────────────────────

class TelegramWatcher:
    """Подключение к аккаунту и сбор событий. Все методы — на loop хаба."""

    def __init__(self, ingest: Callable[[List[Event]], None],
                 settings_getter: Callable[[], dict] = store.load_settings,
                 lang_getter: Callable[[], str] = lambda: "ru"):
        self._ingest = ingest
        self._settings = settings_getter
        self._lang = lang_getter
        self.client = None
        self.me_id: Optional[int] = None
        self._fingerprint = None
        self._buffers: Dict[int, dict] = {}
        self._read_upto: Dict[int, int] = {}
        self._mute_cache: Dict[int, tuple] = {}
        self.status: dict = {"running": False, "error": ""}

    # ── жизненный цикл ──

    async def reconcile(self) -> None:
        """Подключиться/отключиться по настройкам и секретам."""
        settings = self._settings()
        want = settings["services"]["telegram"]["enabled"] and configured() and available()
        sec = store.load_secrets("telegram") if want else {}
        fp = (sec.get("session"), sec.get("api_id"), sec.get("api_hash")) if want else None
        if fp == self._fingerprint and (self.client is not None or not want):
            return
        await self.stop()
        self._fingerprint = fp
        if want:
            await self._start(sec)

    async def _start(self, sec: dict) -> None:
        from telethon import events
        try:
            client = _client(sec["session"], sec["api_id"], sec["api_hash"])
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                self.status = {"running": False, "error": "сеанс завершён — войдите заново",
                               "auth": True}
                store.update_state("telegram", {"auth_error": "session revoked"})
                return
            me = await client.get_me()
        except Exception as e:
            self.status = {"running": False, "error": _err(e)}
            logger.warning(f"[Telegram] подключение не удалось: {_err(e)}")
            return
        self.client = client
        self.me_id = me.id
        client.add_event_handler(self._on_new, events.NewMessage())
        client.add_event_handler(self._on_read, events.MessageRead(inbox=True))
        self.status = {"running": True, "error": "", "account": display_name(me)}
        logger.info(f"[Telegram] аккаунт подключён: {display_name(me)}")

    async def stop(self) -> None:
        client, self.client = self.client, None
        self._buffers.clear()
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        self.status = {"running": False, "error": self.status.get("error", "")}

    # ── события ──

    async def _muted(self, event) -> bool:
        chat_id = event.chat_id
        hit = self._mute_cache.get(chat_id)
        now = time.time()
        if hit and now - hit[1] < _MUTE_TTL:
            return hit[0]
        muted = False
        try:
            from telethon.tl.functions.account import GetNotifySettingsRequest
            from telethon.tl.types import InputNotifyPeer
            res = await self.client(GetNotifySettingsRequest(
                peer=InputNotifyPeer(await event.get_input_chat())))
            until = getattr(res, "mute_until", None)
            if isinstance(until, datetime):
                muted = until.replace(tzinfo=until.tzinfo or timezone.utc).timestamp() > now
            elif isinstance(until, (int, float)):
                muted = until > now
        except Exception as e:
            logger.debug(f"[Telegram] настройки звука чата не получены: {e}")
        self._mute_cache[chat_id] = (muted, now)
        return muted

    async def _on_new(self, event) -> None:
        try:
            await self._handle(event)
        except Exception as e:
            logger.warning(f"[Telegram] сообщение не разобрано: {e}")

    async def _handle(self, event) -> None:
        msg = event.message
        chat_id = event.chat_id
        if msg.out:
            # Ответил сам — о чате он в курсе
            self._read_upto[chat_id] = max(self._read_upto.get(chat_id, 0), msg.id)
            self._buffers.pop(chat_id, None)
            return
        if chat_id in (self.me_id, SERVICE_USER_ID):
            return
        opts = self._settings()["services"]["telegram"]
        sender = await event.get_sender()
        if getattr(sender, "bot", False) or getattr(sender, "id", None) in (self.me_id, SERVICE_USER_ID):
            return
        if event.is_private:
            if not opts.get("private", True):
                return
            kind = "message"
        elif event.is_group:
            mentioned = bool(msg.mentioned)
            if mentioned and opts.get("mentions", True):
                kind = "mention"
            elif opts.get("groups", False):
                kind = "group"
            else:
                return
        elif event.is_channel:
            if not opts.get("channels", False):
                return
            kind = "channel"
        else:
            return
        chat = await event.get_chat()
        text = (msg.message or "").strip()
        placeholder = media_placeholder(msg, self._lang())
        if placeholder:
            text = f"{placeholder} {text}".strip()
        if not text:
            return
        name = display_name(sender) or display_name(chat)
        buf = self._buffers.get(chat_id)
        now = time.time()
        if buf is None:
            buf = self._buffers[chat_id] = {
                "first": now, "msgs": [], "first_id": msg.id, "kind": kind,
                "chat": display_name(chat) if not event.is_private else "",
                "username": getattr(chat, "username", None) or "",
                "contact": bool(getattr(sender, "contact", False)) if event.is_private else False,
                "muted": await self._muted(event), "mentioned": False,
            }
        if kind == "mention":
            buf["kind"] = "mention"
            buf["mentioned"] = True
        buf["msgs"].append((name, text))
        buf["last"] = now
        buf["max_id"] = msg.id
        buf["sender"] = name
        buf["ts"] = msg.date.timestamp() if msg.date else now
        buf["chat_id"] = chat_id
        if len(buf["msgs"]) >= MAX_MSGS:
            self._flush(chat_id)

    async def _on_read(self, event) -> None:
        chat_id = event.chat_id
        if chat_id is None:
            return
        self._read_upto[chat_id] = max(self._read_upto.get(chat_id, 0), int(event.max_id or 0))
        buf = self._buffers.get(chat_id)
        if buf and self._read_upto[chat_id] >= buf.get("max_id", 0):
            self._buffers.pop(chat_id, None)

    def tick(self, now: Optional[float] = None) -> None:
        """Отпустить пачки, по которым DEBOUNCE секунд тишины."""
        now = now if now is not None else time.time()
        for chat_id, buf in list(self._buffers.items()):
            if now - buf.get("last", now) >= DEBOUNCE or now - buf["first"] >= MAX_HOLD:
                self._flush(chat_id)

    def _flush(self, chat_id: int) -> None:
        buf = self._buffers.pop(chat_id, None)
        if not buf or not buf.get("msgs"):
            return
        skip_read = self._settings()["services"]["telegram"].get("skip_read", True)
        if skip_read and self._read_upto.get(chat_id, 0) >= buf["max_id"]:
            return
        private = buf["kind"] == "message"
        senders = {n for n, _ in buf["msgs"]}
        if private or len(senders) == 1 and buf["kind"] != "channel":
            text = "\n".join(t for _, t in buf["msgs"])
        else:
            text = "\n".join(f"{n}: {t}" for n, t in buf["msgs"])
        if buf["kind"] == "channel":
            sender, group = buf["chat"], buf["chat"]
        else:
            sender, group = buf["sender"], buf["chat"]
        username = buf.get("username")
        url = ""
        if username:
            url = f"https://t.me/{username}" if private else f"https://t.me/{username}/{buf['max_id']}"
        first_line = text.split("\n", 1)[0]
        ev = Event(source="telegram", key=f"{chat_id}:{buf['first_id']}-{buf['max_id']}",
                   kind=buf["kind"], title=first_line[:120], sender=sender, text=text, url=url,
                   ts=buf.get("ts") or time.time(), group=group,
                   meta={"chat_id": chat_id, "max_id": buf["max_id"], "muted": buf["muted"],
                         "mentioned": buf["mentioned"], "contact": buf["contact"],
                         "username": username, "count": len(buf["msgs"]),
                         "chat": buf["chat"]})
        self._ingest([ev])

    def gone(self, items: List[dict]) -> set:
        out = set()
        for it in items:
            meta = it.get("meta") or {}
            if it.get("source") != "telegram" or meta.get("chat_id") is None:
                continue
            if self._read_upto.get(meta["chat_id"], 0) >= int(meta.get("max_id") or 0):
                out.add(it["id"])
        return out

    async def unread(self, limit: int = 15) -> List[dict]:
        """Для чата: непрочитанные диалоги (кроме ботов и служебных)."""
        if self.client is None:
            return []
        out = []
        now = time.time()
        async for d in self.client.iter_dialogs(limit=80, archived=False):
            if d.unread_count <= 0:
                continue
            ent = d.entity
            if getattr(ent, "bot", False) or getattr(ent, "id", None) in (self.me_id, SERVICE_USER_ID):
                continue
            ns = getattr(d.dialog, "notify_settings", None)
            until = getattr(ns, "mute_until", None)
            muted = bool(until and (until.timestamp() if isinstance(until, datetime) else until) > now)
            last = d.message
            text = (getattr(last, "message", "") or media_placeholder(last, self._lang()) or "") if last else ""
            out.append({"name": d.name, "unread": d.unread_count, "muted": muted,
                        "mentions": getattr(d, "unread_mentions_count", 0) or 0,
                        "kind": "private" if d.is_user else ("channel" if d.is_channel and not d.is_group else "group"),
                        "last": text[:300]})
            if len(out) >= limit:
                break
        # Личные и с упоминаниями — выше, «без звука» — ниже
        out.sort(key=lambda x: (x["muted"], 0 if x["kind"] == "private" or x["mentions"] else 1))
        return out
