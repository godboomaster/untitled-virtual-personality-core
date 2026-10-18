"""Команды телефону: будильник, таймер, список будильников, «открой
приложение», фонарик.

Исполняет их само приложение на телефоне (web/src/phone/: разбор фразы,
плагин PhoneActions на Java) — ядро ничего не делает, ему приходит итог
(ChatRequest.phone_action). Отсюда — заметка персоне в промпт хода: ответ
честно говорит, что сделано, а что нет, и не «ставит» будильник на словах.

Тексты — зеркало web/src/phone/notes.ts (без связи с ядром заметку собирает
телефон); scripts/test_phone_actions.py сверяет их на одних отчётах.
"""

from typing import Optional

# Calendar Android: 1 — воскресенье … 7 — суббота; в заметке — с понедельника
_DAY_NAMES = {1: "Sunday", 2: "Monday", 3: "Tuesday", 4: "Wednesday",
              5: "Thursday", 6: "Friday", 7: "Saturday"}

_NOT_DONE = "Say so honestly and briefly."


def _clean(text) -> str:
    # Подпись будильника и имя приложения — в кавычках внутри заметки
    return " ".join(str(text or "").replace('"', "'").split())[:80]


def _hm(r: dict) -> str:
    return f"{int(r.get('hour') or 0):02d}:{int(r.get('minute') or 0):02d}"


def _days(days) -> str:
    s = sorted({int(d) for d in days or [] if 1 <= int(d) <= 7}, key=lambda d: (d + 5) % 7)
    if not s:
        return ""
    if len(s) == 7:
        return " every day"
    if s == [2, 3, 4, 5, 6]:
        return " on weekdays"
    if s == [7, 1]:
        return " on weekends"
    return " every " + ", ".join(_DAY_NAMES[d] for d in s)


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _duration(seconds) -> str:
    sec = int(seconds or 0)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    parts = [_plural(n, u) for n, u in ((h, "hour"), (m, "minute"), (s, "second")) if n]
    return " ".join(parts) or "0 seconds"


def _label(r: dict) -> str:
    label = _clean(r.get("label"))
    return f' labelled "{label}"' if label else ""


def _asked(r: dict) -> str:
    """Что просили — для заметки «не дали из фона»."""
    kind = r.get("kind")
    if kind == "alarm":
        return f"an alarm at {_hm(r)}"
    if kind == "timer":
        return f"a timer for {_duration(r.get('seconds'))}"
    if kind == "open_app":
        return f'to open "{_clean(r.get("app"))}"'
    if kind == "torch":
        return f"to turn the flashlight {'on' if r.get('on') else 'off'}"
    return "to show the alarms"


def action_note(r: dict) -> str:
    """Заметка персоне по итогу команды телефону (поля — PhoneActionReport)."""
    kind = r.get("kind")
    status = r.get("status")
    app = _clean(r.get("app"))
    if status == "background":
        # Голосом при свёрнутом приложении: Android не даёт открыть часы или
        # другое приложение из фона без права «Поверх других приложений» —
        # служба показала уведомление, нажатие на которое это сделает
        return (f"The user asked by voice for {_asked(r)}, but Android does not let the app open the "
                "clock or other apps while it is in the background, so nothing was done yet. A "
                "notification has appeared on the phone: tapping it will do it. "
                f"{_NOT_DONE} Tell the user to tap the notification, or to allow the app \"Display "
                "over other apps\" (the app's settings, under listening for a name) so it works by "
                "voice right away. Do NOT say that it was done.")
    if kind == "alarm":
        hm = _hm(r)
        if status == "ok":
            return (f"The app on the user's phone has just set an alarm in the phone's clock app "
                    f"for {hm}{_days(r.get('days'))}{_label(r)}. Confirm it briefly, in your own style.")
        if status == "ask":
            return ("The user wants an alarm on the phone but did not say the time, so nothing "
                    "was set yet. Ask briefly what time to set it for.")
        if status == "no_handler":
            return (f"The user asked for an alarm at {hm}, but the phone has no clock app that "
                    f"accepts alarms from other apps, so NO alarm was set. {_NOT_DONE} The user "
                    "can set it in the clock app by hand. Do NOT say the alarm was set.")
        if status == "unsupported_date":
            when = "today" if r.get("nearest") == "today" else "tomorrow"
            return (f"The user asked for an alarm at {hm} on a particular day, but the phone's "
                    f"clock app can only set the nearest {hm} ({when}) or repeat it on days of "
                    f"the week, so NO alarm was set. {_NOT_DONE} Do NOT say the alarm was set.")
        return (f"The app on the user's phone tried to set an alarm for {hm}, but it failed, so "
                f"NO alarm was set. {_NOT_DONE} Do NOT say the alarm was set.")
    if kind == "timer":
        dur = _duration(r.get("seconds"))
        if status == "ok":
            return (f"The app on the user's phone has just started a timer for {dur}{_label(r)} "
                    "in the phone's clock app. Confirm it briefly, in your own style.")
        if status == "ask":
            return ("The user wants a timer on the phone but did not say for how long, so "
                    "nothing was started yet. Ask briefly for how long.")
        if status == "no_handler":
            return (f"The user asked for a timer for {dur}, but the phone has no clock app that "
                    f"accepts timers from other apps, so NO timer was started. {_NOT_DONE} "
                    "Do NOT say the timer was started.")
        return (f"The app on the user's phone tried to start a timer for {dur}, but it failed, "
                f"so NO timer was started. {_NOT_DONE} Do NOT say the timer was started.")
    if kind == "alarms_list":
        if status == "ok":
            return ("The app on the user's phone has just opened the list of alarms in the "
                    "phone's clock app. You cannot see the alarms themselves. Reply briefly.")
        return ("The user asked to see their alarms, but the phone has no clock app that can "
                f"show them, so nothing was opened. {_NOT_DONE}")
    if kind == "alarm_off":
        if status == "ok":
            return ("The user asked to turn off or delete an alarm. The app cannot change or "
                    "delete alarms by itself, so it has opened the list of alarms in the phone's "
                    "clock app for the user to do it there. Say so briefly. Do NOT say any alarm "
                    "was turned off or deleted.")
        return ("The user asked to turn off or delete an alarm, but the app cannot change alarms "
                f"and found no clock app to open, so nothing was changed. {_NOT_DONE} Do NOT say "
                "any alarm was turned off or deleted.")
    if kind == "open_app":
        if status == "ok":
            return (f'The app on the user\'s phone has just opened "{app}" on the phone. '
                    "Confirm it briefly, in your own style.")
        if status == "not_found":
            return (f'The user asked to open "{app}" on the phone, but no installed app with '
                    f"that name was found, so nothing was opened. {_NOT_DONE} Do NOT say that "
                    "anything was opened.")
        if status == "ambiguous":
            listed = "; ".join(f"{i}. {_clean(c)}" for i, c in enumerate(r.get("candidates") or [], 1))
            return (f'The user asked to open "{app}" on the phone, and several installed apps '
                    f"match: {listed}. Nothing was opened yet. Ask briefly which one, listing them "
                    "with these numbers; the user can answer with a number or a name.")
        return (f'The app on the user\'s phone tried to open "{app}", but it failed, so nothing '
                f"was opened. {_NOT_DONE} Do NOT say that it was opened.")
    if kind == "torch":
        state = "on" if r.get("on") else "off"
        if status == "ok":
            return (f"The app on the user's phone has just turned the flashlight {state}. "
                    "Confirm it briefly, in your own style.")
        if status == "no_handler":
            return (f"The user asked to turn the flashlight {state}, but the phone has no "
                    f"flashlight, so nothing happened. {_NOT_DONE}")
        if status == "busy":
            return (f"The user asked to turn the flashlight {state}, but the camera is busy with "
                    f"another app, so nothing happened. {_NOT_DONE}")
        return (f"The app on the user's phone tried to turn the flashlight {state}, but it "
                f"failed, so nothing happened. {_NOT_DONE}")
    return ("The app on the user's phone could not do what the user asked, so nothing was "
            f"done. {_NOT_DONE} Do NOT say that it was done.")


def request_note(report) -> Optional[str]:
    """Заметка из ChatRequest.phone_action (pydantic-модель или dict)."""
    if report is None:
        return None
    data = report.model_dump() if hasattr(report, "model_dump") else dict(report)
    return action_note(data)
