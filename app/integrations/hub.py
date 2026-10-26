"""
Хаб сервисов: опросы источников, Telegram-аккаунт, разбор и уведомления
в одном фоновом потоке со своим event loop.

Один активный хаб на папку данных — межпроцессный замок
data/integrations/hub.lock. Процесс, который замок не взял (второе ядро,
стенд на тех же данных), работает «в ожидании»: настройки, вход в Google
и Telegram, лента — доступны, а опросов и уведомлений нет. Так нет двойных
уведомлений, и сессия Telegram не подключается дважды (Telegram за это
сбрасывает ключ авторизации).

Доставка — в чат выбранной персоны: запись в историю (как напоминание:
под гейтом хода, во время ответа пользователю не вклинивается), inbox
веба и телефона, и по желанию — сообщение от бота персоны в Telegram
владельцу (Bot API напрямую, процесс Telegram-ботов для этого не нужен).

INTEGRATIONS_HUB=0 — хаб не запускается (тесты).
"""

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import Future
from typing import Dict, List, Optional

from app.integrations import notifier as notifier_mod
from app.integrations import store, triage
from app.integrations.feed import Event, feed
from app.integrations.sources import AuthError, PollSource, http
from app.integrations.sources.github import GitHubSource
from app.integrations.sources.google import DriveSource, GmailSource
from app.integrations.sources.imap_mail import ImapSource
from app.integrations.sources.rss import RssSource
from app.integrations.sources import telegram_user

logger = logging.getLogger(__name__)

TICK_SEC = 10.0
RECONCILE_SEC = 15.0
TRIAGE_DELAY = 3.0
MAX_BACKOFF = 3600.0
WEB_CHAT_ID = "web_user"

_AUTH_TEXT = {
    "ru": "Не могу проверить {name}: доступ истёк или отозван. Подключите заново в разделе «Сервисы».",
    "en": "I can't check {name}: access expired or was revoked. Reconnect it in Services.",
}
_SERVICE_NAMES = {
    "ru": {"google": "почту и Диск Google", "github": "GitHub", "telegram": "Telegram",
           "imap": "почту", "rss": "ленты"},
    "en": {"google": "Google mail and Drive", "github": "GitHub", "telegram": "Telegram",
           "imap": "mail", "rss": "feeds"},
}


def _account_key(service: str) -> str:
    return "google" if service in ("gmail", "drive") else service


class ProcessLock:
    def __init__(self, path):
        self.path = path
        self._fd = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


class Hub:
    def __init__(self):
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock: Optional[ProcessLock] = None
        self.active = False
        self.started = False
        self._stop = threading.Event()
        self.polls: Dict[str, PollSource] = {
            "gmail": GmailSource(), "drive": DriveSource(), "github": GitHubSource(),
            "imap": ImapSource(), "rss": RssSource(),
        }
        self._next_poll: Dict[str, float] = {}
        self._fails: Dict[str, int] = {}
        self._busy: set = set()
        self._triage_event: Optional[asyncio.Event] = None
        self.telegram = telegram_user.TelegramWatcher(self.ingest, lang_getter=self.lang)
        self.tg_login = telegram_user.Login()
        self.notifier = notifier_mod.Notifier(self.deliver, relevant=self._gone,
                                              lang=self.lang, restyle=self._restyle)

    # ── запуск ──

    def start(self) -> None:
        if self.started or os.getenv("INTEGRATIONS_HUB", "1").strip() in ("0", "false", "off"):
            return
        self.started = True
        self._lock = ProcessLock(store.root() / "hub.lock")
        self.active = self._lock.acquire()
        if not self.active:
            logger.info("[Integrations] хаб уже работает в другом процессе ядра — "
                        "здесь только настройки и вход")
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="integrations")
        self._thread.start()

    def stop(self) -> None:
        if not self.started:
            return
        self._stop.set()
        if self.loop is not None:
            try:
                self.run(self.telegram.stop(), timeout=5)
            except Exception:
                pass
        if self._lock is not None:
            self._lock.release()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._triage_event = asyncio.Event()
        if self.active:
            self.loop.create_task(self._main())
            self.loop.create_task(self._triage_worker())
        self.loop.run_forever()

    def run(self, coro, timeout: float = 60.0):
        """Корутина на loop хаба из другого потока (API)."""
        if self.loop is None:
            raise RuntimeError("хаб сервисов не запущен")
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    async def _main(self) -> None:
        # Хвосты прошлого запуска: неразобранное — в разбор
        if feed.with_status("new") and self._triage_event is not None:
            self._triage_event.set()
        last_reconcile = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                if now - last_reconcile >= RECONCILE_SEC:
                    last_reconcile = now
                    await self.telegram.reconcile()
                    self._telegram_status()
                    if self.tg_login.client is not None and now > self.tg_login.expires:
                        await self.tg_login._drop()
                    # События, записанные другим процессом (вебхук в ядре
                    # «в ожидании»), — тоже в разбор
                    if feed.with_status("new"):
                        self._triage_event.set()
                settings = store.load_settings()
                for name, src in self.polls.items():
                    if name in self._busy:
                        continue
                    if not settings["services"][name]["enabled"] or not src.configured():
                        self._next_poll.pop(name, None)
                        continue
                    if now >= self._next_poll.get(name, 0):
                        self._busy.add(name)
                        asyncio.create_task(self._poll(name, src, settings))
                self.telegram.tick(now)
                await asyncio.to_thread(self.notifier.tick, now)
            except Exception as e:
                logger.warning(f"[Integrations] тик хаба: {e}")
            await asyncio.sleep(TICK_SEC)

    # ── опросы ──

    async def _poll(self, name: str, src: PollSource, settings: dict) -> None:
        interval = float(settings["services"][name].get("interval") or 300)
        try:
            events = await asyncio.to_thread(src.poll)
            self._fails[name] = 0
            hint = src.interval_hint()
            if hint:
                interval = max(interval, hint)
            store.update_state(name, {"last_ok": time.time(), "last_error": "",
                                      "auth_error": ""})
            store.update_state(_account_key(name), {"auth_error": "", "auth_notified": False})
            if events:
                self.ingest(events)
        except AuthError as e:
            self._fails[name] = self._fails.get(name, 0) + 1
            interval = MAX_BACKOFF
            store.update_state(name, {"last_error": str(e), "auth_error": str(e)})
            await asyncio.to_thread(self._auth_lost, _account_key(name), str(e))
        except Exception as e:
            n = self._fails[name] = self._fails.get(name, 0) + 1
            interval = min(MAX_BACKOFF, interval * (2 ** min(n, 6)))
            store.update_state(name, {"last_error": str(e)[:300], "last_fail": time.time()})
            logger.info(f"[Integrations] {name}: {e} (следующая попытка через {int(interval)} с)")
        finally:
            self._next_poll[name] = time.time() + interval
            self._busy.discard(name)

    def poll_now(self, name: str) -> None:
        self._next_poll[name] = 0

    def _telegram_status(self) -> None:
        st = self.telegram.status
        if st.get("auth"):
            store.update_state("telegram", {"last_error": st.get("error", ""),
                                            "auth_error": st.get("error", "")})
            self._auth_lost("telegram", st.get("error", ""))
        else:
            store.update_state("telegram", {"running": bool(st.get("running")),
                                            "last_error": st.get("error", ""),
                                            "last_ok": time.time() if st.get("running") else None})

    def _auth_lost(self, account: str, error: str) -> None:
        """Один раз сказать пользователю, что доступ к сервису пропал."""
        st = store.load_state(account)
        if st.get("auth_notified"):
            return
        store.update_state(account, {"auth_error": error})
        lang = notifier_mod.tr(self.lang())
        name = _SERVICE_NAMES[lang].get(account, account)
        try:
            if self.deliver(_AUTH_TEXT[lang].format(name=name), [], "system"):
                store.update_state(account, {"auth_notified": True})
        except Exception as e:
            logger.info(f"[Integrations] не сообщил о потере доступа: {e}")

    # ── события и разбор ──

    def ingest(self, events: List[Event]) -> List[dict]:
        added = feed.add(events)
        if added:
            logger.info(f"[Integrations] новых событий: {len(added)} "
                        f"({', '.join(sorted({a['source'] for a in added}))})")
            self._wake_triage()
        return added

    def _wake_triage(self) -> None:
        if self.loop is not None and self._triage_event is not None:
            self.loop.call_soon_threadsafe(self._triage_event.set)

    async def _triage_worker(self) -> None:
        while not self._stop.is_set():
            await self._triage_event.wait()
            # Пауза — события одной волны разбираются одним вызовом модели
            await asyncio.sleep(TRIAGE_DELAY)
            self._triage_event.clear()
            try:
                await asyncio.to_thread(self.triage_pending)
            except Exception as e:
                logger.warning(f"[Integrations] разбор не удался: {e}")

    def triage_pending(self, llm=None) -> dict:
        items = feed.with_status("new")
        if not items:
            return {}
        settings = store.load_settings()
        decisions = triage.decide(items, settings, lang=self.lang(), llm=llm)
        self.notifier.accept(decisions, items)
        levels = {}
        for d in decisions.values():
            levels[d["level"]] = levels.get(d["level"], 0) + 1
        logger.info(f"[Integrations] разобрано {len(decisions)}: {levels}")
        return decisions

    def _gone(self, items: List[dict]) -> set:
        out = set()
        if store.load_settings()["services"]["telegram"].get("skip_read", True):
            out |= self.telegram.gone(items)
        by_src: Dict[str, List[dict]] = {}
        for it in items:
            by_src.setdefault(it.get("source"), []).append(it)
        for name in ("gmail", "github"):
            if by_src.get(name):
                out |= self.polls[name].gone(by_src[name])
        return out

    # ── персона и доставка ──

    _default_persona: tuple = (0.0, None)

    def persona(self) -> Optional[str]:
        """Кто сообщает: выбранная персона, иначе та, с которой
        пользователь общался последней (запоминается на 10 минут)."""
        from app.api import runtime
        pid = store.load_settings()["notify"].get("persona")
        names = runtime.list_personas()
        if pid and pid in names:
            return pid
        ts, cached = self._default_persona
        if cached in names and time.time() - ts < 600:
            return cached
        try:
            from app.api import warmup
            recent = warmup.recent_personas(24 * 365)
        except Exception:
            recent = []
        pick = recent[0] if recent else (names[0] if names else None)
        self._default_persona = (time.time(), pick)
        return pick

    def lang(self) -> str:
        cfg = store.load_settings()["notify"].get("language", "auto")
        if cfg in ("ru", "en"):
            return cfg
        try:
            from app.api import runtime
            pid = self.persona()
            bot = runtime.registry._bots.get(pid) if pid else None
            if bot is not None:
                code = bot.chat_user_language(WEB_CHAT_ID)
                if code in ("ru", "en"):
                    return code
                from app.core.language import persona_language
                code = persona_language(getattr(bot.persona, "system_prompt", ""))
                if code in ("ru", "en"):
                    return code
        except Exception:
            pass
        return "ru"

    def deliver(self, text: str, items: List[dict], kind: str) -> Optional[bool]:
        """Сообщение в каналы. True — хоть один канал принял; None — идёт
        ход пользователя в чате (повтор на следующем тике)."""
        settings = store.load_settings()
        channels = settings["notify"]["channels"]
        persona = self.persona()
        if not persona:
            logger.warning("[Integrations] нет ни одной персоны — уведомлять некому")
            return False
        ok = False
        if channels.get("web", True):
            res = self._deliver_web(persona, text)
            if res is None:
                return None
            ok = ok or res
        if channels.get("telegram"):
            ok = self._deliver_telegram(persona, text) or ok
        if ok:
            logger.info(f"[Integrations] уведомление ({kind}, {len(items)} соб.) "
                        f"от {persona}: {len(text)} симв.")
            # Последнее сообщённое — для вопросов и оценок в чате
            # («что он написал?», «это не важно»), в том числе из процесса
            # Telegram-ботов
            if items:
                store.update_state("delivery", {"persona": persona, "ts": time.time(),
                                                "kind": kind,
                                                "ids": [it["id"] for it in items]},
                                   replace=True)
        return ok

    @staticmethod
    def _deliver_web(persona: str, text: str) -> Optional[bool]:
        try:
            from app.api import runtime
            from app.api.inbox import inbox_push
        except Exception:
            return False
        try:
            bot = runtime.registry.get(persona)
        except Exception as e:
            # Нет ни одного источника ответов (NoProvidersError) и т.п.:
            # чат персоны не поднять — уведомление остаётся в ленте
            logger.warning(f"[Integrations] персона {persona} недоступна: {e}")
            return False
        if bot is None:
            return False
        if bot.is_muted():
            logger.info(f"[Integrations] персона {persona} заморожена — уведомление только в ленте")
            return False
        gate = bot._get_turn_gate()
        res = gate.commit_message(bot.memory, WEB_CHAT_ID, text)
        if res.status == "busy":
            return None
        if res.status not in ("ok",):
            return False
        inbox_push(persona, WEB_CHAT_ID, text, kind="notify")
        return True

    @staticmethod
    def telegram_target(persona: str) -> tuple:
        """(токен бота персоны, chat_id владельца) или пустые строки."""
        token = os.getenv(f"{persona.upper()}_BOT_TOKEN", "").strip()
        owner = ""
        try:
            from app.api import runtime
            info = runtime.get_persona_info(persona) or {}
            owner = str((info.get("features") or {}).get("owner") or "")
        except Exception:
            pass
        owner = owner or os.getenv("OWNER_USER_ID", "").strip()
        return token, owner

    def _deliver_telegram(self, persona: str, text: str) -> bool:
        token, owner = self.telegram_target(persona)
        if not token or not owner:
            return False
        try:
            proxy = os.getenv("TELEGRAM_PROXY", "").strip() or None
            with http(20.0, proxy=proxy) as c:
                resp = c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": owner, "text": text[:4000],
                                    "disable_web_page_preview": True})
            if resp.status_code >= 400:
                logger.warning(f"[Integrations] Telegram не принял уведомление: "
                               f"HTTP {resp.status_code}")
                return False
            return True
        except Exception as e:
            logger.warning(f"[Integrations] Telegram недоступен: {type(e).__name__}")
            return False

    def _restyle(self, text: str, items: List[dict], kind: str) -> Optional[str]:
        """Пересказ персоной (style persona): факты — из готового текста."""
        settings = store.load_settings()
        if settings["notify"].get("style") != "persona" or kind == "system":
            return None
        from app.api import runtime
        from app.core.language import user_language_line
        from app.core.persona import persona_prompt
        persona = self.persona()
        bot = runtime.registry.get(persona) if persona else None
        if bot is None:
            return None
        system = (
            persona_prompt(bot.persona, WEB_CHAT_ID, WEB_CHAT_ID).strip() + "\n\n---\n"
            "You tell the user about new notifications from their services (mail, "
            "messengers, GitHub and so on), in your own character and voice. Keep every "
            "fact exact: names, numbers, dates, links. Do not add facts. Be brief: one "
            "short sentence per item, links on their own lines. No markdown.\n"
            + user_language_line(self.lang()))
        answer = bot.router.get_response(
            [{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.6, max_tokens=700, webchat_channel="proactive", timeout=60.0)
        answer = (answer or "").strip()
        # Ссылки должны дойти: модель их потеряла — оставляем свой текст
        urls = [it.get("url") for it in items if it.get("url")]
        if not answer or any(u not in answer for u in urls[:3]):
            return None
        return answer

    # ── для API и чата ──

    def test_notify(self) -> Optional[bool]:
        return self.deliver(notifier_mod.test_text(self.lang()), [], "test")

    def telegram_unread(self) -> Optional[List[dict]]:
        """Непрочитанные чаты Telegram; None — аккаунт подключён не в этом
        процессе (Telegram-боты, ядро «в ожидании»): тогда — по ленте."""
        if not self.active or self.telegram.client is None:
            return None
        return self.run(self.telegram.unread(), timeout=30)


hub = Hub()
