"""Суточный ритм персоны: утреннее приветствие, ночной «пора спать»,
предупреждения о погоде (features.rhythm в YAML персоны).

Триггеры:
- утро — пробуждение машины (бот работает на устройстве пользователя: во время
  сна ОС wall-clock уходит вперёд относительно monotonic, цикл это замечает)
  или первое появление пользователя (note_presence: сообщение в TG, поллинг
  веб-инбокса) в утреннем окне после долгой паузы;
- ночь — пересечение bedtime_hour (по умолчанию полночь) при недавней
  активности пользователя; один раз за ночь;
- погода — опрос прогноза Open-Meteo раз в check_interval_minutes (нужна
  настроенная локация — data/env_location.json, см. env_context): осадки
  в пределах rain_lead_hours, гроза или перепад температуры ≥ temp_delta_c.

Текст генерируется LLM в характере персоны (паттерн ReminderManager), при
сбое — статический шаблон. Отправка через MessageSender (Telegram /
веб-inbox). Замороженная персона (features.muted) молчит: событие «сгорает».
Состояние: data/{context}/rhythm_state.json.
"""

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from app.core.language import detect_dialogue_language, language_name
from app.features.env_context import _WMO_DESC, fetch_forecast, is_precip_code, load_location

logger = logging.getLogger(__name__)

_TICK_SECONDS = 60          # шаг фонового цикла
_WAKE_DRIFT_SECONDS = 300   # wall-clock минус monotonic ≥ 5 минут → машина спала
_NIGHT_WINDOW_HOURS = 2     # окно срабатывания ночного nudge после bedtime_hour
_PRESENCE_THROTTLE = 30.0   # note_presence обрабатывается не чаще раза в 30 с на чат
# Кулдауны погодных алертов (от последнего отправленного алерта этого типа)
_WEATHER_COOLDOWN = {"rain": 6 * 3600, "storm": 6 * 3600, "temp": 12 * 3600}


def _strip_markdown(text: str) -> str:
    """Убирает markdown-разметку перед отправкой (как в proactive)."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'\1', text)
    return text.strip()


def _detect_wake(wall_before: float, mono_before: float,
                 wall_after: float, mono_after: float,
                 threshold: float = _WAKE_DRIFT_SECONDS) -> bool:
    """Спала ли машина между замерами: wall-clock ушёл вперёд относительно
    monotonic (monotonic не тикает во время сна ОС, asyncio.sleep просыпается
    с опозданием — разница и есть время сна)."""
    return (wall_after - wall_before) - (mono_after - mono_before) >= threshold


# ─── Конфигурация ──────────────────────────────────────────────────────

@dataclass
class MorningConfig:
    enabled: bool = True
    window_start: int = 5     # утреннее окно [window_start, window_end), часы
    window_end: int = 12
    min_gap_hours: int = 4    # пауза до появления ≥ N часов = «начало нового дня»


@dataclass
class SleepConfig:
    enabled: bool = True
    bedtime_hour: int = 0     # час «пора спать» (0 = полночь)
    active_within_minutes: int = 120   # слать только при активности за это время


@dataclass
class WeatherConfig:
    enabled: bool = True
    check_interval_minutes: int = 30
    rain_lead_hours: int = 3  # предупреждать, если осадки в пределах N часов
    temp_delta_c: float = 8.0  # перепад температуры за ~12 часов


@dataclass
class RhythmConfig:
    enabled: bool = False
    dossier: bool = True   # отмечать события ритма в досье чата (ChatDossier)
    morning: MorningConfig = field(default_factory=MorningConfig)
    sleep: SleepConfig = field(default_factory=SleepConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)

    @classmethod
    def from_dict(cls, data) -> "RhythmConfig":
        if isinstance(data, bool):
            data = {"enabled": data}
        data = data or {}

        def _sub(dc, src):
            if isinstance(src, bool):
                src = {"enabled": src}
            out = {}
            for f in dc_fields(dc):
                val = (src or {}).get(f.name)
                if val is None:
                    continue
                try:
                    if f.type is bool:
                        val = bool(val)
                    elif f.type is int:
                        val = int(val)
                    elif f.type is float:
                        val = float(val)
                except (TypeError, ValueError):
                    continue
                out[f.name] = val
            return dc(**out)

        return cls(
            enabled=bool(data.get("enabled", False)),
            dossier=bool(data.get("dossier", True)),
            morning=_sub(MorningConfig, data.get("morning_greeting")),
            sleep=_sub(SleepConfig, data.get("sleep_nudge")),
            weather=_sub(WeatherConfig, data.get("weather_alerts")),
        )


# ─── Менеджер ──────────────────────────────────────────────────────────

# Fallback-тексты, если LLM недоступна
_FALLBACK = {
    ("morning", "Russian"): "Доброе утро!",
    ("morning", "English"): "Good morning!",
    ("night", "Russian"): "Уже за полночь — может, пора спать?",
    ("night", "English"): "It's past midnight — maybe time to get some sleep?",
    ("rain", "Russian"): "Кажется, скоро начнутся осадки — на всякий случай захвати зонт.",
    ("rain", "English"): "Looks like rain is coming — maybe grab an umbrella.",
    ("storm", "Russian"): "Собирается гроза — лучше переждать в безопасном месте.",
    ("storm", "English"): "A thunderstorm is coming — better stay safe.",
    ("temp", "Russian"): "Погода скоро сильно изменится — одевись соответствующе.",
    ("temp", "English"): "The weather is going to change a lot soon — dress accordingly.",
}

_TASK_PROMPTS = {
    "morning": "The user has just started their day (it is morning). "
               "Write a short warm morning greeting (1-2 sentences) in your character.",
    "night": "It is late night and the user is still awake. Write a short gentle "
             "message in your character suggesting they get some sleep (1-2 sentences).",
    "rain": "You noticed the weather forecast and want to warn the user about coming "
            "precipitation. Write a short warning (1-2 sentences) in your character, "
            "naturally mentioning the essence.",
    "storm": "You noticed the weather forecast and want to warn the user about an "
             "incoming thunderstorm. Write a short warning (1-2 sentences) in your character.",
    "temp": "You noticed the weather will change dramatically soon. Write a short "
            "warning (1-2 sentences) in your character so the user can dress accordingly.",
}

# Как событие отмечается в досье чата (features.rhythm.dossier)
_EVENT_TEXT = {
    "morning": {"Russian": "утреннее приветствие", "English": "morning greeting"},
    "night": {"Russian": "напоминание, что поздно и пора спать",
              "English": "late-night sleep nudge"},
    "rain": {"Russian": "предупреждение о приближающихся осадках",
             "English": "rain warning"},
    "storm": {"Russian": "предупреждение о приближающейся грозе",
              "English": "thunderstorm warning"},
    "temp": {"Russian": "предупреждение о резкой смене погоды",
             "English": "sharp weather change warning"},
}


class RhythmManager:
    """Утренние приветствия / ночные «пора спать» / погодные предупреждения.
    Фоновая asyncio-задача в event loop бота (как ReminderManager)."""

    def __init__(self, context: str, config,
                 router=None, persona=None, memory=None,
                 activity_tracker=None, sender=None, muted_check=None,
                 dossier=None):
        self.context = context
        self.config = config if isinstance(config, RhythmConfig) else RhythmConfig.from_dict(config)
        self._router = router
        self._persona = persona
        self._memory = memory
        self._tracker = activity_tracker
        self._sender = sender
        self._muted_check = muted_check
        self._dossier = dossier  # ChatDossier — отметки событий (config.dossier)

        self._base_dir = Path(f"data/{context}")
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._file = self._base_dir / "rhythm_state.json"
        self._lock = threading.Lock()

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._aio_loop: Optional[asyncio.AbstractEventLoop] = None
        self._presence_ts: Dict[str, float] = {}   # chat_id → последнее появление (in-memory)
        self._busy: set = set()                    # (chat_id, kind) — защита от дублей
        self._next_weather_ts = 0.0

        # {"chats": {chat_id: {morning_date, night_key}},
        #  "weather": {kind: {"ts": float}}}
        self._state = {"chats": {}, "weather": {}}
        self._load()

    # ── сеттеры (живое подключение, как у ReminderManager) ──

    def set_sender(self, sender):
        self._sender = sender

    def update_config(self, data) -> None:
        """Живое обновление конфига из YAML (веб-настройки без рестарта)."""
        self.config = RhythmConfig.from_dict(data)

    # ── persistence ──

    def _load(self):
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._state = {"chats": data.get("chats") or {},
                                   "weather": data.get("weather") or {}}
            except Exception as e:
                logger.warning(f"[Rhythm] Не удалось загрузить состояние: {e}")

    def _save(self):
        """Атомарная запись (как reminders): temp-файл + rename."""
        import os
        import tempfile
        try:
            payload = json.dumps(self._state, ensure_ascii=False, indent=2)
            fd, tmp_path = tempfile.mkstemp(dir=str(self._base_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp_path, self._file)
            except Exception:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[Rhythm] Не удалось сохранить состояние: {e}")

    def _chat_state(self, chat_id: str) -> dict:
        with self._lock:
            return dict(self._state["chats"].get(str(chat_id)) or {})

    def _mark(self, chat_id: str, **kv):
        with self._lock:
            cs = self._state["chats"].setdefault(str(chat_id), {})
            cs.update(kv)
            self._save()

    def _mark_weather(self, kind: str):
        with self._lock:
            self._state["weather"].setdefault(kind, {})
            self._state["weather"][kind]["ts"] = time.time()
            self._save()

    # ── чаты и активность ──

    def _chats(self) -> List[str]:
        """Все чаты, с которыми работает ритм: чаты трекера (сообщения TG),
        чаты, отмеченные в состоянии, и чаты, замеченные через presence."""
        chats = {str(c) for c in self._presence_ts}
        if self._tracker is not None:
            try:
                chats.update(str(c) for c in self._tracker.get_known_chats())
            except Exception:
                pass
        with self._lock:
            chats.update(str(c) for c in self._state["chats"])
        return sorted(chats)

    def _last_seen(self, chat_id: str) -> float:
        """Когда пользователя видели в последний раз: сообщения (трекер)
        или presence-сигналы (поллинг веб-инбокса)."""
        ts = self._presence_ts.get(str(chat_id), 0.0)
        if self._tracker is not None:
            try:
                ts = max(ts, self._tracker.get_last_activity(str(chat_id)) or 0.0)
            except Exception:
                pass
        return ts

    # ── решения (чистые, для тестов) ──

    def _should_morning_greet(self, chat_state: dict, now: datetime, last_seen: float) -> bool:
        m = self.config.morning
        if not (self.config.enabled and m.enabled):
            return False
        if not (m.window_start <= now.hour < m.window_end):
            return False
        if chat_state.get("morning_date") == now.date().isoformat():
            return False
        if last_seen <= 0 or now.timestamp() - last_seen < m.min_gap_hours * 3600:
            return False
        return True

    def _should_night_nudge(self, chat_state: dict, now: datetime, last_seen: float) -> bool:
        s = self.config.sleep
        if not (self.config.enabled and s.enabled):
            return False
        hours_past = (now.hour - s.bedtime_hour) % 24
        if hours_past > _NIGHT_WINDOW_HOURS:
            return False
        if chat_state.get("night_key") == self._night_key(now, s.bedtime_hour):
            return False
        if last_seen <= 0 or now.timestamp() - last_seen > s.active_within_minutes * 60:
            return False
        return True

    @staticmethod
    def _night_key(now: datetime, bedtime_hour: int) -> str:
        """Ключ «ночи»: момент bedtime начинает новую ночь (полночь → новая дата,
        23:00 → ночь относится к текущему дню)."""
        return (now - timedelta(hours=bedtime_hour % 24)).date().isoformat()

    def _weather_alert(self, forecast: dict, wstate: dict, now: datetime):
        """Решение по прогнозу → (kind, facts) или None. kind: storm|rain|temp.
        wstate — снапшот состояния погоды (кулдауны)."""
        w = self.config.weather
        now_ts = now.timestamp()

        def _cooldown_ok(kind: str) -> bool:
            last = (wstate.get(kind) or {}).get("ts", 0)
            return now_ts - last >= _WEATHER_COOLDOWN[kind]

        hours = forecast.get("hours") or []

        # Гроза в пределах lead — приоритетнее осадков
        for h in hours:
            code = h.get("code")
            if code is not None and int(code) >= 95:
                in_h = (h["time"] - now).total_seconds() / 3600
                if 0 <= in_h <= w.rain_lead_hours and _cooldown_ok("storm"):
                    desc = _WMO_DESC.get(int(code), "thunderstorm")
                    return "storm", f"thunderstorm ({desc}) expected in ~{in_h:.0f} h"
                break

        # Осадки: сейчас сухо, но в пределах lead начнутся
        cur_code = forecast.get("current_code")
        if not is_precip_code(cur_code):
            for h in hours:
                in_h = (h["time"] - now).total_seconds() / 3600
                if not 0 <= in_h <= w.rain_lead_hours:
                    continue
                code = h.get("code")
                prob = h.get("precip_prob") or 0
                if (is_precip_code(code) or (isinstance(prob, (int, float)) and prob >= 70)) \
                        and _cooldown_ok("rain"):
                    desc = _WMO_DESC.get(code, "precipitation") if code is not None else "precipitation"
                    prob_s = f"{int(prob)}%" if isinstance(prob, (int, float)) else "high"
                    return "rain", f"precipitation ({desc}, probability {prob_s}) expected in ~{in_h:.0f} h"
                if is_precip_code(code) or (isinstance(prob, (int, float)) and prob >= 70):
                    break  # осадки есть, но кулдаун не прошёл

        # Перепад температуры за ~12 часов
        cur_temp = forecast.get("current_temp")
        temps = [h for h in hours if isinstance(h.get("temp"), (int, float))]
        if isinstance(cur_temp, (int, float)) and temps:
            later = max(temps, key=lambda h: h["time"])
            delta = float(later["temp"]) - float(cur_temp)
            in_h = (later["time"] - now).total_seconds() / 3600
            if abs(delta) >= w.temp_delta_c and _cooldown_ok("temp"):
                direction = "drop" if delta < 0 else "rise"
                return ("temp",
                        f"temperature {float(cur_temp):+.0f}°C now, "
                        f"{float(later['temp']):+.0f}°C in ~{in_h:.0f} h ({direction} {abs(delta):.0f}°C)")
        return None

    # ── генерация текста ──

    def _lang(self, chat_id: str) -> str:
        """Язык сообщения: общий детектор app.core.language по последним
        репликам ПОЛЬЗОВАТЕЛЯ из чата (реплики ассистента и синтетика не
        считаются). Без истории — русский."""
        if self._memory is not None:
            try:
                lang = detect_dialogue_language(
                    "", self._memory.stm.get_last(8, chat_id=str(chat_id)))
                if lang:
                    return language_name(lang)
            except Exception:
                pass
        return "Russian"

    def _generate_text(self, kind: str, facts: str, lang: str) -> Optional[str]:
        """Текст в характере персоны через LLM (синхронный вызов)."""
        if not (self._router and self._persona):
            return None
        persona_prompt = self._persona.system_prompt.strip()
        if kind == "morning":
            user_content = (f"Current time: {facts}. Greet the user." if lang == "English"
                            else f"Текущее время: {facts}. Поздоровайся с пользователем.")
        elif kind == "night":
            user_content = (f"Current time: {facts}. The user is still awake." if lang == "English"
                            else f"Текущее время: {facts}. Пользователь ещё не спит.")
        else:
            user_content = (f"Weather fact: {facts}" if lang == "English"
                            else f"Факт о погоде: {facts}")
        messages = [
            {"role": "system", "content": (
                f"{persona_prompt}\n\n---\n{_TASK_PROMPTS[kind]} "
                f"Write the message in {lang}. "
                "Do NOT use markdown. Do NOT write meta-notes."
            )},
            {"role": "user", "content": user_content},
        ]
        response = self._router.get_response(messages, temperature=0.7, max_tokens=200, top_p=0.9)
        if not response or len(response.strip()) < 5:
            return None
        return _strip_markdown(response.strip())

    # ── отправка ──

    def _muted(self) -> bool:
        return bool(self._muted_check and self._muted_check())

    async def _send(self, chat_id: str, text: str, kind: str, lang: str = "Russian") -> bool:
        if self._sender is None:
            return False
        topic_id = None
        if self._tracker is not None:
            try:
                topic_id = self._tracker.get_topic(str(chat_id))
            except Exception:
                pass
        try:
            ok = await self._sender.send_message(chat_id, text, topic_id=topic_id)
        except Exception as e:
            logger.error(f"[Rhythm] Ошибка отправки в {chat_id}: {e}")
            return False
        if ok:
            logger.info(f"[Rhythm] {kind} → chat {chat_id}: {text[:60]}")
            # Логируем в STM — картина в буфере и в чате одна (как reminders)
            if self._memory is not None:
                try:
                    await asyncio.to_thread(
                        self._memory.add_message, "assistant", text,
                        user_id=chat_id, chat_id=chat_id,
                    )
                except Exception as e:
                    logger.warning(f"[Rhythm] Не удалось записать в STM: {e}")
            self._note_dossier(chat_id, kind, lang)
        return bool(ok)

    def _note_dossier(self, chat_id: str, kind: str, lang: str):
        """Отметить событие ритма в досье чата — персона видит его в контексте
        (features.rhythm.dossier, по умолчанию включено)."""
        if not (self.config.dossier and self._dossier is not None):
            return
        try:
            event = _EVENT_TEXT.get(kind, {}).get(lang) or kind
            self._dossier.record_event(str(chat_id), event)
        except Exception as e:
            logger.debug(f"[Rhythm] Не удалось отметить в досье: {e}")

    def _schedule(self, coro):
        """Запланировать корутину на цикл менеджера (потокобезопасно:
        note_presence зовётся из потоков Telegram/API)."""
        loop = self._aio_loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(lambda: loop.create_task(coro))
        except RuntimeError:
            pass

    async def _do_morning(self, chat_id: str, now: datetime, last_seen: float):
        key = (str(chat_id), "morning")
        if key in self._busy:
            return
        self._busy.add(key)
        try:
            # last_seen — снапшот с момента триггера: presence-метка обновляется
            # уже после планирования задачи, её тут учитывать нельзя
            if not self._should_morning_greet(self._chat_state(chat_id), now, last_seen):
                return
            if self._muted():
                # Замороженная персона молчит, приветствие сгорает (как напоминания)
                self._mark(chat_id, morning_date=now.date().isoformat())
                return
            lang = self._lang(chat_id)
            text = None
            try:
                text = await asyncio.to_thread(
                    self._generate_text, "morning", f"{now:%A}, {now:%d.%m.%Y, %H:%M}", lang)
            except Exception as e:
                logger.warning(f"[Rhythm] LLM генерация утра не удалась: {e}")
            text = text or _FALLBACK[("morning", lang)]
            if await self._send(chat_id, text, "morning", lang):
                self._mark(chat_id, morning_date=now.date().isoformat())
        finally:
            self._busy.discard(key)

    async def _do_night(self, chat_id: str, now: datetime, last_seen: float):
        key = (str(chat_id), "night")
        if key in self._busy:
            return
        self._busy.add(key)
        try:
            if not self._should_night_nudge(self._chat_state(chat_id), now, last_seen):
                return
            if self._muted():
                self._mark(chat_id, night_key=self._night_key(now, self.config.sleep.bedtime_hour))
                return
            lang = self._lang(chat_id)
            text = None
            try:
                text = await asyncio.to_thread(
                    self._generate_text, "night", f"{now:%A}, {now:%d.%m.%Y, %H:%M}", lang)
            except Exception as e:
                logger.warning(f"[Rhythm] LLM генерация ночи не удалась: {e}")
            text = text or _FALLBACK[("night", lang)]
            if await self._send(chat_id, text, "night", lang):
                self._mark(chat_id, night_key=self._night_key(now, self.config.sleep.bedtime_hour))
        finally:
            self._busy.discard(key)

    # ── триггеры ──

    def note_presence(self, chat_id):
        """Сигнал «пользователь появился» (сообщение в TG / поллинг веб-инбокса).
        Дёшево: троттлинг + чистые проверки; отправка уходит задачей на цикл."""
        if not (self._running and self._sender is not None):
            return
        chat_id = str(chat_id)
        now_ts = time.time()
        if now_ts - self._presence_ts.get(chat_id, 0.0) < _PRESENCE_THROTTLE:
            return
        now = datetime.now()
        # Решение — ДО обновления presence-метки, иначе пауза «нового дня» обнулится
        seen = self._last_seen(chat_id)
        if self._should_morning_greet(self._chat_state(chat_id), now, seen):
            self._schedule(self._do_morning(chat_id, now, seen))
        self._presence_ts[chat_id] = now_ts

    def _on_wake(self, now: datetime, slept_minutes: float):
        logger.info(f"[Rhythm] Пробуждение машины (сон ≈ {slept_minutes:.0f} мин)")
        for chat_id in self._chats():
            seen = self._last_seen(chat_id)
            if self._should_morning_greet(self._chat_state(chat_id), now, seen):
                self._schedule(self._do_morning(chat_id, now, seen))

    def _check_night(self, now: datetime):
        if not (self.config.enabled and self.config.sleep.enabled):
            return
        for chat_id in self._chats():
            seen = self._last_seen(chat_id)
            if self._should_night_nudge(self._chat_state(chat_id), now, seen):
                self._schedule(self._do_night(chat_id, now, seen))

    async def _check_weather(self, now: datetime):
        if not self._chats():
            return
        cfg = load_location()
        if cfg.get("mode") not in ("manual", "geo") or "lat" not in cfg:
            return  # локация не настроена — погодные алерты тихо пропускаем
        try:
            forecast = await asyncio.to_thread(fetch_forecast, cfg)
        except Exception as e:
            logger.warning(f"[Rhythm] Прогноз недоступен: {e}")
            return
        if not forecast:
            return
        with self._lock:
            wstate = {k: dict(v) for k, v in self._state.get("weather", {}).items()}
        decision = self._weather_alert(forecast, wstate, now)
        if not decision:
            return
        kind, facts = decision
        if self._muted():
            self._mark_weather(kind)  # сгорает, как напоминание при заморозке
            return
        any_sent = False
        for chat_id in self._chats():
            lang = self._lang(chat_id)
            text = None
            try:
                text = await asyncio.to_thread(self._generate_text, kind, facts, lang)
            except Exception as e:
                logger.warning(f"[Rhythm] LLM генерация погоды ({kind}) не удалась: {e}")
            text = text or _FALLBACK[(kind, lang)]
            if await self._send(chat_id, text, kind, lang):
                any_sent = True
        # Отмечаем только реальную отправку: сбой сети → повтор через интервал
        if any_sent:
            self._mark_weather(kind)

    # ── фоновый цикл ──

    async def _loop(self):
        logger.info(f"[Rhythm] Цикл запущен для context={self.context}")
        self._next_weather_ts = time.time() + 90  # первая проверка погоды — после старта
        while self._running:
            wall_before, mono_before = time.time(), time.monotonic()
            await asyncio.sleep(_TICK_SECONDS)
            drift = (time.time() - wall_before) - (time.monotonic() - mono_before)
            woke = drift >= _WAKE_DRIFT_SECONDS
            try:
                now = datetime.now()
                if woke:
                    self._on_wake(now, drift / 60.0)
                self._check_night(now)
                if (self.config.enabled and self.config.weather.enabled
                        and time.time() >= self._next_weather_ts):
                    interval = max(5, self.config.weather.check_interval_minutes) * 60
                    self._next_weather_ts = time.time() + interval
                    await self._check_weather(now)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[Rhythm] Ошибка в цикле: {e}")

    def start(self, loop=None):
        """Запускает фоновую задачу. Идемпотентна (живое включение поверх
        работающего цикла — no-op)."""
        if self._running:
            return
        if not loop:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.error("[Rhythm] Нет running event loop")
                return
        self._running = True
        self._aio_loop = loop
        self._task = loop.create_task(self._loop())
        logger.info(f"[Rhythm] Запущено для {self.context}")

    def stop(self):
        """Останавливает фоновую задачу."""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("[Rhythm] Остановлено")
