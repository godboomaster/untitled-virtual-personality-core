"""
BotInstance — один бот с конкретной персоной и набором фич.
Содержит VirtualPersonality, FileVectorDB и читает features из YAML.
"""

import contextvars
import re
import os
import json
import threading
import time
import yaml
import logging
from contextlib import asynccontextmanager, contextmanager, nullcontext
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from app.core.persona import PersonaLayer, _format_msg_ts
from app.core.addons import TurnInfo, load_addons
from app.core.dialog_scope import dialog_scope
from app.core.language import detect_language, detect_dialogue_language, user_language_line
from app.core.memory import MemoryManager
from app.core.router import ModelRouter
from app.core.config import Config
from app.core.file_vector_db import FileVectorDB
from app.core.file_reader import extract_text, MAX_FILE_SIZE_DEFAULT
from app.core.interfaces import MessageSender
from app.core.presence import web_presence
from app.core import timeutil
from app.core.atomic_io import atomic_write_json, load_json_safe
from app.core.paths import data_dir
from app.core.turn_gate import ChatTurnGate, begin_turn_async
from app.core.users import get_username
from app.features.todo_manager import (
    TodoManager, is_todo_request, extract_task,
    is_todo_done_request, extract_todo_done_index, is_todo_list_request,
    is_explicit_todo_request, is_explicit_todo_done_request, resolve_done_marker,
)
from app.features.reminder_manager import (
    ReminderManager, parse_reminder, parse_recurring, parse_postpone,
    extract_postpone_hint, format_schedule, parse_cancel_reminder,
    is_list_reminders_request, is_reminder_create_request, is_pending_decline,
    looks_like_time_attempt,
)
from app.features.learning_manager import LearningManager, parse_frequency, classify_continue_answer
from app.features.learning_intent import extract_subject
from app.features.inventory_manager import (
    InventoryManager,
    is_inventory_add_request,
    is_inventory_remove_request,
    extract_inventory_item,
    extract_inventory_remove,
    explicit_inventory_item,
)
from app.features import list_offers
from app.features.list_offers import ListOffers
from app.features.computer_control import (
    ComputerControlManager, classify_confirmation, config_enabled as cc_config_enabled,
    MARKER_RE, PAGE_REF, parse_cart_request, parse_click_request, parse_close_request,
    parse_control_mode, parse_download_request, parse_erase_request,
    parse_hover_request,
    parse_key_request,
    parse_media_request,
    parse_open_on_page, parse_open_many, parse_open_request, parse_open_with_url,
    parse_page_question,
    parse_page_view_request, parse_read_request, page_view_text,
    page_view_full_text, parse_scroll_request, parse_scroll_to_goal,
    parse_search_on_site, parse_send_request,
    parse_slider_request,
    parse_tab_list_query, parse_tab_op, parse_tab_switch, parse_type_request,
    parse_zoom_request, _MORE_PHOTOS_RE)
from app.features.computer_control import (
    SOFT_STOP_RE, STOP_CMD_RE, command_has_secret, command_secret_values,
    is_goal_task,
    looks_like_command, next_video_recipe, normalize_command,
    split_compound_command, tag_origin)
from app.features.scenario_manager import ScenarioManager
from app.features.task_agent import TaskAgent, parse_task_request
from app.features.search_gate import search_skip_reason

logger = logging.getLogger(__name__)

# Ленивое создание гейта хода у ботов, собранных без __init__ (тестовые
# заготовки): два потока не должны создать два разных гейта
_TURN_GATE_INIT_LOCK = threading.Lock()
# Якорь STM хода не удалось снять (ошибка чтения STM) — хвост хода неизвестен
_NO_ANCHOR = object()

# Сколько последних реплик передавать в query rewriter для разрешения кореференций
# кореференций - местоимения и указательные слова, которые ссылаются на что-то из предыдущего контекста
_REWRITE_HISTORY = 8

# Эвристика обрыва ответа по max_tokens (learning_manager._looks_truncated —
# та же логика, но там она приватная для LLM-вызовов учебного модуля).
_SENTENCE_END_RE = re.compile(r'[.!?…»"\)\]]\s*$')

# Хвостовой «декор» реплики — каомодзи/эмодзи вроде «(。•̀ᴗ-)✧», «(´• ω •`)ノ» —
# не признак обрыва: срезаем и судим по тексту до него (знаки завершения
# фразы в классе-исключении, поэтому срезка остановится на них).
_TRAILING_DECOR_RE = re.compile(r'[^0-9A-Za-zА-Яа-яЁё.!?…»"\)\]]+$')

# «почини браузер» / «открой капчу» — rescue пула H (web_extended): пул
# веб-чатов перезапускается ВИДИМЫМ, пользователь решает капчу руками;
# возврат в headless — сам, когда капч/входов не ждёт ни один процесс бота
# (web_llm._finish_rescue_if_done из _challenge_check/_login_restored/поиска)
_RESCUE_BROWSER_RE = re.compile(
    r"^\s*(?:(?:почини|починить|открой|пройди|реши|решить)\s+"
    r"(?:браузер|веб-?чат\w*|капч\w+|челлендж)\w*|"
    r"(?:fix|repair)\s+(?:the\s+)?(?:browser|web\s*chats?)|"
    r"(?:open|solve|pass)\s+(?:the\s+)?(?:captcha|challenge))\s*[.!…]*\s*$",
    re.IGNORECASE)

# Скачивание «с сайта X» / «на сайте X» / «с example.ru» — не с текущей
# страницы: это цель для агента задач
_DL_ELSEWHERE_RE = re.compile(
    r"(?:^|\s)(?:с|со|на|from|on)\s+(?:сайт[аеу]?\s+\S+|"
    r"[\w.-]+\.[a-zа-яё]{2,}(?:/\S*)?)", re.IGNORECASE)

# «стоп» при идущем ходе режима управления — ловится ДО лока чата
# (cc_turn_enter): голая команда остановки, без продолжения фразы. Правило
# одно с отменой задачи агента (computer_control.STOP_CMD_RE)
_CC_STOP_RE = STOP_CMD_RE

# Ход, пришедший из скина (process_message(from_skin=True)): флаг потока
# хода — его видит _cc_allowed, единая точка авторизации режима управления.
# Скин — сторонний код в песочнице и мог отправить реплику сам, без человека,
# поэтому в таком ходе нет ни команд, ни подтверждений «да», ни маркеров LLM.
# Фоновые потоки (инициатива, прогоны агента) флага не видят
_SKIN_TURN = threading.local()

# Шаговые команды: повтор — намеренный («громче» ×3, «дальше» ×2), дублем
# не считается никогда (см. cc_turn_enter)
_CC_STEP_CMD_RE = re.compile(
    r"^(?:(?:ещё|еще|сделай|чуть)\s+)*(?:по)?(?:громче|тише)\b|"
    r"^(?:вниз|вверх|дальше|далее|назад|вперёд|вперед|ещё|еще|"
    r"next|back|up|down|louder|quieter|more)\s*[.!…]*$|"
    r"^(?:нажми|кликни|жми|тыкни|click|press|tap)\s+(?:на\s+)?"
    r"(?:кнопку\s+)?(?:далее|дальше|следующ\w*|вперёд|вперед|next|continue|"
    r"продолжить)\b|"
    # Перемотка на N секунд/минут вперёд/назад — повтор намеренный
    # («перемотай на 10 секунд вперёд» ×2 = +20 с)
    r"^(?:(?:пере|про|от)мота(?:й|йте|ть)|мотни|seek|skip|rewind|"
    r"fast[\s-]?forward|forward|back|jump)\b.*"
    r"(?:\b(?:\d+|пару|несколько)\s*"
    r"(?:сек\w*|с\b|мин\w*|seconds?|secs?|s\b|minutes?|mins?|m\b)|"
    r"\b(?:полминуты|минуту|секунду|a\s+minute)\b)|"
    r"^(?:(?:пере|про|от)мота(?:й|йте|ть)|мотни)\s+(?:чуть\s+|немного\s+)?"
    r"(?:вперёд|вперед|назад)\s*[.!…]*$", re.IGNORECASE)
# Прочие (не тяжёлые и не шаговые) повторы — дубль только в этом окне:
# двойная доставка сообщения, а не осознанный повтор
_CC_DUP_WINDOW_SEC = 1.5

# Шаг составной команды после открытия сайта: «(открой ютуб) и включи
# музыку» — воспроизведение/поиск на ЭТОМ сайте (см. _cc_ladder)
_CHAIN_PLAY_RE = re.compile(
    r"^\s*(?:(?P<play>включи|поставь|запусти|play|put\s+on)|"
    r"(?P<find>найди|поищи|ищи|search(?:\s+for)?|find|look\s+up))\s+"
    r"(?P<query>\S.*?)\s*[.!?…]*$", re.IGNORECASE)


def _fmt_reminder_choices(choices: list) -> str:
    """Нумерованный список напоминаний для LLM-контекста: 1) "задача" at
    12:30. Без служебного id: модель переписывала «[r3f9a2]» в ответ
    пользователю. Ответ номером (resolve_postpone_choice) сверяется со
    списком id, запомненным на момент вопроса, — порядок тот же."""
    parts = []
    for i, c in enumerate(choices):
        # Время пользователя (TIMEZONE), а не системный пояс процесса
        when_dt = timeutil.from_ts(c["trigger_at"])
        when = when_dt.strftime("%H:%M")
        if when_dt.date() != timeutil.today():
            when = when_dt.strftime("%d.%m %H:%M")
        parts.append(f"{i+1}) \"{c.get('task') or '?'}\" at {when}")
    return "; ".join(parts)


def _fmt_reminder_list(items: list) -> str:
    """Список напоминаний для LLM-контекста ответа «какие у меня напоминания?»:
    1) "задача" — at 12:30 (by Аня). Повтор — расписанием, пауза — пометкой.
    Без служебного id (модель переписывала его в ответ): «отмени напоминание
    2» — строка показанного списка (ReminderManager.note_listed)."""
    parts = []
    for i, r in enumerate(items):
        if r.get("recurrence"):
            when = format_schedule(r["recurrence"])
        else:
            when_dt = timeutil.from_ts(r["trigger_at"])
            when = when_dt.strftime("%H:%M")
            if when_dt.date() != timeutil.today():
                when = when_dt.strftime("%d.%m %H:%M")
            when = f"at {when}"
        if r.get("paused"):
            when += " (paused)"
        # В группе имя хранится тегом «Имя (ID)» — id модели не нужен
        who_name = re.sub(r"\s*\(\d+\)\s*$", "", r.get("user_name") or "")
        who = f" (by {who_name})" if who_name else ""
        parts.append(f"{i + 1}) \"{r.get('task') or '?'}\" — {when}{who}")
    return "; ".join(parts)


def _reminder_declined_context(task: Optional[str]) -> str:
    task_disp = f" \"{task}\"" if task else ""
    return (
        f"The user decided not to set the reminder{task_disp}. Nothing was scheduled. "
        "Acknowledge briefly in your own style. Do NOT say any reminder was set."
    )


def _reminder_cancel_context(result: dict) -> str:
    """LLM-контекст после отмены напоминания текстом — честно: отменено ли
    что-то и что именно."""
    if result.get("none"):
        return ("The user asked to cancel a reminder, but there are NO active reminders. "
                "NOTHING was cancelled. Say so briefly in your own style.")
    if result.get("cancelled"):
        names = ", ".join(f"\"{r.get('task') or '?'}\"" for r in result["cancelled"])
        return (f"The user asked to cancel reminders. Cancelled: {names} — this is ALREADY "
                "done. Confirm briefly in your own style. Name the reminders exactly as given.")
    if result.get("ambiguous"):
        return ("NOTHING was cancelled yet. Several reminders match: "
                f"{_fmt_reminder_list(result['ambiguous'])}. Your reply MUST ask which one "
                "to cancel (number or words from the task), showing the numbered list above. "
                "In your own style, briefly.")
    listing = _fmt_reminder_list(result.get("active") or [])
    return ("The user asked to cancel a reminder, but none of the active reminders "
            f"matches. NOTHING was cancelled. Active reminders: {listing}. Say so and show "
            "the list — in your own style, briefly.")


def _postpone_result_context(result: Optional[dict]) -> str:
    """LLM-контекст после попытки переноса напоминания (см. process_message).
    Явно сообщаем, применён ли перенос, — иначе модель «подтверждала»
    перенос, которого на самом деле не было."""
    if not result:
        return (
            "The user asked to move a reminder, but there is nothing to move "
            "(no active and no recently fired reminders). Say there is nothing "
            "to move — in your own style, briefly. Do NOT confirm any rescheduling."
        )
    when_dt = timeutil.from_ts(result["trigger_at"])
    when = when_dt.strftime("%H:%M")
    if when_dt.date() != timeutil.today():
        when = when_dt.strftime("%d.%m %H:%M")
    task_disp = f" '{result['task']}'" if result.get("task") else ""
    if result.get("recreated"):
        return (
            f"The user asked to move a reminder{task_disp}, but it had already fired, "
            f"so a NEW reminder with the same task was created — at {when}. "
            f"The reminder is scheduled — confirm briefly in your own style. "
            f"Name the reminder exactly as given above."
        )
    return (
        f"The user asked to move the reminder{task_disp}. "
        f"It is now scheduled for {when} — the change is ALREADY applied. "
        f"Confirm briefly in your own style. "
        f"Name the reminder exactly as given above."
    )


def _looks_truncated(text: str) -> bool:
    """Эвристика обрыва ответа по max_tokens: текст не заканчивается знаком
    завершения предложения. Каомодзи/эмодзи на хвосте («…выбрали? (。•̀ᴗ-)✧»)
    обрывом не считаются."""
    t = (text or "").rstrip()
    if not t:
        return False
    t = _TRAILING_DECOR_RE.sub("", t).rstrip()
    if not t:
        return False
    return not _SENTENCE_END_RE.search(t)


# Эвристика: похоже ли сообщение на ответ про частоту уроков (а не на произвольную реплику
# или запрос другой фичи). Используется, чтобы бот в setup-состоянии (ждёт «как часто?»)
# активировался на «раз в день»/«каждые 2 часа», но НЕ на длинное сообщение или посторонний
# текст. Короткое (до ~12 слов) И содержит временнУю лексику — типичный ответ о периодичности.
_FREQUENCY_WORDS_RE = re.compile(
    r"\b(?:раз\s+в|кажды[ей]|каждую|через|полчаса|ежечасн\w*|ежедневн\w*|еженедельн\w*|интервал\w*|"
    r"час(?:а|ов|у|е|ом|ы|ами|ах)?\b|минут(?:а|ы|у|е|ой|ам|ами|ах)?\b|"
    r"день\b|дн[юяей]\w*|недел[ьюя]\w*|месяц\w*|секунд\w*)",
    re.IGNORECASE,
)


# Эвристика: похоже ли сообщение на исправление/поправку бота или просьбу запомнить.
# Срабатывание лишь запускает локальную LLM-формулировку правила (см. ниже) — дёшево.
_CORRECTION_HINT_RE = re.compile(
    r"\b(?:не\s+так|неправильно|неверно|я\s+име[лл]\s+в\s+виду|запомни|не\s+называй|"
    r"не\s+надо\s+так|не\s+говори\s+так|поправ\w*|исправь|ты\s+опять|ты\s+снова)\b",
    re.IGNORECASE,
)

# То же по-английски — отдельно, русский шаблон выше не трогаем. Каждое
# срабатывание — вызов локальной модели, ложное правило пинится навсегда,
# поэтому только явные формы: «remember» многозначно («do you remember…»,
# «remember when…», «remember to buy milk» — вопрос/воспоминание/напоминалка),
# ловим лишь повелительное в начале фразы; «wrong»/«again» сами по себе —
# обычные слова рассказа, без «that's»/«you» не ловим.
# Начало фразы: начало текста, после знака препинания или «please/and/but…»
_EN_LEAD = (r"(?:^|(?<=[.!?;:,\n(—–-])|\b(?:please|pls|plz|just|and|also|so|now|ok|"
            r"okay|hey|no|nope|oh|but|then)\b)\s*")
# До конца фразы нет «?» — вопрос («remember my name?»), а не просьба.
# Окно ограничено: без него длинная фраза с сотнями «that's wrong» без точки
# проверялась бы квадратично
_EN_NOQ = r"(?![^.!\n]{0,500}\?)"
_EN_DONT = r"(?:do(?:n['’]?t|\s+not)|never)"
_CORRECTION_HINT_EN_RE = re.compile("|".join([
    # «please remember …», «remember, please …». «Please remember the scenario …» —
    # команда сценария, не правило: ни вне режима управления, ни в нём (там
    # вежливая форма до сценариев не доходит и раньше правилом не становилась)
    r"\b(?:please|pls|plz)\s*(?:,\s*)?remember\b"
    r"(?!\s+(?:to\b(?!\s+(?:always|never|not)\b)|when\b|(?:this\s+|the\s+)?scenario\b))",
    r"\bremember\s*(?:,\s*)?(?:please|pls|plz)\b(?![\s,]+(?:this\s+|the\s+)?scenario\b)",
    # «remember: …», «remember, I'm vegan», «remember this!», «remember that I …»,
    # «remember I don't eat meat», «remember my name is …», «remember to never …»
    _EN_LEAD + r"remember(?:"
    r"\s*[:—–]|\s+-\s"
    r"|\s*,\s*(?!(?:when|how|what|where|who|why|the\s+time|that\s+time|back|last|"
    r"yesterday|this|that)\b)"
    r"|\s+(?:this|that)\s*(?:[:!.—–]|$)"
    r"|\s+that\s+(?:i|i['’]?m|im|i['’]?ve|i['’]?d|my|me|we|we['’]?re|our|you|you['’]?re|"
    r"your|it['’]?s|there|nobody|no\s+one|everyone)\b"
    r"|\s+(?:i['’]?m|im|i\s+am|i\s+(?:do\s+not|don['’]?t|can['’]?t|cannot|never|always|"
    r"hate|love|like|prefer|dislike|want|need)|my\s+[\w'’]+(?:\s+[\w'’]+)?\s+(?:is|are))\b"
    r"|\s+(?:to\s+(?:always|never|not)|not\s+to)\b"
    r")" + _EN_NOQ,
    # «keep in mind», «bear that in mind», «don't forget that …», «for future reference»
    _EN_LEAD + r"(?:keep|bear)\s+(?:(?:this|that|it)\s+)?in\s+mind\b",
    _EN_LEAD + r"do(?:n['’]?t|\s+not)\s+forget\s*"
    r"(?:that\b|this\b|:|to\s+(?:always|never|not)\b)",
    r"\bfor\s+future\s+reference\b",
    # «from now on, don't …/always …/answer …» (но не «from now on I'm going to the
    # gym»; «from now on call me X» — имя, его забирает «зови меня», как в русском)
    _EN_LEAD + r"(?:from\s+now\s+on|going\s+forward)\s*(?:,\s*)?(?:please\s+)?"
    r"(?:don['’]?t|do\s+not|never|always|use|answer|reply|respond|speak|talk|write|"
    r"say|stop|be|only|just|keep|remember|avoid|i\s+want\s+you|"
    r"i['’]?d\s+like\s+you|you\s+(?:will|should|must|are\s+to))\b",
    # «that's wrong», «that's not right», «that's not what I meant», «you're wrong»
    # (но не «that's wrong of me», «it's wrong to steal», «is that wrong?»)
    r"\b(?:that|this)(?:['’]?s|\s+is|\s+was)\s+(?:wrong|incorrect|not\s+(?:right|correct|"
    r"true|it|what\s+i\s+(?:meant|asked|said|wanted|mean)))\b"
    r"(?!\s+(?:to|of|for|with|about|because|when|if|now|anymore)\b)" + _EN_NOQ,
    _EN_LEAD + r"not\s+what\s+i\s+(?:meant|asked|said|wanted)\b",
    r"\byou(?:['’]?re|re|\s+are|\s+were)\s+wrong\b" + _EN_NOQ,
    r"\byou(?:['’]?ve|\s+have)?\s+(?:got|gotten)\s+(?:it|that|this|me|my\s+[\w'’]+)\s+"
    r"(?:all\s+)?wrong\b",
    r"\byou\s+(?:misunderstood|misheard|misread|mixed\s+(?:(?:it|that|them|things)\s+)?up)\b",
    # «not like that» в начале фразы (не «it's not like that between us»)
    _EN_LEAD + r"not\s+(?:like\s+(?:that|this)|that\s+way|this\s+way)\b",
    # «I meant …», «what I meant» (но не «I meant to call you», «I meant it»; «I mean» —
    # слово-паразит, не ловим; «I meant to ask / tell you, …», «I meant what I said»,
    # «I meant no offense» — начало разговора и идиомы, не поправка)
    r"\bi\s+meant\b(?!\s+(?:to\b(?!\s+(?:say|write|type)\b)|it\b|well\b|"
    r"every\s+word\b|what\s+i\s+said\b|no\s+(?:harm|offen[cs]e|disrespect)\b))",
    # «don't call me …», «don't say it like that», «don't do that», «never use …»
    # (но не «don't call me tomorrow», «don't worry», «don't mention it»)
    _EN_LEAD + _EN_DONT + r"\s+(?:ever\s+)?(?:"
    # За «call me» — слово (обращение): «don't call me, I'll call you» — про звонок
    r"call\s+me\b(?=\s+[\"'“‘«]?[^\W\d_])"
    r"(?!\s+(?:tomorrow|tonight|today|later|now|again|back|anymore|after|"
    r"before|at|on|in|until|till|during|while|when|if|unless|early|late|every|next|this|"
    r"here|there|from|up|out|so|too)\b)"
    r"|address\s+me\b|refer\s+to\s+me\b"
    r"|(?:say|write|answer|reply|respond|talk|speak)\s+(?:it\s+|that\s+|to\s+me\s+)?"
    r"(?:like\s+(?:that|this)|that\s+way|this\s+way)\b"
    r"|say\s+(?:that|this|it)\b|do\s+(?:that|this)\b"
    r"|use\b|swear\b|curse\b|apologi[sz]e\b|repeat\s+(?:yourself|that|this|it|the\s+same)\b"
    r"|end\s+(?:with|every|each|your)\b"
    r"|ask\s+me\s+(?:about|that|this|again|so\s+many|questions|if|whether)\b"
    r")",
    # «stop calling me …», «stop using emoji» (но не «I need to stop using my phone»)
    _EN_LEAD + r"stop\s+(?:calling\s+me|saying|using|asking|apologi[sz]ing|repeating|"
    r"doing\s+(?:that|this)|talking\s+(?:like|to\s+me\s+like)|being\s+so|adding|ending)\b",
    # «you did it again», «you keep forgetting», «again you …» (≈ «ты опять/снова»;
    # но не «see you again», «thanks again»)
    r"\byou(?:['’]?(?:re|ve)|\s+(?:are|have))?\s+(?:did|done|doing|said|saying|used|using|"
    r"forgot|forgotten|called\s+me|calling\s+me|asked|asking|messed\s+up)\b"
    r"[^.!?\n]{0,30}?\bagain\b",
    r"\byou\s+keep\s+(?:doing|saying|calling\s+me|using|asking|forgetting|repeating|"
    r"ignoring|adding|ending|getting\s+(?:it|that|this|my\s+[\w'’]+)\s+wrong|"
    r"making\s+the\s+same)\b",
    _EN_LEAD + r"(?:once\s+)?again(?:\s*,)?\s+you\b(?!\s+(?:too|as\s+well)\b)",
    # «correct yourself», «fix that.» (но не «how do I fix it?», «fix this bug»)
    r"\bcorrect\s+yourself\b",
    _EN_LEAD + r"(?:correct|fix)\s+(?:that|this|it)\s*(?:(?:,\s*)?(?:please|pls|plz)\s*)?"
    r"(?:[.!,;\n]|$)",
    # «how many times do I have to tell you», «I told you not to …»
    r"\bhow\s+many\s+times\s+(?:do\s+i\s+(?:have|need)\s+to|have\s+i|did\s+i|must\s+i)\s+"
    r"(?:tell|told|say|said|ask|asked|repeat|remind)",
    r"\bi\s+(?:already\s+|just\s+)?(?:told|asked)\s+you\s+"
    r"(?:not\s+to(?!\s+worry\b)|to\s+(?:stop|never|always|not))\b",
    r"\bi\s+said\s+(?:not\s+to|don['’]?t|do\s+not|never)\b",
]), re.IGNORECASE)
_WS_RUN_RE = re.compile(r"\s{2,}")


def _looks_like_correction(text: Optional[str], typed: Optional[str] = None) -> bool:
    """Похоже ли на поправку бота или просьбу запомнить правило (запускает
    извлечение правила). RU — по всему вводу, как раньше; EN — только по
    тому, что человек написал сам (typed: подпись к фото/файлу): «remember
    that…», «don't use…» в английском тексте документа или OCR — обычное
    дело, а правило из чужого текста запинилось бы навсегда."""
    if _CORRECTION_HINT_RE.search(text or ""):
        return True
    # Пробельные серии — в один символ (перевод строки сохраняем: он граница
    # фразы): на сотнях пустых строк «\s*» начала фразы перебирался квадратично
    en = _WS_RUN_RE.sub(lambda m: "\n" if "\n" in m.group() else " ",
                        (text if typed is None else typed) or "")
    return bool(_CORRECTION_HINT_EN_RE.search(en))


# Обращение к персоне в начале фразы (BotInstance._strip_address): «hey
# Connor …», «эй Коннор, …»
_ADDRESS_LEAD = r"\s*(?:(?:hey|hi|ok|okay|so|эй)\b\s*,?\s*)?"
# Имя-модальный глагол без знака после — часть фразы, а не обращение:
# «Will remember that!» у персоны Will — «(я) запомню», а не «Уилл, запомни»
_ADDRESS_MODAL_NAMES = frozenset(
    "will may can shall must might would could should do does did".split())
# «…сценарий утро, пожалуйста» / «save the scenario as X please» — вежливый
# хвост не часть имени сценария
_POLITE_TAIL_RE = re.compile(
    r"(?<=\S)[\s,]+(?:пожалуйста|плиз|please|pls)\s*[.!…]*\s*$", re.IGNORECASE)


# Маркеры фич в ответе LLM (вырезает BotInstance._cut_markers)
def _marker_re(tag: str, body: str = r"[^\]]+") -> "re.Pattern":
    return re.compile(rf"\[{tag}:({body})\]")


# Факт может содержать скобки одного уровня: «Привычка: [секрет] грызёт ногти»
_PUNISH_FACT_MARK_RE = _marker_re("PUNISH:FACT", r"(?:[^\[\]\n]|\[[^\[\]\n]*\])+")
_TODO_DONE_MARK_RE = _marker_re("TODO_DONE", r"[^\]]*")
_TODO_ADD_MARK_RE = _marker_re("TODO_ADD")
_INV_USE_MARK_RE = _marker_re("INVENTORY_USE")
_INV_ADD_MARK_RE = _marker_re("INVENTORY_ADD")
_INV_REMOVE_MARK_RE = _marker_re("INVENTORY_REMOVE")


def _cap_first(text: str) -> str:
    # Название предмета с заглавной: «шоколадку» → «Шоколадку»
    return text[:1].upper() + text[1:] if text else text


# «Запомни, пожалуйста, сценарий …» → «запомни сценарий …» (для разбора
# команды сценария вне режима управления, см. _is_scenario_save_command)
_POLITE_LEAD_RE = re.compile(r"^(?:(?:пожалуйста|плиз|please|pls)[\s,]+)+", re.IGNORECASE)
_POLITE_AFTER_VERB_RE = re.compile(
    r"^((?:запомни|запиши|сохрани|save|remember|record))[\s,]+"
    r"(?:(?:пожалуйста|плиз|please|pls)[\s,]+)+", re.IGNORECASE)


# «Зови меня X» — предпочитаемое имя пользователя. Глагол — целым словом:
# «позови / обзови меня …» — не просьба об имени; «назови меня» и вежливые
# «зовите / называйте» — просьба. Мягкие слова перед
# именем пропускаем, как «just» в английском: «просто Саша», «лучше Сашей»,
# «своим котиком»
_ALIAS_RE = re.compile(
    r"(?<![^\W\d_])(?:зови|называй|назови)(?:те)?\s+меня\s+"
    r"(?:(?:просто(?:-напросто)?|лучше|теперь|отныне|впредь|всегда|только|уже|уж|тогда|"
    r"пожалуйста|плиз|плз|пж|пжл|пжлст|своей|своим)\s+)*"
    r"([А-Яа-яЁёA-Za-z\-]{2,30})", re.IGNORECASE)
# «Не зови / не называй меня X» — запрет (его формулирует правило), а не имя
_ALIAS_NEG_RU_RE = re.compile(r"(?<![^\W\d_])не\s*$", re.IGNORECASE)
# Первое слово после «зови меня», которое именем не бывает: «зови меня так /
# завтра / когда будет готово», «называй меня как хочешь / на ты / кем угодно».
# Имена, похожие на служебные слова (Ник, Люба, Ли, Ян, Мир), сюда не входят
_ALIAS_RU_STOP = frozenset("""
так как этак иначе никак всяко когда тогда если раз пока чтобы чтоб будто словно хотя чуть
где куда откуда почему зачем сколько
завтра сегодня вчера послезавтра потом позже попозже позднее раньше пораньше сейчас сразу
сначала сперва теперь отныне впредь всегда никогда иногда часто редко опять снова вновь
обратно уже уж ещё еще лучше хуже просто только тоже также даже лишь вот ведь же ну да нет
не ни бы утром днём днем вечером ночью срочно скорее быстрее немедленно обязательно вместе
сюда туда здесь там тут домой назад вперёд вперед наверх вниз везде всюду
пожалуйста плиз плз пж пжл пжлст ладно хорошо ок окей давай
нормально правильно неправильно ласково нежно вежливо официально уважительно вслух тише
громче полностью
я ты он она оно мы вы они меня тебя его её ее их нас вас мне тебе ему ей им нам вам мной
мною тобой тобою ним ней нею ними нами вами себя собой себе
мой моя моё мое мои моим моей моими твой твоя твоё твое твои твоим твоей свой своя своё
свое свои своим своей наш наша наше наши нашим нашей ваш ваша ваше ваши вашим вашей
это этим этой этот эта эти этими то тем той тот та те теми такой таким такая такое такие
такими какой каким какая какое какие какими сам сама само сами самим самой
кто кем что чем чего кого ком чём никем ничем ничего никого никто ничто
весь вся всё все всем всеми всей любым любая другим другой другая иным иной кое
в во на к ко по с со за из от до у о об обо при про для без под над перед через между
вместо около после кроме ради сквозь вроде насчёт насчет
и а но или либо зато однако ибо нежели
имя именем имени никнейм никнеймом прозвище прозвищем кличка кличкой
полным настоящим нормальным новым старым прежним коротким сокращённым сокращенным
уменьшительным официальным
зови называй
""".split())
# Инфинитив — приглашение, а не имя: «зови меня гулять / обедать / кататься».
# «-сть» не берём: «Радость», «Прелесть» — прозвища
_ALIAS_RU_INF_RE = re.compile(r"(?:[аеёиоуыэюя]ть|ться|тись|чь|чься)$")
# Уже именительный, хотя кончается как творительный: «Алексей», «Артём»,
# «Ефрем», прозвища «Герой», «Ковбой»; «дорогой / родной» — и муж.
# именительный, и жен. творительный: оставляем как написано
_ALIAS_RU_NOM_KEEP = frozenset("""
алексей андрей сергей матвей тимофей елисей гордей евсей корней моисей фаддей авдей ерофей
макей мокей аггей агей еремей пантелей дорофей варфоломей елизей аникей фалалей
соловей воробей муравей злодей чародей лицедей кощей бармалей
артём артем ефрем рустем вилем салем бахром акром ахром икром
толстой герой супергерой ковбой плейбой изгой малой большой крутой седой молодой дорогой
родной святой простой золотой чужой лихой плохой немой слепой глухой
""".split())
# Беглая гласная и средний род — не по правилу: «Павлом» → «Павел»
_ALIAS_RU_INSTR_SPECIAL = {
    "павлом": "павел", "львом": "лев", "орлом": "орёл", "псом": "пёс", "чудом": "чудо",
    "солнцем": "солнце", "сердцем": "сердце", "товарищем": "товарищ",
}
# Ласковые прилагательные: «любимой» → «любимая», «любимым» → «любимый»;
# ударные: «родным» → «родной» (их «-ой» — см. _ALIAS_RU_NOM_KEEP)
_ALIAS_RU_ADJ = frozenset("""
любим единственн красив нежн сладк маленьк хорошеньк хорош лучш умн славн добр ласков
прекрасн послушн ненаглядн драгоценн бесценн желанн
""".split())
_ALIAS_RU_ADJ_STRESSED = frozenset(
    "родн дорог золот молод крут свят прост больш сед лих плох мал зл".split())
_RU_VOWELS = "аеёиоуыэюя"
# Англ.: «call me X», «address me as X», «refer to me as X», «I go by X».
# Одно слово, как в русском; «Dr. Smith» — с титулом, иначе в имя попадёт «Dr»
_ALIAS_EN_RE = re.compile(
    r"\b(?:call\s+me(?:\s+as)?|address\s+me\s+as|refer\s+to\s+me\s+as)\s+"
    r"(?:just\s+|simply\s+)?[\"'“‘]?"
    r"((?:(?:mr|mrs|ms|mx|dr|prof)\.?\s+)?([^\W\d_]+(?:['’\-][^\W\d_]+)*))"
    r"(?!\w|['’\-][^\W\d_])",
    re.IGNORECASE)
_ALIAS_GO_BY_EN_RE = re.compile(
    r"\bi(?:\s+(?:usually|mostly|just|actually|now|prefer\s+to)|(?:['’]d|\s+would)\s+rather)?"
    r"\s+go\s+by\s+(?:the\s+name\s+(?:of\s+)?)?[\"'“‘]?"
    r"(([^\W\d_]+(?:['’\-][^\W\d_]+)*))(?!\w|['’\-][^\W\d_])",
    re.IGNORECASE)
# Имя заменяется везде, поэтому «call me» — только просьба в начале своей
# части фразы: «please / you can / I'd like you to call me X». Так «don't
# call me X», «why do you call me X», «my friends call me X» имени не задают
_ALIAS_EN_CLAUSE_SPLIT_RE = re.compile(
    r"[.!?;,:…()\[\]\"«»“”\n—–]|\s-+\s|\b(?:but|and|so|then|or)\b", re.IGNORECASE)
_ALIAS_EN_SOFT = (r"(?:please|pls|plz|kindly|just|simply|also|now|always|only|instead|"
                  r"maybe|perhaps|rather|from\s+now\s+on|henceforth|going\s+forward)")
_ALIAS_EN_LEAD_RE = re.compile(
    r"\s*(?:(?:ok(?:ay)?|well|hey|hi|hello|oh|yes|yeah|yep|sure|alright|anyway|btw|"
    r"actually|honestly|" + _ALIAS_EN_SOFT + r")\s+)*"
    r"(?:(?:(?:you|u|ya)(?:\s+(?:can|could|may|should|will|must|shall)|['’]ll)"
    r"|(?:can|could|will)\s+(?:you|u|ya)"
    # «would you call me smart?» — вопрос о мнении; просьба — только с please
    r"|would\s+(?:you|u|ya)\s+(?:please|kindly)"
    r"|i(?:['’]d|\s+would)?\s+(?:really\s+|much\s+)?(?:like|love|prefer|want|wish)\s+"
    r"(?:(?:it\s+)?(?:if|that)\s+)?(?:you|u|ya)(?:\s+(?:to|would|could|can|will)|['’]d)?"
    r"|i(?:['’]d|\s+would)\s+rather\s+(?:you|u|ya)(?:\s+would|['’]d)?"
    r"|(?:feel\s+free|remember|be\s+sure|make\s+sure)\s+to"
    r"|how\s+about\s+(?:you|u|ya)|why\s+not"
    r")\s+(?:" + _ALIAS_EN_SOFT + r"\s+)*)?",
    re.IGNORECASE)
# Слова после «call me», которые именем не бывают: звонок («call me back /
# tomorrow / at 5 / a taxi»), местоимения («call me that»), идиомы «call me crazy»,
# транспорт («I go by Uber.», «call me taxi»)
_ALIAS_EN_STOP = frozenset("""
back up out over down off home in on at by to for from with via about after before around
between during until till til through using like as if when whenever once unless while
because cause cuz than so too also ever even very more less most instead
tomorrow tmrw tmr today tonight tonite later soon now asap again sometime sometimes someday
anytime early late first next last yesterday morning evening afternoon noon midnight weekend
daily weekly monthly nightly maybe perhaps please pls plz ok okay right straight immediately
urgently directly personally privately quickly quick fast real really
monday tuesday wednesday thursday friday saturday sunday
a an the that this these those it its some any no not never every each all both either neither
one someone somebody anyone anybody everyone everybody nobody something anything nothing
everything whatever whichever whoever what which who whom whose how why where there here
me myself you yourself him her them us my your his our their mine yours i he she they we such
names name two three four five six seven eight nine ten eleven twelve twenty thirty half
couple few mr mrs ms mx dr prof
crazy stupid paranoid biased lazy naive naïve silly weird picky sentimental cautious
traditional mad insane nuts dumb selfish childish foolish romantic nostalgic petty ignorant
boring cheesy corny nerdy geeky obsessed spoiled spoilt difficult demanding impatient strange
odd slow dense dramatic extra basic emotional soft pedantic fussy stubborn uncultured simple
greedy lame judgmental judgemental shallow bonkers mental old old-fashioned oldfashioned
old-school oldschool square uptight prude prudish skeptic sceptic cynic pessimistic optimistic
idealistic unrealistic neurotic sarcastic arrogant entitled insensitive rude mean harsh cruel
cold kooky wacky quirky eccentric peculiar lucky unlucky blessed cursed weak fragile choosy
snobby snobbish posh fancy vain proud stingy frugal thrifty jaded sheltered close-minded
closed-minded narrow-minded self-centered self-centred
uber lyft taxi cab bus train metro subway tube tram car bike ambulance
""".split())
# Прилагательные по суффиксу («call me superstitious / careful / clueless /
# sensitive»). «-ish», «-ic» и короткие «-ive/-able» не берём: Manish, Eric,
# Clive, Mable — имена
_ALIAS_EN_ADJ_RE = re.compile(r"^(?:.*(?:ous|ful|less|ical|minded)|.{3,}(?:ive|able|ible|ist))$")
# «call me crazy, but …» — идиома, а не имя
_ALIAS_EN_IDIOM_TAIL_RE = re.compile(
    r"\s*(?:[,—–-]\s*)?but\b(?!\s+(?:not|never|no|don['’]?t|do\s+not|please)\b)", re.IGNORECASE)
# «I go by X» — имя в конце фразы; «I go by Walmart every day» — не имя
_ALIAS_GO_BY_TAIL_RE = re.compile(
    r"\s*(?:$|[^\w\s]|(?:now|these\s+days|nowadays|though|tho|actually|instead|here|online|"
    r"usually|mostly|for\s+short|btw)\b)", re.IGNORECASE)


def _alias_en_name(text: str, m: "re.Match", go_by: bool) -> Optional[str]:
    # Начало части фразы ищем в окне перед «call me», а не во всём тексте:
    # иначе тысячи «call me …» в длинном тексте разбирались бы квадратично.
    # Вводная часть длиннее окна просьбой не бывает
    ws = max(0, m.start() - 300)
    cut = None
    for cut in _ALIAS_EN_CLAUSE_SPLIT_RE.finditer(text, ws, m.start()):
        pass
    if cut is None and ws:
        return None
    clause = text[cut.end() if cut else 0:m.start()]
    if not _ALIAS_EN_LEAD_RE.fullmatch(clause):
        return None
    word = m.group(2)
    low = word.lower()
    if (len(word) < 2 or low in _ALIAS_EN_STOP or low.endswith(("'s", "’s"))
            or _ALIAS_EN_ADJ_RE.match(low)):
        return None
    if go_by:
        # Имя — с заглавной: «I go by feel / by the rules» — не имя
        if not word[0].isupper() or not _ALIAS_GO_BY_TAIL_RE.match(text, m.end()):
            return None
    elif _ALIAS_EN_IDIOM_TAIL_RE.match(text, m.end()):
        return None
    alias = " ".join(m.group(1).split())
    return alias if len(alias) <= 30 else None


def _alias_ru_nom_low(low: str) -> str:
    # Творительный → именительный по окончанию (слово в нижнем регистре)
    special = _ALIAS_RU_INSTR_SPECIAL.get(low)
    if special:
        return special
    end, stem = low[-2:], low[:-2]
    if end in ("ой", "ей", "ым", "им"):
        if stem in _ALIAS_RU_ADJ:
            return stem + {"ым": "ый", "им": "ий"}.get(end, "ая")
        if stem in _ALIAS_RU_ADJ_STRESSED and end in ("ым", "им"):
            return stem + "ой"
    # Без гласной в основе — уже именительный: «Ной», «Том», «Джей», «Грей»
    if len(stem) < 2 or not any(c in _RU_VOWELS for c in stem):
        return low
    last = stem[-1]
    if end in ("ей", "ёй"):
        # Сашей → Саша, Серёжей → Серёжа; Олей → Оля, Марией → Мария, Ильёй → Илья
        return stem + ("а" if last in "жшчщц" else "я")
    if end == "ой":
        return stem + "а"                          # Мариной → Марина
    if end == "ью":
        return stem + "ь"                          # Любовью → Любовь
    if end == "ым":
        return stem + "ый"                         # милым → милый
    if end not in ("ом", "ем", "ём"):
        return low
    # Беглая гласная: котёнком → котёнок, Сашком → Сашок, Саньком → Санёк,
    # отцом → отец, красавцем → красавец (но «Принцем» → «Принц»)
    if stem.endswith(("ёнк", "онк")):
        return stem[:-1] + "ок"
    if stem.endswith(("ышк", "ишк", "ечк")):
        return stem + "о"                          # солнышком → солнышко
    if stem.endswith("ьк"):
        return stem[:-2] + "ёк"
    if last == "к" and stem[-2] in "жшч":
        return stem[:-1] + "ок"
    if last == "ц" and len(stem) >= 3 and stem[-2] in "тйвдм":
        return stem[:-2] + ("е" if stem[-2] == "й" else stem[-2] + "е") + "ц"
    if end == "ом":
        return stem                                # Александром → Александр
    if last == "ь":
        # счастьем → счастье, соловьём → соловей
        return stem + "е" if end == "ем" else stem[:-1] + "ей"
    if last in _RU_VOWELS:
        return stem + "й"                          # Алексеем → Алексей, Юрием → Юрий
    if stem.endswith("ищ"):
        return stem + "е"                          # дружищем → дружище
    if last in "жшчщц":
        return stem                                # Тёмычем → Тёмыч, Ильичём → Ильич
    return stem + "ь"                              # Игорем → Игорь, Королём → Король


def _alias_ru_nominative(word: str) -> str:
    """«Сашей» → «Саша», «Александром» → «Александр» без морфологической
    библиотеки (имя подставляется во все промпты — творительный там режет
    слух); именительный («Саша», «Алексей») и латиница — как есть, регистр —
    как написал человек."""
    if "-" in word:
        # «Анной-Марией» → «Анна-Мария»: части склоняются порознь
        return "-".join(_alias_ru_nominative(p) if len(p) >= 2 else p
                        for p in word.split("-"))
    low = word.lower()
    if low in _ALIAS_RU_NOM_KEEP or not "а" <= low[-1] <= "я":
        return word
    new = _alias_ru_nom_low(low)
    if new == low:
        return word
    p = 0
    while p < min(len(low), len(new)) and low[p] == new[p]:
        p += 1
    return word[:p] + (new[p:].upper() if word.isupper() else new[p:])


def _alias_ru_name(word: str) -> Optional[str]:
    # Имя из «зови меня X»; None — после «меня» не имя («так», «завтра», «гулять»)
    low = word.lower()
    if (low in _ALIAS_RU_STOP or low.split("-")[0] in _ALIAS_RU_STOP
            or _ALIAS_RU_INF_RE.search(low)):
        return None
    return _alias_ru_nominative(word)


def _extract_alias(text: str, typed: Optional[str] = None) -> Optional[str]:
    """Имя из «зови меня X» / «call me X»; None — просьбы нет. RU — по всему
    вводу, как раньше; EN — только по тому, что человек написал сам (typed:
    подпись к фото/файлу), как в _looks_like_correction: «you can call me
    Dave» в английском письме или на скриншоте имя пользователя не меняет."""
    for m in _ALIAS_RE.finditer(text or ""):
        # Окно перед «зови»: «не» стоит вплотную, весь текст не перебираем
        if _ALIAS_NEG_RU_RE.search(text, max(0, m.start() - 100), m.start()):
            continue
        alias = _alias_ru_name(m.group(1))
        if alias:
            return alias
    en = (text if typed is None else typed) or ""
    for regex, go_by in ((_ALIAS_EN_RE, False), (_ALIAS_GO_BY_EN_RE, True)):
        for m in regex.finditer(en):
            alias = _alias_en_name(en, m, go_by)
            if alias:
                return alias
    return None


def _looks_like_frequency_answer(text: str) -> bool:
    if not text or not text.strip():
        return False
    # Короткое сообщение (ответ о частоте обычно в одну строчку)
    word_count = len(text.split())
    if word_count > 12:
        return False
    return bool(_FREQUENCY_WORDS_RE.search(text))


# Ответ на «чему тебя научить?», который темой не является
_NOT_A_TOPIC_RE = re.compile(
    r"^(?:не\s+знаю|хз|потом|позже|неважно|забей|ничему|ничего|нет|не\s+надо|отмена|"
    r"never\s*mind|nothing|no|nope|idk|dunno|later)(?![a-zа-яё])",
    re.IGNORECASE,
)


# Максимальная длина «короткого» ответа на вопрос «продолжаем?» (в словах).
_CONTINUE_SHORT_ANSWER_MAX_WORDS = 6


def _is_plain_yes_no(text: str) -> bool:
    """Похоже ли сообщение на короткий ответ «да/нет» — такой безопасно принять как
    ответ на вопрос «продолжаем?» даже без Telegram-reply. Длинное сообщение, пусть и
    начинающееся с «да», обычно несёт свой вопрос/тему — его нельзя съедать ответом
    на «продолжаем?», оно должно уйти в обычную обработку."""
    return bool(text) and len(text.split()) <= _CONTINUE_SHORT_ANSWER_MAX_WORDS


class BotInstance:

    # У каждой персоны — свой BotInstance.


    def __init__(self, persona_name: str, context: str = None):
        self.persona_name = persona_name
        self.context = context or persona_name
        self.persona = PersonaLayer(persona_name=persona_name)

        # Гейт «ход пользователя ↔ фоновое сообщение» (app/core/turn_gate.py):
        # общий для API и Telegram — фоновые инициативы/ритм/напоминания/уроки
        # по нему видят, что в чате идёт живой обмен, и не пишут в STM посреди
        # хода. Создаётся первым: менеджеры ниже получают его при создании
        self.turn_gate = ChatTurnGate()

        # Список дел/инвентарь для отправки отдельным сообщением после основного ответа.
        # Per-chat (dict по chat_id): process_message крутится конкурентно в потоках для
        # разных чатов, и общий атрибут давал гонки — список одного чата мог уехать в другой.
        self._pending_list_messages: Dict[str, List[str]] = {}

        # Тип последнего ответа-вопроса бота ('frequency' | 'continue'), per-chat — по той
        # же причине: общий флаг один чат сбрасывал/перезаписывал за другим.
        self._pending_question_kind: Dict[str, Optional[str]] = {}

        # Хвост расщеплённого ответа (settings.split_messages): ответ режется
        # по абзацам на отдельные сообщения, первая часть возвращается как обычно,
        # остальные ждут здесь — платформа (веб/TG) забирает их после отправки
        # первой. Per-chat — та же защита от гонок, что у списков выше.
        self._pending_split_messages: Dict[str, List[str]] = {}

        # Скриншоты страницы для отправки пользователю (режим управления,
        # «что на странице?»): {"data": jpeg-bytes, "caption": str}.
        # Per-chat — та же защита от гонок, что у списков выше.
        self._pending_photos: Dict[str, List[dict]] = {}

        # Переспрос «Записать «X» в список дел?» / «Добавить «X» в инвентарь?»
        # (без локальной модели): отложенный вопрос чата ждёт «да»/«нет»
        # того, кого спросили (см. _list_offer_turn)
        self.list_offers = ListOffers()

        # Остаток полностраничного альбома («покажи всю страницу» режется
        # на партии по 10 — лимит media group Telegram): «ещё» досылает
        # следующую партию. {"photos": [{"data": ...}], "ts": epoch},
        # TTL 10 минут
        self._pending_more_photos: Dict[str, dict] = {}

        # Читаем features из YAML
        persona_data = self.persona.persona_data
        self.features: dict = persona_data.get("features", {}) # получаем навыки персоны

        # Уровень интеллекта: tier + overrides; без блока
        # intellect — legacy-режим, уровневые механики не активируются
        from app.core.intellect import IntellectConfig
        self.intellect = IntellectConfig(persona_data)

        # Платформенное правило финальных вопросов (conversation_style):
        # дефолт rare применяется ко ВСЕМ персонам, включая legacy
        from app.features.conversation_style import ConversationStyleConfig
        self.conversation_style = ConversationStyleConfig(persona_data)

        # STM size из YAML (fallback на Config)
        self.stm_size: int = persona_data.get("stm_size", Config.STM_SIZE)

        # Max docs из YAML (fallback на дефолт 3)
        self.max_docs: int = persona_data.get("max_docs", 3)

        # Max file size из YAML в МБ (fallback на дефолт 10 МБ)
        max_file_size_mb = persona_data.get("max_file_size_mb", 10)
        self.max_file_size: int = max_file_size_mb * 1024 * 1024

        # Trigger words
        self.trigger_words: set = set(self.features.get("trigger_words", [persona_name.lower()]))

        # File DB (только если file_upload)
        self.file_db: Optional[FileVectorDB] = None
        if self.features.get("file_upload", False):
            self.file_db = FileVectorDB(context=context, max_docs=self.max_docs)
            logger.info(f"  [{persona_name}] FileVectorDB включён")

        # Todo manager (только если todo)
        self.todo_manager: Optional[TodoManager] = None
        if self.features.get("todo", False):
            self.todo_manager = TodoManager(context=self.context)
            logger.info(f"  [{persona_name}] Todo manager включён")

        # Reminder manager — независимый флаг reminder
        self.reminder_manager: Optional[ReminderManager] = None
        if self.features.get("reminder", False):
            self.reminder_manager = ReminderManager(context=self.context)
            self.reminder_manager.set_turn_gate(self.turn_gate)
            # на primitive-tier — минимальная вербализация напоминаний
            self.reminder_manager.set_intellect_tier(self.intellect.tier)
            # Замороженная персона напоминаний не шлёт — в любом канале
            self.reminder_manager.set_muted_check(self.is_muted)
            logger.info(f"  [{persona_name}] Reminder manager включён")

        # Inventory manager (только если inventory)
        self.inventory_manager: Optional[InventoryManager] = None
        if self.features.get("inventory", False):
            self.inventory_manager = InventoryManager(context=self.context)
            logger.info(f"  [{persona_name}] Inventory manager включён")

        # Computer control (только если computer_control): открытие сайтов/
        # приложений/именованных задач на компьютере пользователя (уровень 1).
        # Режим выключен: false/отсутствует, пустой dict или enabled: false
        # внутри dict (веб-настройка фич так гасит режим, сохраняя allowlist'ы)
        self.computer_control: Optional[ComputerControlManager] = None
        cc_cfg = self.features.get("computer_control", False)
        if cc_config_enabled(cc_cfg):
            self.computer_control = ComputerControlManager(
                context=self.context, config=cc_cfg)
            logger.info(f"  [{persona_name}] Computer control включён "
                        f"(confirm={self.computer_control.confirm})")
        # Доп. allowlist режима управления (помимо владельца) — читаем
        # независимо от enabled: значение сохраняется, как и прочие
        # allowlist'ы фичи, даже пока сам режим временно выключен
        self._cc_allowed_users: set = {
            str(u).strip() for u in (cc_cfg.get("allowed_users", []) if isinstance(cc_cfg, dict) else [])
            if str(u).strip()
        }

        # Сценарии (запись/воспроизведение цепочек действий) — надстройка над
        # computer_control: без него бессмысленны. `scenarios: false` гасит.
        self.scenario_manager: Optional[ScenarioManager] = None
        if self.computer_control and self.features.get("scenarios", True):
            self.scenario_manager = ScenarioManager(
                context=self.context, computer_control=self.computer_control)
            logger.info(f"  [{persona_name}] Scenario manager включён")

        # Агент-автопилот (цель → цепочка действий с уточнениями у
        # пользователя) — тоже надстройка над computer_control.
        # `task_agent: false` гасит
        self.task_agent: Optional[TaskAgent] = None
        if self.computer_control and self.features.get("task_agent", True):
            self.task_agent = TaskAgent(computer_control=self.computer_control,
                                        context=self.context)
            logger.info(f"  [{persona_name}] Task agent включён")

        # Режим управления (per chat): computer control работает ТОЛЬКО в нём
        # («перейди в режим управления»), иначе CC-команды не перехватываются.
        # В режиме наоборот молчат напоминания/дела/инвентарь/обучение.
        # Режим переживает рестарт (control_mode.json: чат → момент последней
        # активности) и сам гаснет после простоя (idle_exit_min)
        self._control_mode: set = set()
        self._control_mode_ts: Dict[str, float] = {}
        self._control_mode_path = (data_dir() / self.context
                                   / "computer_control" / "control_mode.json")
        self._cc_mode_load()

        # Learning manager (только если learning) — режим обучения по запросу
        self.learning_manager: Optional[LearningManager] = None
        learning_cfg = self.features.get("learning", False)
        if isinstance(learning_cfg, bool):
            learning_on, learning_cfg = learning_cfg, {}
        else:
            # dict: enabled решает явно; без него непустой dict — включён (обратная совместимость)
            learning_on = bool(learning_cfg) and learning_cfg.get("enabled", True)
        if learning_on:
            self.learning_manager = LearningManager(context=self.context, config=learning_cfg)
            self.learning_manager.set_turn_gate(self.turn_gate)
            logger.info(f"  [{persona_name}] Learning manager включён")

        # Router (создаём до Memory, чтобы передать в LTM)
        self.router = ModelRouter(context=self.context)

        # Персональный выбор провайдеров (YAML, секция llm):
        # основной + приоритет fallback-цепочки + свои модели + веб-чат сайт
        # + лимиты веб-чатов (webchat_limits)
        llm_cfg = persona_data.get("llm") or {}
        if llm_cfg:
            self.router.set_persona_llm(llm_cfg.get("primary"), llm_cfg.get("fallback"),
                                        llm_cfg.get("models"), webchat=llm_cfg.get("webchat"),
                                        webchat_limits=llm_cfg.get("webchat_limits"),
                                        webchat_modes=llm_cfg.get("webchat_mode"),
                                        exclude=llm_cfg.get("exclude"))

        # Memory + Router
        self.memory = MemoryManager(
            stm_size=self.stm_size,
            enable_ltm_extraction=Config.LTM_EXTRACTION_ENABLED,
            ltm_model_provider=Config.LTM_MODEL_PROVIDER,
            load_stm_from_db=not persona_data.get("fresh_stm", False),
            context=context,
            main_router=self.router
        )
        # Секреты ввода и текст приватных страниц режима управления — в
        # историю маской (см. _cc_hist_install)
        self._cc_hist_install()

        # Web search
        self._web_search_enabled = self.features.get("web_search", False)
        self._web_search_disabled_chats: set = set()  # chat_id где /web выключил поиск
        self._web_pool = None
        if self._web_search_enabled:
            from app.features.web_search import search_web, format_web_results
            self._search_web = search_web
            self._format_web_results = format_web_results
            self._web_pool = ThreadPoolExecutor(max_workers=2)
            logger.info(f"  [{persona_name}] Web search включён (pool: 2 workers)")

        # Local router — вид общего роутера для этой персоны: движки служебных
        # задач (Ollama/веб-чат) из llm.local_tasks, веб-чаты фоновых задач —
        # из цепочки провайдеров персоны (self.router)
        self._local_router = None
        try:
            from app.core.local_router import get_local_router
            get_local_router().bind_persona(self.context, self.router,
                                            llm_cfg.get("local_tasks"))
            self._local_router = get_local_router(self.context)
        except Exception as e:
            logger.warning(f"  [{persona_name}] Локальный роутер не подключён: {e}")
        if self._web_search_enabled and self._local_router is not None:
            # Улучшение/верификация поискового запроса — движками этой персоны
            import functools
            self._search_web = functools.partial(self._search_web,
                                                 local_router=self._local_router)

        # Punish block (нужен до rate limiter: использует block_user/is_blocked)
        self._punish_enabled = self.features.get("punish_block", False)

        # Rate limiter
        self._rate_limit_enabled = self.features.get("rate_limit", False)
        self._rate_limit_individual: dict = {}
        if self._rate_limit_enabled or self._punish_enabled:
            from app.features.rate_limiter import check_rate_limit, block_user, is_blocked, get_status_text
            self._check_rate_limit = check_rate_limit
            self._block_user = block_user
            self._is_blocked = is_blocked
            self._rate_limit_status = get_status_text

        if self._rate_limit_enabled:
            # Парсим individual limits из env (RATE_LIMIT_USER_<ID>=<seconds>)
            self._rate_limit_individual = {}
            for key, value in os.environ.items():
                if key.startswith("RATE_LIMIT_USER_"):
                    uid = key[len("RATE_LIMIT_USER_"):]
                    try:
                        self._rate_limit_individual[uid] = int(value)
                    except ValueError:
                        pass
            logger.info(f"  [{persona_name}] Rate limiter включён ({len(self._rate_limit_individual)} индивидуальных)")

        # Moderation
        self._moderation_enabled = self.features.get("moderation", False)
        if self._moderation_enabled:
            from app.features.moderation import moderate_message
            self._moderate_message = moderate_message
            logger.info(f"  [{persona_name}] Модерация включена")

        # Владелец — полная защита от всех блокировок.
        # Fallback: YAML персоны → глобальный OWNER_USER_ID из окружения.
        self.owner: str = str(self.features.get("owner") or os.getenv("OWNER_USER_ID") or "")

        # Режим управления без владельца и без allowlist'а — в Telegram его
        # не сможет включить никто (is_owner всегда False, fail-closed);
        # веб/API не задет — там web_single_user сам назначает владельца.
        # Предупреждаем один раз при старте, а не молчим о дыре в конфиге.
        if self.computer_control and not self.owner and not self._cc_allowed_users:
            logger.warning(
                f"  [{persona_name}] computer_control включён, но owner не задан "
                "(ни в персоне, ни в OWNER_USER_ID) и allowed_users пуст — в Telegram "
                "режим управления недоступен никому; в веб/API однопользовательский "
                "режим сам назначит владельца")

        # Однопользовательский режим (веб/API): собеседник один — он и владелец.
        # Флаг выставляет API-реестр (app/api/runtime.py); в Telegram-режиме False.
        self.web_single_user: bool = False

        # Allowed DM users — могут писать в личку, но подлежат наказаниям
        # Пустые записи ("") отбрасываем, id приводим к str; пустой список = ЛС открыты всем
        self.allowed_dm_users: set = {
            str(u).strip() for u in self.features.get("allowed_dm_users", []) if str(u).strip()
        }
        self.blocked_users: set = {
            str(u).strip() for u in self.features.get("blocked_users", []) if str(u).strip()
        }

        # Self memory (эпизодическая память бота)
        # Режим по intellect tier: none — модуль не создаётся вообще,
        # primitive — вспышки-впечатления, full — как обычно
        self.self_memory = None
        if self.features.get("self_memory", False) and self.intellect.self_memory_mode != "none":
            from app.core.self_memory import BotSelfMemory
            self.self_memory = BotSelfMemory(
                context=context,
                persona_name=persona_name,
                router=self.router,
                mode=self.intellect.self_memory_mode,
            )
            if self.intellect.self_memory_mode == "primitive":
                logger.info(f"  [{persona_name}] Self memory в примитивном режиме (вспышки-впечатления)")

        # Living persona (слои state/world):
        # тики состояния, офлайн-события мира, суммаризация, сюжетные арки.
        # Intellect tier сужает слои: primitive — world без NPC/арок,
        # события = физические действия с инвентарём
        self.living = None
        try:
            from app.core.living_persona import LivingPersona, LivingPersonaConfig
            _living_cfg = LivingPersonaConfig(self.features)
            if _living_cfg.enabled:
                self.living = LivingPersona(
                    context=context or persona_name,
                    persona=self.persona,
                    router=self.router,
                    config=_living_cfg,
                    self_memory=self.self_memory,
                    intellect=self.intellect,
                    inventory_manager=self.inventory_manager,
                )
                logger.info(
                    f"  [{persona_name}] Living persona включена "
                    f"(state={_living_cfg.state_enabled}, world={_living_cfg.world_enabled}"
                    + (", primitive-режим" if self.intellect.is_primitive else "") + ")")
        except Exception as e:
            logger.warning(f"  [{persona_name}] Living persona не запущена: {e}")

        # Язык чата без сохранённого значения (первый запуск, новый чат) —
        # по репликам STM
        if self.living is not None:
            self.living.get_chat_user_language = self.chat_user_language

        # Напоминания знают о living-состоянии (mood/energy в тексте)
        if self.reminder_manager is not None and self.living is not None:
            self.reminder_manager.set_living(self.living)

        # Аддоны персоны (features.addons, напр. книжный RAG Арродеса):
        # свой блок промпта на ход и чистка ответа
        self.addons = []
        for addon in load_addons(self.features, persona_name):
            try:
                addon.setup(self)
            except Exception as e:
                logger.warning(f"  [{persona_name}] Аддон «{addon.name}» не запущен: {e}")
                continue
            self.addons.append(addon)

        # Proactive messaging (самоинициатива)
        self.proactive = None
        self._activity_tracker = None
        self._sender: Optional[MessageSender] = None
        # Общее досье на чаты: один экземпляр на бота (proactive + rhythm),
        # иначе два экземпляра перезатирали бы записи друг друга на диске
        self._chat_dossier = None
        proactive_config = self.features.get("proactive", {})
        if isinstance(proactive_config, bool):  # допускаем простой true/false
            proactive_config = {"enabled": proactive_config}
        if proactive_config.get("enabled", False):
            from app.features.proactive_messaging import ProactiveConfig, ProactiveMessaging, ChatActivityTracker
            self._activity_tracker = ChatActivityTracker(context=context)
            # ProactiveMessaging создаётся в setup_proactive(), когда появится sender
            self.proactive = None
            logger.info(f"  [{persona_name}] Proactive messaging подготовлен (ожидает sender)")

        # Суточный ритм: утреннее приветствие / ночной «пора спать» / погода
        self.rhythm = None
        rhythm_config = self.features.get("rhythm", {})
        if isinstance(rhythm_config, bool):  # допускаем простой true/false
            rhythm_config = {"enabled": rhythm_config}
        if rhythm_config.get("enabled", False):
            if self._activity_tracker is None:
                from app.features.proactive_messaging import ChatActivityTracker
                self._activity_tracker = ChatActivityTracker(context=context)
            # manager создастся позже через setup_rhythm(sender)
            logger.info(f"  [{persona_name}] Rhythm (утро/ночь/погода) подготовлен (ожидает sender)")

        # Банк flavor-реплик для CC-команд (AI Mode — «системные» сообщения
        # без истории/LTM): фоновая генерация, если банк отсутствует или
        # устарел (хэш system_prompt). Без CC реплики не нужны.
        if self.computer_control is not None:
            try:
                from app.features import flavor_text
                flavor_text.ensure_flavor_bank(self)
            except Exception as _fe:
                logger.debug(f"  [{persona_name}] flavor-банк не запущен: {_fe}")

        logger.info(f"  [{persona_name}] BotInstance создан | stm_size={self.stm_size} | features: {list(self.features.keys())}")

    def is_muted(self) -> bool:
        """Заморожена ли персона (features.muted), с подхватом правки YAML из
        другого процесса (см. PersonaLayer.is_muted). Свежий флаг переносится
        и в self.features — его читают остальные проверки."""
        muted = self.persona.is_muted()
        features = self.features
        if isinstance(features, dict) and bool(features.get("muted")) != muted:
            features["muted"] = muted
        return muted

    def sync_feature_managers(self) -> dict:
        """Приводит менеджеры reminder/todo/inventory в соответствие с self.features
        (живое включение/выключение фич из веб-настроек, без рестарта бота):
        создаёт недостающие, останавливает и убирает лишние. Платформенную
        обвязку (sender, запуск фонового цикла) делает вызывающая сторона —
        см. _apply_feature_managers_live в settings_api / start_bot_features в inbox.
        Возвращает {feature: включена ли} после синхронизации."""
        result = {}
        if self.features.get("reminder", False):
            if self.reminder_manager is None:
                self.reminder_manager = ReminderManager(context=self.context)
                # getattr: вызывается и на заготовках бота (тесты живого переключения)
                self.reminder_manager.set_turn_gate(getattr(self, "turn_gate", None))
                self.reminder_manager.set_intellect_tier(self.intellect.tier)
                is_muted = getattr(self, "is_muted", None)
                if callable(is_muted):
                    self.reminder_manager.set_muted_check(is_muted)
                logger.info(f"  [{self.persona_name}] Reminder manager включён (live)")
        elif self.reminder_manager is not None:
            self.reminder_manager.stop()
            self.reminder_manager = None
            logger.info(f"  [{self.persona_name}] Reminder manager выключен (live)")
        result["reminder"] = self.reminder_manager is not None

        if self.features.get("todo", False):
            if self.todo_manager is None:
                self.todo_manager = TodoManager(context=self.context)
                logger.info(f"  [{self.persona_name}] Todo manager включён (live)")
        elif self.todo_manager is not None:
            self.todo_manager = None
            logger.info(f"  [{self.persona_name}] Todo manager выключен (live)")
        result["todo"] = self.todo_manager is not None

        if self.features.get("inventory", False):
            if self.inventory_manager is None:
                self.inventory_manager = InventoryManager(context=self.context)
                logger.info(f"  [{self.persona_name}] Inventory manager включён (live)")
        elif self.inventory_manager is not None:
            self.inventory_manager = None
            logger.info(f"  [{self.persona_name}] Inventory manager выключен (live)")
        result["inventory"] = self.inventory_manager is not None
        return result

    # Trigger logic

    def should_respond(self, text: str) -> bool:
        lower = text.strip().lower()
        for trigger in self.trigger_words:
            if lower.startswith(trigger):
                return True
        return False

    def _web_race_enabled(self) -> bool:
        """Поиск гонкой AI Mode + DDG. features.web_search_ai_mode: явный
        true/false; не задан — авто: основной провайдер персоны — веб-чат
        (браузерный пул и так поднят, AI Mode живёт в нём же)."""
        flag = self.features.get("web_search_ai_mode")
        if isinstance(flag, bool):
            return flag
        primary = str(getattr(self.router, "pinned_provider", None)
                      or getattr(self.router, "active_provider", None) or "")
        return primary.startswith("webchat")

    def _race_search(self, search_args: tuple) -> list:
        """Одна нога — AI Mode (канал search), другая — тот же DDG-поиск,
        но только сниппеты. AI Mode в карантине — обычный поиск целиком."""
        from app.features import web_search_race as race
        if not race.ai_mode_available():
            return self._search_web(*search_args)
        return race.race_search(
            search_args[0], context=self.context,
            ddg_search=lambda q: self._search_web(q, *search_args[1:],
                                                  fetch_pages=False))

    def _address_names(self) -> set:
        # Как к персоне обращаются: trigger_words + id + имя из YAML.
        # getattr — бот мог быть собран без __init__ (тестовые заготовки)
        names = set(getattr(self, "trigger_words", ()) or ())
        if getattr(self, "persona_name", None):
            names.add(self.persona_name)
        display = (getattr(getattr(self, "persona", None), "persona_data", None)
                   or {}).get("name")
        if display:
            names.add(str(display))
        return names

    def strip_trigger(self, text: str) -> str:
        lower = text.strip().lower()
        for trigger in sorted(self.trigger_words, key=len, reverse=True):
            if lower.startswith(trigger):
                return text.strip()[len(trigger):].strip().lstrip(",.!?:; ")
        return text

    def _strip_address(self, text: Optional[str]) -> Optional[str]:
        """Без обращения к персоне в начале: «connor call me max», «hey
        Connor, don't …», «эй Коннор запомни …». В Telegram имя срезано до
        бота, в веб-чате и скине — нет, а английским эвристикам (правило,
        «call me X») и командам сценария нужна просьба в начале фразы. Имя —
        целым словом: «Connors …» и «Don't …» у персоны Don не режутся."""
        if not text:
            return text
        names = sorted({str(n).strip() for n in self._address_names()
                        if n and len(str(n).strip()) >= 2}, key=len, reverse=True)
        if not names:
            return text
        m = re.match(_ADDRESS_LEAD + "(" + "|".join(map(re.escape, names))
                     + r")(?![\w'’-])(\s*[,:;!?.…—–-]+)?\s*", text, re.IGNORECASE)
        if not m or (not m.group(2) and m.group(1).lower() in _ADDRESS_MODAL_NAMES):
            return text
        rest = text[m.end():]
        return rest if rest.strip() else text

    def _scenario_command_text(self, text: Optional[str]) -> str:
        """Команда сценария без обращения и «пожалуйста» (в начале, после
        глагола, в конце): «Коннор, запомни, пожалуйста, сценарий утро» →
        «запомни сценарий утро». Один разбор и в режиме управления, и для
        подсказки вне его — иначе подсказка отправляла бы в режим, где та
        же фраза сценарием не считалась (и становилась правилом)."""
        s = (self._strip_address((text or "").strip()) or "").strip()
        s = _POLITE_AFTER_VERB_RE.sub(r"\1 ", _POLITE_LEAD_RE.sub("", s))
        return _POLITE_TAIL_RE.sub("", s)

    def _is_scenario_save_command(self, text: Optional[str]) -> bool:
        """«Запомни/сохрани сценарий X», «save the scenario X» — тем же
        разбором, что в режиме управления. Не мешают обращение по имени в
        начале («Коннор, запомни сценарий …»), «пожалуйста» после глагола
        и шаги с новой строки («запомни сценарий:\\nшаг 1»)."""
        if not text:
            return False
        flat = re.sub(r"\s+", " ", text).strip()
        return ScenarioManager.parse_save_request(
            self._scenario_command_text(flat)) is not None

    def is_owner(self, user_id: str) -> bool:
        """Владелец ли пользователь: id из YAML персоны или OWNER_USER_ID.
        В однопользовательском веб-режиме собеседник всегда владелец."""
        if self.web_single_user:
            return True
        return bool(user_id) and user_id in {self.owner, os.getenv("OWNER_USER_ID", "")}

    def _cc_allowed(self, user_id: str, chat_id=None) -> bool:
        """Единая точка авторизации режима управления (браузер/ОС от имени
        пользователя): владелец персоны или allowlist фичи
        (features.computer_control.allowed_users). pre_check её НЕ покрывает —
        тот защищает обычный диалог, а общий браузер режима управления несёт
        авторизованные сессии владельца, поэтому каждый вход в CC (переключатель
        режима, rescue, fast-path, подтверждение pending, маркеры LLM, сценарии)
        обязан пройти через этот гейт. chat_id пока не используется в решении —
        зарезервирован под будущие чат-специфичные allowlist'ы/аудит.
        В ходе из скина (_SKIN_TURN) — всегда False: реплику мог написать
        не человек, а скрипт скина."""
        if getattr(_SKIN_TURN, "on", False):
            return False
        return self._cc_user_allowed(user_id)

    def _cc_user_allowed(self, user_id: str) -> bool:
        """Права пользователя на режим управления сами по себе (владелец или
        allowlist), без учёта того, откуда пришла реплика."""
        return self.is_owner(user_id) or (
            bool(user_id) and str(user_id) in self._cc_allowed_users)

    # Pre-check pipeline

    def pre_check(self, user_id: str, text: str, is_private: bool) -> Optional[str]:

        # Проверки перед обработкой. Возвращает текст ошибки или None если всё ОК.
        
        # 0. Владелец — полная защита
        if self.is_owner(user_id):
            return None

        # 1. Заблокированные
        if user_id in self.blocked_users:
            return "BLOCKED"

        # 2. DM только для разрешённых (пустой список = ЛС открыты всем)
        if is_private and self.allowed_dm_users and user_id not in self.allowed_dm_users:
            return "BLOCKED"

        # 3. Punish block
        if self._punish_enabled and self._is_blocked(user_id):
            return "PUNISH_BLOCKED"

        # 4. Rate limit
        if self._rate_limit_enabled and not self._check_rate_limit(user_id, self._rate_limit_individual):
            return "RATE_LIMITED"

        # 5. Moderation
        if self._moderation_enabled and self._moderate_message(text):
            if self._punish_enabled:
                self._block_user(user_id)
            return "MODERATION_BLOCKED"

        return None

    # ── per-chat pending-состояние (досылка списков, регистрация вопросов) ──

    def _pending_lists(self, chat_id) -> List[str]:
        # Бакет списков (дел/инвентаря) текущего чата для досылки после ответа.
        return self._pending_list_messages.setdefault(str(chat_id), [])

    def pop_pending_list_messages(self, chat_id) -> List[str]:
        """Забирает накопленные списки чата для досылки — и очищает бакет.
        Вызывается telegram-слоем после отправки основного ответа."""
        return self._pending_list_messages.pop(str(chat_id), [])

    def pop_pending_photos(self, chat_id) -> List[dict]:
        """Забирает накопленные скриншоты чата для досылки — и очищает бакет.
        Вызывается платформой после отправки основного ответа (TG шлёт
        фото, веб — dataURL в ответе)."""
        return self._pending_photos.pop(str(chat_id), [])

    def pop_pending_question_kind(self, chat_id) -> Optional[str]:
        """Забирает (и снимает) тип последнего ответа-вопроса бота для чата:
        'frequency' | 'continue' | None. Pop-семантика: флаг одноразовый, старое
        значение не может протечь в следующий ответ ни в этом, ни в чужом чате."""
        return self._pending_question_kind.pop(str(chat_id), None)

    def pop_pending_split_messages(self, chat_id) -> List[str]:
        """Забирает хвост расщеплённого ответа чата (и очищает бакет).
        Вызывается платформой сразу после process_message/command_reply —
        до pop_pending_list_messages, чтобы части ушли раньше досылаемых списков."""
        return self._pending_split_messages.pop(str(chat_id), [])

    def split_reply_parts(self, text: str) -> List[str]:
        """Расщепление ответа на отдельные сообщения (settings.split_messages).

        Маркер границы — пустая строка: каждый абзац (блок строк между пустыми
        строками) становится отдельным сообщением. Выключено или абзац один —
        возвращается [text] как есть. Пустые куски отбрасываются."""
        if not text or not text.strip():
            return []
        if not self.persona.settings.get("split_messages"):
            return [text]
        parts = [p.strip() for p in re.split(r"\n[ \t]*\n+", text)]
        parts = [p for p in parts if p]
        return parts or [text.strip()]

    def _save_assistant_reply(self, answer: str, user_id: str, chat_id: str) -> str:
        """Сохраняет ответ персоны в STM и возвращает текст для отправки.

        При включённом settings.split_messages ответ режется на абзацы: каждая
        часть пишется в STM отдельным сообщением (история совпадает с тем, что
        видит пользователь), а возвращается только первая часть — хвост ждёт
        в _pending_split_messages, его платформа досылает следом."""
        # Отметка в кадре хода «ответ на ТЕКУЩУЮ реплику записан» — ПОСЛЕ
        # записи (упала — отметки нет, реплика-ошибка ещё нужна): по ней
        # _pipeline_failure_reply отличает ответ от чужой записи ассистента
        frame = self._get_turn_gate().current_frame(self.stm_key(chat_id, user_id))
        parts = self.split_reply_parts(answer)
        if len(parts) <= 1:
            self.memory.add_message("assistant", answer, user_id, chat_id)
            if frame is not None:
                frame["answer_saved"] = True
            return answer
        for part in parts:
            self.memory.add_message("assistant", part, user_id, chat_id)
        if frame is not None:
            frame["answer_saved"] = True
        self._pending_split_messages[str(chat_id)] = parts[1:]
        return parts[0]

    # ── Приватность истории режима управления ──
    # Секрет из команды («введи Kotik2019! в поле пароль») и текст приватной
    # страницы (вход/оплата/банк) человек видит в ответе, но в STM/БД/LTM они
    # не пишутся: история уходит облачной модели с каждым следующим ходом.
    # Маски копятся в кадре хода (живут до его конца) там, где действие
    # исполнено/отложено, а применяются в единой точке записи — обёртке
    # memory.add_message: так их видят все пути истории (fast-path, «да» на
    # pending, сценарии, основной поток) без правок в каждом.

    # describe/describe_done режут введённый текст до 40 символов
    _CC_HIST_HEAD = 40

    def _cc_hist_install(self) -> None:
        mem = getattr(self, "memory", None)
        orig = getattr(mem, "add_message", None)
        if orig is None or getattr(orig, "_cc_hist_wrapped", False):
            return

        def add_message(role, content, user_id="default", chat_id=None,
                        *args, **kwargs):
            try:
                content = self._cc_hist_redact(role, content, user_id, chat_id)
            except Exception as e:
                logger.warning(f"[CompControl] маска истории не применилась: {e}")
            return orig(role, content, user_id, chat_id, *args, **kwargs)

        add_message._cc_hist_wrapped = True
        mem.add_message = add_message
        self._cc_hist_hook_cc()

    def _cc_hist_hook_cc(self) -> None:
        # Каждый исполненный ввод менеджера (сценарий, маркер, «да», агент)
        # — в маску истории хода: ComputerControlManager._audit зовёт on_typed
        cc = getattr(self, "computer_control", None)
        if cc is not None and getattr(cc, "on_typed", None) is None:
            try:
                cc.on_typed = self._cc_hist_note_typed
            except Exception:
                pass

    def _cc_sc_note_answer(self, chat_id, text) -> None:
        """Ответ на вопрос-слот идущего сценария: слот-секрет (вопрос о
        пароле/коде/телефоне или слот «секретN» из _slotify_secrets) — маской
        хода и в KnownSecrets чата, до записи реплики в STM."""
        self._cc_hist_hook_cc()
        sm = getattr(self, "scenario_manager", None)
        if sm is None or not str(text or "").strip():
            return
        try:
            with sm._lock:
                run = (getattr(sm, "_runs", None) or {}).get(str(chat_id))
                step = dict((run or {}).get("awaiting") or {})
        except Exception:
            return
        if not step:
            return
        question = str(step.get("question") or "")
        if re.match(r"(?:секрет|secret)", str(step.get("slot") or ""),
                    re.IGNORECASE):
            question += " [секрет]"
        try:
            self._cc_hist_note_user_text(text, question)
        except Exception as e:
            logger.warning(f"[Scenarios] маска ответа слота не поставлена: {e}")

    @staticmethod
    def _cc_hist_frames() -> list:
        # Кадры ходов текущего контекста: регистрация идёт из мест, где
        # ключ STM неизвестен (_cc_reply), — ход контекста один, его кадр
        from app.core import turn_gate
        return list((turn_gate._FRAMES.get() or {}).values())

    def _cc_hist_add_mask(self, kind: str, needle: str,
                          repl: Optional[str] = None) -> None:
        if not needle:
            return
        for frame in self._cc_hist_frames():
            masks = frame.setdefault("cc_hist_masks", [])
            if not any(m[0] == kind and m[1] == needle for m in masks):
                masks.append((kind, needle, repl))
            if kind == "secret":
                self._cc_hist_remember(frame.get("key"), needle)

    def _cc_hist_vault(self):
        # Известные секреты чата (cc_privacy.KnownSecrets): маски хода живут
        # один ход, а пароль из ответа агент вводит ходом позже. Лениво —
        # тестовые заготовки бота собираются без __init__
        vault = self.__dict__.get("_cc_known_secrets")
        if vault is None:
            from app.features.cc_privacy import KnownSecrets
            vault = self.__dict__.setdefault("_cc_known_secrets",
                                             KnownSecrets())
        return vault

    def _cc_hist_remember(self, chat_key, value) -> None:
        # «да»/«нет»/«отмена» на вопрос о пароле — не секрет: маской на
        # полчаса ложилось бы каждое следующее «да» в чате
        from app.features.computer_control import classify_confirmation
        s = str(value or "").strip()
        if (chat_key is None or not s or TaskAgent.parse_cancel(s)
                or classify_confirmation(s) != "UNKNOWN"):
            return
        self._cc_hist_vault().add(chat_key, s)

    def _cc_hist_note_typed(self, action) -> None:
        """Ввод в чувствительное поле (флаг снапшота, подпись «пароль/код/
        карта…» или секретоподобное значение) — текст ввода в историю
        маской. multi — по вложенным действиям."""
        from app.features.cc_privacy import typed_is_sensitive
        if not isinstance(action, dict):
            return
        items = (action.get("items") if action.get("kind") == "multi"
                 else [action])
        for a in items or ():
            if not isinstance(a, dict) or a.get("kind") != "type":
                continue
            text = str(a.get("text") or "").strip()
            if text and typed_is_sensitive(text, a.get("element"),
                                           bool(a.get("field_sensitive"))):
                self._cc_hist_add_mask("secret", text)

    # Кандидаты в телефон/карту с пробелами/скобками/дефисами — целиком
    # проверяет looks_secret
    _CC_HIST_DIGITS_RE = re.compile(r"\+?\d[\d ()-]{8,}\d")

    def _cc_hist_note_user_text(self, text, question: Optional[str] = None,
                                contacts: bool = True,
                                answer_secret: bool = False) -> None:
        """Секреты в реплике человека — маской ДО её записи в STM. Реплика
        пишется раньше, чем становится известно, что агент задачи/маркер
        LLM введёт её в поле (а пост-правка STM не догонит батч
        LTM-экстракции, который собирается в момент записи), поэтому
        секрет угадывается по самому тексту: значение после «пароль/код/
        PIN…» (command_secret_values), email/телефон/карта/токен целиком
        или словом (looks_secret), а ответ на вопрос агента о пароле/коде/
        карте/почте/телефоне/логине (question) — весь.
        contacts=False — обычная реплика (путь маркеров): email/телефон в
        разговоре не секрет и маской не ложатся; если маркер введёт их в
        поле, реплику хода перепишет _cc_hist_after_markers."""
        from app.features.cc_privacy import (is_sensitive_label, looks_contact,
                                             looks_secret)
        s = str(text or "").strip()
        if not s:
            return

        def _secret(v: str) -> bool:
            return looks_secret(v) and (contacts or not looks_contact(v))

        if answer_secret or (question and is_sensitive_label(question)
                             and not TaskAgent.parse_cancel(s)) or _secret(s):
            self._cc_hist_add_mask("secret", s)
        values = set(command_secret_values(s))
        for raw in re.findall(r"[^\s«»\"'“”„`,;]+", s):
            tok = raw.strip(".!?…:()")
            if tok and (tok in values or _secret(tok)):
                # И слово с краевой пунктуацией: «Kotik2019!» — пароль с «!»
                # или «Kotik2019» в конце фразы, заранее не понять
                self._cc_hist_add_mask("secret", tok)
                if raw != tok:
                    self._cc_hist_add_mask("secret", raw)
        for v in values:
            self._cc_hist_add_mask("secret", v)
        for m in self._CC_HIST_DIGITS_RE.findall(s):
            if _secret(m.strip()):
                self._cc_hist_add_mask("secret", m.strip())

    def stm_add_message(self, role: str, content, user_id, chat_id) -> None:
        """Запись прямо в STM (без LTM — правка хвоста хода, например
        картинки на сервере) через те же маски хода, что и memory.add_message."""
        try:
            content = self._cc_hist_redact(role, content, user_id, chat_id)
        except Exception as e:
            logger.warning(f"[CompControl] маска истории не применилась: {e}")
        self.memory.stm.add_message(role, content, user_id, chat_id)

    def _cc_hist_rewrite_tail(self, chat_id, user_id) -> int:
        """Записи ЭТОГО хода в STM, легшие до маски, — заново через маски
        хода: хвост снимается (буфер и Chroma, pop_last_n) и пишется снова.
        Батч LTM-экстракции, собранный в момент записи, не догнать — поэтому
        это запасной путь, а основной — маска до записи. → сколько записей
        изменилось."""
        key = self.stm_key(chat_id, user_id)
        tail = self._turn_stm_tail(key)
        if not tail:
            return 0
        new = [self._cc_hist_redact(m.get("role"), m.get("content"), user_id,
                                    chat_id) for m in tail]
        if all(c == m.get("content") for m, c in zip(tail, new)):
            return 0
        stm = self.memory.stm
        stm.pop_last_n(len(tail), key)
        for m, c in zip(tail, new):
            stm.add_message(m.get("role"), c, m.get("sender_id") or user_id,
                            chat_id, m.get("user_name"))
        return sum(c != m.get("content") for m, c in zip(tail, new))

    def _cc_hist_after_markers(self, chat_id, user_id) -> None:
        """Маркер LLM поставил ввод (pending): его значение известно только
        после ответа модели, а реплика человека уже в STM. Ввод в поле
        логина/почты/пароля или секретоподобное значение → маска хода и
        правка уже записанной реплики этого хода."""
        cc = getattr(self, "computer_control", None)
        if cc is None or chat_id is None:
            return
        with cc._lock:
            entry = (cc._pending or {}).get(str(chat_id))
        if not entry:
            return
        self._cc_hist_note_typed(entry.get("action"))
        n = self._cc_hist_rewrite_tail(chat_id, user_id)
        if n:
            logger.info(f"[CompControl] реплика хода переписана маской "
                        f"(ввод маркера): {n} зап.")

    def _cc_private_host(self, *urls) -> Optional[str]:
        """Хост приватной страницы, если хоть один адрес приватный (любой —
        консервативно), иначе None."""
        cc = getattr(self, "computer_control", None)
        check = getattr(cc, "is_private_page", None)
        if not callable(check):
            return None
        for u in urls:
            if u and check(str(u)):
                from urllib.parse import urlsplit
                s = str(u)
                return (urlsplit(s).hostname if "://" in s
                        else s.split("/")[0]) or s
        return None

    def _cc_hist_note_private(self, text: Optional[str], host: str,
                              what: str = "read") -> None:
        # Текст приватной страницы в ответе → в историю только заглушка
        text = (text or "").strip()
        if not text:
            return
        if what == "read":
            repl = f"[прочитано {len(text)} символов с приватной страницы {host}]"
        else:
            repl = (f"[обзор приватной страницы {host}: {len(text)} символов "
                    f"— в историю не сохранён]")
        self._cc_hist_add_mask("private", text, repl)

    def _cc_hist_note_read(self, action, detail) -> None:
        # Прочитанный текст приватной страницы: адрес действия, хост и
        # текущий адрес браузера, если это та же вкладка (путь /login и т.п.)
        if not isinstance(action, dict) or not detail:
            return
        host = str(action.get("host") or "")
        last = getattr(getattr(self, "computer_control", None), "_last_url", None)
        if last and host and self._cc_private_host(host) is None:
            from urllib.parse import urlsplit
            try:
                lh = (urlsplit(str(last)).hostname or "").lower()
            except Exception:
                lh = ""
            if lh != host.lower().split(":")[0]:
                last = None
        priv = self._cc_private_host(action.get("value"), action.get("url"),
                                     host, last)
        if priv:
            self._cc_hist_note_private(detail, host or priv, "read")

    def _cc_hist_redact(self, role: str, content, user_id, chat_id):
        """Маски кадра хода и известные секреты чата → текст записи истории.
        Вне хода (фоновые сообщения) — только известные секреты."""
        if not isinstance(content, str) or not content:
            return content
        key = self.stm_key(chat_id, user_id)
        known = self._cc_hist_vault().values(key)
        frame = self._get_turn_gate().current_frame(key)
        if frame is None:
            for needle in sorted(known, key=len, reverse=True):
                content = self._cc_mask_secret(content, needle)
            return content
        # Ввод, отложенный этим ходом до «да» (_cc_run_steps → set_pending):
        # запись истории идёт после set_pending — маска ставится здесь
        cc = getattr(self, "computer_control", None)
        if cc is not None and chat_id is not None:
            try:
                with cc._lock:
                    entry = (cc._pending or {}).get(str(chat_id))
                if entry:
                    self._cc_hist_note_typed(entry.get("action"))
            except Exception:
                pass
        masks = frame.get("cc_hist_masks") or []
        if not masks and not known:
            return content
        # Сначала крупные куски приватного текста, затем секреты
        for kind, needle, repl in masks:
            if kind == "private" and role == "assistant" and needle in content:
                content = content.replace(needle, repl)
        # Длинные секреты первыми: «Kotik2019» из текста реплики не должен
        # надкусить «Kotik2019!» ввода и оставить хвост «!»
        secrets = sorted({m[1] for m in masks if m[0] == "secret"}
                         | set(known), key=len, reverse=True)
        for needle in secrets:
            content = self._cc_mask_secret(content, needle)
        return content

    @classmethod
    def _cc_mask_secret(cls, content: str, secret: str) -> str:
        """Все вхождения секрета — маской ***(N), без учёта регистра.
        Длинный секрет ищется по голове (describe режет до 40 символов) и
        съедается вместе с совпадающим хвостом; короткий — только отдельным
        словом («1234» внутри «12345» не трогаем)."""
        from app.features.cc_privacy import mask
        head = secret[:cls._CC_HIST_HEAD]
        low, hl, sl = content.lower(), head.lower(), secret.lower()
        if len(low) != len(content) or len(sl) != len(secret):
            low, hl, sl = content, head, secret  # lower() сменил длину
        out, i, pos = [], 0, 0
        while True:
            j = low.find(hl, pos)
            if j < 0:
                break
            end = j + len(hl)
            while (end - j < len(sl) and end < len(low)
                   and low[end] == sl[end - j]):
                end += 1
            if len(secret) < cls._CC_HIST_HEAD and (
                    (j and content[j - 1].isalnum())
                    or (end < len(content) and content[end].isalnum())):
                pos = j + 1
                continue
            out.append(content[i:j])
            out.append(mask(secret))
            i = pos = end
        out.append(content[i:])
        return "".join(out)

    # Main processing

    def control_mode_on(self, chat_id) -> bool:
        # Включён ли режим управления (computer control) для чата.
        # Простой дольше idle_exit_min гасит режим здесь же (лениво, при
        # первом обращении), уведомление — со следующим сообщением
        key = str(chat_id)
        if key not in self._control_mode:
            return False
        if getattr(self, "computer_control", None) is None:
            # computer_control выключен (конфиг/веб-настройки): режим без
            # менеджера только глушил бы напоминания/дела — гасим молча
            self._cc_mode_drop_all("computer_control выключен")
            return False
        ts = getattr(self, "_control_mode_ts", {}).get(key)
        idle_min = self._cc_idle_exit_min()
        if ts and idle_min > 0 and time.time() - ts > idle_min * 60:
            logger.info(f"[BotInstance] режим управления OFF по простою "
                        f"(chat {key}, {idle_min} мин)")
            self._control_mode.discard(key)
            self._control_mode_off_cleanup(key)
            self.__dict__.setdefault("_cc_idle_notice", {})[key] = idle_min
            self._cc_mode_save()
            return False
        return True

    def _cc_idle_exit_min(self) -> int:
        # features.computer_control.idle_exit_min (0 — без автовыхода);
        # читается на каждом обращении — веб-настройки меняют его на живую
        cfg = (getattr(self, "features", None) or {}).get("computer_control")
        try:
            return int(cfg.get("idle_exit_min", 30)) if isinstance(cfg, dict) else 30
        except (TypeError, ValueError):
            return 30

    def _cc_mode_touch(self, chat_id) -> None:
        """Активность в режиме — отодвигает автовыход по простою. На диск —
        не чаще раза в минуту: ошибка в минуту после рестарта не важна."""
        key = str(chat_id)
        if key not in self._control_mode:
            return
        if getattr(self, "computer_control", None) is None:
            self._cc_mode_drop_all("computer_control выключен")
            return
        tsd = self.__dict__.setdefault("_control_mode_ts", {})
        prev = tsd.get(key) or 0
        now = time.time()
        tsd[key] = now
        # Режим восстановлен из файла после рестарта — браузерный слой о нём
        # ещё не знает (пул V не прогрет): сообщаем при первой активности
        cold = self.__dict__.get("_cc_mode_cold", set())
        if key in cold:
            cold.discard(key)
            try:
                from app.features import browser_actions as _ba
                _ba.set_control_mode(key, True)
            except Exception:
                pass
        if now - prev > 60:
            self._cc_mode_save()

    def _cc_mode_drop_all(self, why: str) -> None:
        """Снять режим управления во всех чатах и очистить файл режима
        (computer_control выключен — восстанавливать нечего)."""
        chats = list(getattr(self, "_control_mode", ()) or ())
        if not chats:
            return
        logger.info(f"[BotInstance] режим управления OFF в {len(chats)} "
                    f"чат(ах): {why}")
        for key in chats:
            self._control_mode.discard(key)
            try:
                self._control_mode_off_cleanup(key)
            except Exception as e:
                logger.debug(f"[BotInstance] чистка режима {key}: {e}")
        self._cc_mode_save()

    def _cc_mode_load(self) -> None:
        path = getattr(self, "_control_mode_path", None)
        if path is None:
            return
        data = load_json_safe(path, {}, label="ControlMode")
        if not isinstance(data, dict):
            return
        if getattr(self, "computer_control", None) is None:
            # Режим сохранён, а computer_control с тех пор выключен: не
            # восстанавливаем и чистим файл
            if data:
                logger.info(f"  [{self.persona_name}] режим управления не "
                            f"восстановлен: computer_control выключен")
                self._cc_mode_save()
            return
        for k, v in data.items():
            try:
                self._control_mode_ts[str(k)] = float(v)
                self._control_mode.add(str(k))
                self.__dict__.setdefault("_cc_mode_cold", set()).add(str(k))
            except (TypeError, ValueError):
                continue
        if self._control_mode:
            logger.info(f"  [{self.persona_name}] режим управления восстановлен "
                        f"для {len(self._control_mode)} чат(ов)")

    def _cc_mode_save(self) -> None:
        # Заготовки без __init__ (тесты) пути не имеют — на диск не пишем
        path = getattr(self, "_control_mode_path", None)
        if path is None:
            return
        tsd = getattr(self, "_control_mode_ts", {})
        data = {k: tsd.get(k) or time.time() for k in sorted(self._control_mode)}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(path, data)
        except Exception as e:
            logger.warning(f"[BotInstance] режим управления не сохранился: {e}")

    def _cc_pop_idle_notice(self, chat_id, lang=None) -> Optional[str]:
        # Одноразовое «режим выключен после простоя» для следующего сообщения
        mins = self.__dict__.get("_cc_idle_notice", {}).pop(str(chat_id), None)
        if mins is None:
            return None
        from app.features import cc_texts
        return cc_texts.t("cc_mode_idle_off", lang, minutes=mins)

    @staticmethod
    def _cc_mode_feature_note(user_input: str) -> Optional[str]:
        """Просьба напомнить/записать дело/положить в инвентарь в режиме
        управления: сами фичи на время режима выключены — пометка модели,
        чтобы ответ честно сказал «не сохранено», а не «напомню»."""
        t = str(user_input or "")
        low = t.lower()
        try:
            asked = ("напом" in low or re.search(r"\bremind", low)
                     or is_todo_request(t) or is_todo_done_request(t)
                     or is_todo_list_request(t)
                     or is_inventory_add_request(t)
                     or is_inventory_remove_request(t))
        except Exception:
            asked = "напом" in low
        if not asked:
            return None
        return ("Control mode is on in this chat, so reminders, the todo list "
                "and the inventory are paused. The user's request was NOT "
                "saved and nothing was scheduled. Say so honestly and briefly, "
                "in your own style: to use it, the user should first exit "
                "control mode (\"выйди из режима управления\" / \"exit control "
                "mode\") and ask again. Do NOT say that anything was scheduled, "
                "added or remembered.")

    def _cc_phrase(self, key: str, lang=None, **values) -> str:
        """Служебная реплика режима управления: flavor-банк (голос персоны)
        только если язык пользователя совпадает с языком банка, иначе —
        шаблон cc_texts на языке пользователя."""
        from app.features import cc_texts, flavor_text
        template = cc_texts.t(key, lang, **values)
        try:
            from app.core.language import persona_language
            sp = getattr(getattr(self, "persona", None), "system_prompt", "") or ""
            bank_lang = persona_language(sp)
        except Exception:
            bank_lang = None
        if lang and bank_lang and lang != bank_lang:
            return template
        return flavor_text.phrase(getattr(self, "context", "default"), key,
                                  template, **values)

    def _control_mode_switch(self, chat_id: str, turn_on: bool,
                             lang: Optional[str] = None) -> str:
        """Реплика на «перейди в режим управления»/«выйди из режима
        управления» + побочки переключения (чистка подвисших CC-состояний).
        Реплики — голосом персоны из flavor-банка (секция phrases), при
        пустом банке или другом языке пользователя — шаблоны cc_texts."""
        if turn_on:
            if not self.computer_control:
                return self._cc_phrase("cc_mode_disabled", lang)
            if chat_id in self._control_mode:
                return self._cc_phrase("cc_mode_already_on", lang)
            self._control_mode.add(chat_id)
            self.__dict__.setdefault("_control_mode_ts", {})[chat_id] = time.time()
            self.__dict__.get("_cc_mode_cold", set()).discard(chat_id)
            self._cc_mode_save()
            logger.info(f"[BotInstance] режим управления ON (chat {chat_id})")
            # Пул V (headed Chrome): поднимаем заранее и показываем окно —
            # пользователь ждёт готовый браузер (web_extended)
            try:
                from app.features import browser_actions as _ba
                _ba.set_control_mode(chat_id, True)
            except Exception:
                pass
            return self._cc_phrase("cc_mode_on", lang)
        if chat_id not in self._control_mode:
            return self._cc_phrase("cc_mode_already_off", lang)
        self._control_mode.discard(chat_id)
        self._cc_mode_save()
        logger.info(f"[BotInstance] режим управления OFF (chat {chat_id})")
        self._control_mode_off_cleanup(chat_id)
        return self._cc_phrase("cc_mode_off", lang)

    def _control_mode_off_cleanup(self, chat_id: str) -> None:
        """Побочки выхода из режима (явного и по простою): браузерный слой,
        подвисшие pending/сценарий/задача агента."""
        getattr(self, "_control_mode_ts", {}).pop(chat_id, None)
        self.__dict__.get("_cc_mode_cold", set()).discard(chat_id)
        # Пул V больше этому чату не нужен: догорит по idle-таймеру
        try:
            from app.features import browser_actions as _ba
            _ba.set_control_mode(chat_id, False)
        except Exception:
            pass
        # Подвисшие CC-состояния чата недействительны вне режима
        try:
            if self.computer_control:
                self.computer_control.clear_pending(chat_id)
        except Exception:
            pass
        try:
            if self.scenario_manager:
                if self.scenario_manager.active(chat_id):
                    self.scenario_manager.cancel(chat_id)
                if self.scenario_manager.recording(chat_id):
                    self.scenario_manager.record_stop(chat_id)
        except Exception:
            pass
        try:
            if self.task_agent:
                self.task_agent.cancel(chat_id)
        except Exception:
            pass

    # ── Ход до лока чата: «стоп», дубли, занятый агент ──────────

    def cc_answer_options(self, chat_id, user_id=None) -> Optional[dict]:
        """Кнопки ответа для веба: варианты вопроса, которого сейчас ждёт
        режим управления в чате (см. TaskAgent.answer_options и
        ComputerControlManager.pending_answer_options). Порядок — как в
        process_message: живой прогон агента важнее подтверждения команды;
        прогон без вариантов (свободный ответ) кнопок не даёт. Кнопка шлёт
        обычную реплику, поэтому здесь ничего не меняется — только чтение."""
        key = str(chat_id or user_id or "")
        cc = getattr(self, "computer_control", None)
        if not key or cc is None:
            return None
        try:
            if not self.control_mode_on(key):
                return None
            ta = self.task_agent
            if ta is not None and ta.has_run(key):
                return ta.answer_options(key)
            return cc.pending_answer_options(key, user_id)
        except Exception as e:
            logger.debug(f"[BotInstance] варианты ответа не собрались: {e}")
            return None

    def cc_tracked_page(self, chat_id) -> Optional[dict]:
        # Отслеживаемая вкладка чата — для трансляции в веб (только чтение)
        cc = getattr(self, "computer_control", None)
        if cc is None:
            return None
        try:
            return cc.tracked_page(chat_id)
        except Exception:
            return None

    def cc_view_describe(self, url: str) -> dict:
        # Адрес для трансляции: без токенов и с признаком приватной страницы;
        # без менеджера — пустой адрес и «приватная» (консервативно)
        cc = getattr(self, "computer_control", None)
        if cc is None:
            return {"url": "", "private": True}
        return cc.view_describe(url)

    def cc_task_card(self, chat_id, user_id=None) -> Optional[dict]:
        """Карточка задачи агента для веба (TaskAgent.task_card): цель,
        статус, план заказа и журнал хода. Только чтение."""
        key = str(chat_id or user_id or "")
        ta = getattr(self, "task_agent", None)
        if not key or ta is None:
            return None
        try:
            return ta.task_card(key)
        except Exception as e:
            logger.debug(f"[BotInstance] карточка задачи не собралась: {e}")
            return None

    def cc_turn_enter(self, text: str, user_id, chat_id):
        """Вызывается платформой ДО ожидания лока чата (Telegram/веб).
        → (reply, token): reply не None — ответить им сразу и сообщение
        дальше не обрабатывать; token — отдать в cc_turn_exit по окончании
        хода (None — регистрировать нечего).
        Только в режиме управления и только для авторизованного: иначе
        сообщение идёт обычным путём, как раньше.
        - «стоп/отмена/хватит» при идущем ходе этого чата — флаг остановки
          агенту и циклам исполнения, ответ «Останавливаю…» сразу, а не
          после 4-минутного прогона;
        - реплика при занятом агенте — «ещё работаю», а не очередь (иначе
          она ушла бы ответом на вопрос, которого человек ещё не видел);
        - та же команда, пока прежняя ещё идёт, — «уже выполняю»."""
        key = str(chat_id or user_id or "")
        raw = str(text or "")
        try:
            if not (key and getattr(self, "computer_control", None)
                    and self._cc_allowed(user_id, chat_id)
                    and self.control_mode_on(key)):
                return None, None
            try:
                clean = self.strip_trigger(raw) or raw
            except Exception:
                clean = raw
            lang = detect_language(clean)
            norm = " ".join(normalize_command(
                clean, self._address_names()).lower().split())
        except Exception as e:
            logger.debug(f"[BotInstance] cc_turn_enter: {e}")
            return None, None
        from app.features import cc_texts
        ta = getattr(self, "task_agent", None)
        # Ключ прогона агента — как у хода: веб без chat_id — user_id
        ta_chat = chat_id or user_id
        cc = self.computer_control
        with self.__dict__.setdefault("_cc_inflight_lock", threading.Lock()):
            inflight = self.__dict__.setdefault("_cc_inflight", {})
            running = inflight.get(key) or {}
            # Страховка от утечки токена (исключение до cc_turn_exit):
            # запись старше 15 мин — не «идущий ход»
            for _n, (_c, _ts) in list(running.items()):
                if time.time() - _ts > 900:
                    running.pop(_n, None)
            ta_busy = bool(ta and ta.busy(ta_chat))
            # «Коннор, стоп» в вебе: имя strip_trigger не снимает
            is_stop = bool(_CC_STOP_RE.match(clean) or _CC_STOP_RE.match(norm))
            # Прогон агента ждёт ответа (вопрос, «да/нет») — «стоп» его
            # автора снимает его сразу, до лока хода: иначе «да», уже
            # ждущее лока, исполнило бы подтверждённый шаг и повело прогон
            # дальше. Чужой «стоп» ждущий прогон не трогает (ответит feed:
            # «задачей управляет …»); идущий (busy) останавливает любой
            # допущенный — остановка в сторону безопасности
            ta_mine = False
            if ta and is_stop and not ta_busy:
                try:
                    own = ta.owner(ta_chat)
                    ta_mine = ta.active(ta_chat) and (
                        own is None or user_id is None or own == str(user_id))
                    aw_q = getattr(ta, "awaiting_kind", lambda c: None)(ta_chat)
                    if ta_mine and aw_q in ("ask", "confirm", "switch") \
                            and (SOFT_STOP_RE.match(clean)
                                 or SOFT_STOP_RE.match(norm)):
                        # «не надо»/«хватит» на вопрос или «да/нет» агента —
                        # это «нет» на него (разберёт feed), а не отмена
                        # задачи; идущий ход (running) по-прежнему стоп
                        ta_mine = False
                except Exception as e:
                    logger.debug(f"[BotInstance] владелец задачи: {e}")
            # Идущий прогон сценария этого чата (шаги исполняются сейчас):
            # «стоп» ставит ему флаг — цикл шагов проверяет его перед каждым
            sm = getattr(self, "scenario_manager", None)
            _sm_stop = getattr(sm, "request_stop", None)
            if is_stop and (running or ta_busy or ta_mine):
                ta_reply = None
                if ta_busy or ta_mine:
                    ta_reply = ta.cancel(ta_chat)
                if callable(_sm_stop):
                    _sm_stop(chat_id)
                cc.request_stop(key)
                # Листание — только этого чата: «стоп» другого чата чужую
                # страницу не гасит
                cc.stop_scroll_if_active(chat_id=key)
                logger.info(f"[BotInstance] «стоп» до лока хода (chat {key})")
                if ta_mine and not running and ta_reply:
                    # Прогон ждал ответа — снят сразу, останавливать нечего
                    return ta_reply, None
                return cc_texts.t("stopping", lang), None
            if parse_control_mode(clean) is False and (running or ta_busy):
                # Выход из режима при идущем ходе: остановить сразу, само
                # переключение — своим ходом после текущего
                if ta_busy:
                    ta.cancel(ta_chat)
                if callable(_sm_stop):
                    _sm_stop(chat_id)
                cc.request_stop(key)
            elif ta_busy:
                return self._cc_phrase("task_busy", lang), None
            elif norm and norm in running \
                    and self._cc_is_dup(norm, running[norm][1]):
                logger.info(f"[BotInstance] дубль команды «{norm[:40]}» "
                            f"пока идёт прежняя (chat {key}) — пропуск")
                return cc_texts.t("already_running", lang), None
            running = inflight.setdefault(key, {})
            running[norm] = ((running.get(norm) or (0, 0))[0] + 1, time.time())
            return None, (key, norm)

    @staticmethod
    def _cc_dup_kind(norm: str) -> str:
        """Вид команды для отсева дублей: "step" — шаговая/идемпотентная
        (медиа, клавиши, листание, «далее») — повтор никогда не дубль;
        "heavy" — открытие/навигация/задача/ввод/отправка — повтор, пока
        идёт прежняя, дубль; "other" — дубль только в _CC_DUP_WINDOW_SEC."""
        try:
            if (_CC_STEP_CMD_RE.match(norm) or parse_media_request(norm)
                    or parse_key_request(norm) or parse_scroll_request(norm)
                    or parse_erase_request(norm) or parse_zoom_request(norm)
                    is not None or next_video_recipe(norm)):
                return "step"
            if (parse_type_request(norm) or parse_send_request(norm)
                    or parse_open_with_url(norm) or parse_search_on_site(norm)
                    or parse_task_request(norm) or parse_open_many(norm)):
                return "heavy"
        except Exception as e:
            logger.debug(f"[BotInstance] вид команды для дублей: {e}")
        return "other"

    def _cc_is_dup(self, norm: str, last_ts: float) -> bool:
        kind = self._cc_dup_kind(norm)
        if kind == "step":
            return False
        if kind == "heavy":
            return True
        return time.time() - float(last_ts or 0) < _CC_DUP_WINDOW_SEC

    def cc_turn_exit(self, token) -> None:
        if not token:
            return
        key, norm = token
        with self.__dict__.setdefault("_cc_inflight_lock", threading.Lock()):
            running = self.__dict__.setdefault("_cc_inflight", {}).get(key)
            if not running:
                return
            cnt, ts = running.get(norm) or (0, 0)
            if cnt > 1:
                running[norm] = (cnt - 1, ts)
            else:
                running.pop(norm, None)
            if not running:
                self._cc_inflight.pop(key, None)

    def cc_pending_stamp(self, chat_id):
        """Отметка живого подтверждения чата (pending режима управления или
        вопрос агента) — платформа сравнивает до/после хода: новый вопрос
        «да/нет» в группе требует reply на сообщение бота (см. group_reply_hint)."""
        cc = getattr(self, "computer_control", None)
        if cc is None:
            return None
        stamp = None
        try:
            with cc._lock:
                entry = cc._pending.get(str(chat_id))
                if entry:
                    stamp = ("cc", entry.get("expires_at"))
        except Exception:
            pass
        ta = getattr(self, "task_agent", None)
        if stamp is None and ta is not None:
            try:
                with ta._lock:
                    run = ta._runs.get(str(chat_id))
                    aw = run.get("awaiting") if run else None
                    if aw and aw.get("kind") in ("confirm", "continue",
                                                 "ask", "switch"):
                        # Вопрос агента тоже ждёт ответа именно автора: в
                        # группе без reply ответ отбрасывался платформой
                        stamp = ("ta", id(aw))
            except Exception:
                pass
        return stamp

    def cc_group_confirm_hint(self, text: str) -> str:
        from app.features import cc_texts
        return cc_texts.t("group_reply_hint", detect_language(text or ""))

    def rebind_computer_control(self) -> None:
        """После живой смены computer_control в веб-настройках: надстройки
        (сценарии, агент, flavor-банк) — на текущий менеджер. Без этого они
        держали старый экземпляр, а включённые после старта — не появлялись
        до рестарта."""
        cc = self.computer_control
        if cc is None:
            # Режим управления без менеджера бессмыслен: гасим во всех чатах
            # (и в файле — иначе вернулся бы после рестарта)
            try:
                self._cc_mode_drop_all("computer_control выключен в настройках")
            except Exception as e:
                logger.debug(f"[BotInstance] снятие режима управления: {e}")
            for attr in ("scenario_manager", "task_agent"):
                mgr = getattr(self, attr, None)
                if mgr is None:
                    continue
                try:
                    for chat in list(getattr(mgr, "_runs", {}) or {}):
                        mgr.cancel(chat)
                except Exception:
                    pass
                setattr(self, attr, None)
            return
        if self.features.get("scenarios", True):
            if self.scenario_manager is None:
                self.scenario_manager = ScenarioManager(
                    context=self.context, computer_control=cc)
                logger.info(f"[{self.persona_name}] Scenario manager включён на живую")
            else:
                self.scenario_manager.cc = cc
        if self.features.get("task_agent", True):
            if self.task_agent is None:
                self.task_agent = TaskAgent(computer_control=cc,
                                            context=self.context)
                logger.info(f"[{self.persona_name}] Task agent включён на живую")
            else:
                self.task_agent.cc = cc
        try:
            from app.features import flavor_text
            flavor_text.ensure_flavor_bank(self)
        except Exception as e:
            logger.debug(f"[{self.persona_name}] flavor-банк не запущен: {e}")

    def _ta_foreign_command(self, text: str, ta_chat) -> bool:
        """Реплика при ждущем прогоне агента — явная посторонняя команда, а
        не ответ ему: новая «задача: …», «открой <сайт/приложение из
        конфига>», медиа-команда («громче», «пауза»). «открой меню»,
        «пепперони», «да» — ответы: алиаса «меню» в конфиге нет."""
        try:
            names = self._address_names()
            if parse_task_request(text, names):
                return True
            clean = normalize_command(text, names)
            if classify_confirmation(text, names) != "UNKNOWN" \
                    or re.match(r"^\s*(?:продолж\w*|дальше|давай\s+дальше|"
                                r"continue|go\s+on|resume)[\s.!…]*$", clean,
                                re.IGNORECASE):
                return False  # «да/нет», «продолжай» — ответ агенту
            media = parse_media_request(clean)
            if media and media[2] not in ("toggle", "play", "pause"):
                # Громкость/перемотка — посторонняя команда; пауза/«продолжи»
                # — нет (Space ушёл бы в кнопку в фокусе на оформлении)
                return True
            word = parse_open_request(clean)
            if word and self.computer_control is not None:
                a = self.computer_control.resolve(word, web_search=False)
                return bool(a) and a.get("kind") in ("url", "app", "task")
        except Exception as e:
            logger.debug(f"[TaskAgent] разбор посторонней команды: {e}")
        return False

    def _task_agent_turn(self, user_input: str, user_id, chat_id, user_name,
                         turn_lang, goal: Optional[str] = None,
                         feed_text: Optional[str] = None,
                         announce: bool = True) -> str:
        """Ход агента-автопилота: goal — запуск новой задачи, None — реплика
        в живой прогон (ответ на вопрос, «да/нет», отмена).
        Реплика пользователя пишется в STM ДО прогона: метка времени STM —
        момент записи, а промежуточные сообщения хода (notify) уходят во
        время прогона. Запись в конце ставила вопрос пользователя в ленте
        веба (сортировка по времени) после шагов, которые он вызвал.
        Поэтому маски секретов ставятся до записи: по тексту реплики (ответ
        на вопрос агента о пароле/карте — целиком), а ввод агента в поле —
        хуком on_typed до исполнения (ответ хода пишется после прогона)."""
        # Ключ прогона агента: веб-клиент API без chat_id — user_id (иначе все
        # такие клиенты делили один прогон и одну память «None»)
        ta_chat = chat_id or user_id
        try:
            self.task_agent.on_typed = self._cc_hist_note_typed
            # Известные секреты чата — агенту: поисковый запрос с паролем,
            # данным ответом, уходит только после «да»
            self.task_agent.known_secrets = (
                lambda chat: self._cc_hist_vault().values(chat))
            # Текст приватной страницы в отчёте агента — в историю заглушкой
            self.task_agent.on_private_text = self._cc_hist_note_private
            # Группа (Telegram: отрицательный chat_id) — строки хода с
            # приватной страницы видят все участники: агент шлёт заглушку
            self.task_agent.is_group = (
                lambda chat: str(chat or "").strip().startswith("-"))
            # Разбор ответа по слотам брифа — тем же роутером, что шаги
            self.task_agent.slot_router = self.router
            said = feed_text if feed_text is not None else user_input
            # Ответ на вопрос агента — секрет по тому же признаку, что у
            # {{secretN}} агента, а не только по ключевым словам вопроса
            # («Какие данные для входа?» → «Kotik2019!»)
            ans_secret = (not goal and
                          self.task_agent.answer_is_secret(ta_chat, said))
            self._cc_hist_note_user_text(
                said,
                None if goal else self.task_agent.awaiting_question(ta_chat),
                answer_secret=ans_secret)
            if feed_text is not None and feed_text != user_input:
                self._cc_hist_note_user_text(user_input)
        except Exception as e:
            logger.warning(f"[TaskAgent] маска истории не поставлена: {e}")
        self.memory.add_message("user", user_input, user_id, chat_id, user_name)
        notify = self._cc_notifier(chat_id, user_id)
        from app.features import cc_texts
        try:
            if goal:
                reply = self.task_agent.start(ta_chat, goal, self.router,
                                              notify=notify, lang=turn_lang,
                                              user_id=user_id,
                                              announce=announce)
            elif feed_text is not None and not feed_text.strip():
                # feed_text — только написанное человеком: OCR фото/текст файла
                # не должны ни отвечать на вопрос агента, ни давать «да»
                reply = cc_texts.t("task_text_only", turn_lang)
            else:
                reply = self.task_agent.feed(
                    ta_chat, user_input if feed_text is None else feed_text,
                    self.router, notify=notify, user_id=user_id,
                    names=self._address_names())
        except Exception as e:
            logger.warning(f"[TaskAgent] ход упал: {e}", exc_info=True)
            self.task_agent.cancel(ta_chat)
            reply = cc_texts.t("task_turn_crashed", turn_lang,
                               err=str(e)[:80])
        if reply is None:
            # Прогон снят между проверкой и feed (TTL) — реплика уже в STM,
            # в обычный поток её не отдаём (задвоилась бы)
            reply = cc_texts.t("task_already_closed", turn_lang)
        self.memory.add_message("assistant", reply, user_id, chat_id)
        try:
            if not self.task_agent.active(ta_chat):
                # Прогон закончился (итог уже записан маской) — известные
                # секреты чата больше не нужны
                self._cc_hist_vault().purge(self.stm_key(chat_id, user_id))
        except Exception as e:
            logger.debug(f"[TaskAgent] секреты чата не сброшены: {e}")
        if self.proactive and chat_id:
            self.proactive.record_user_response(chat_id)
        return reply

    def _cc_notifier(self, chat_id, user_id=None):
        """Промежуточные сообщения в чат из потока process_message (ход
        задачи агента) — через sender платформы на его event loop'е (TG-бот
        привязан к loop'у приложения), в ту же тему форума, что и ход.
        Sender+loop берём у фоновых менеджеров, которые их уже держат; ни
        одного живого — None (ход уйдёт строками в итоговый ответ).
        Отправленное — и в STM (через маски хода: приватное — заглушкой),
        иначе в истории остался бы только итог без шагов."""
        import asyncio
        try:
            topic = (self.get_chat_topic(chat_id)
                     if hasattr(self, "get_chat_topic") else None)
        except Exception:
            topic = None
        for mgr in (self.reminder_manager, self.proactive):
            sender = getattr(mgr, "_sender", None)
            task = getattr(mgr, "_task", None)
            if sender is None or task is None or task.done():
                continue
            loop = task.get_loop()

            def _notify(text: str, _s=sender, _l=loop):
                try:
                    kw = {"topic_id": topic} if topic else {}
                    asyncio.run_coroutine_threadsafe(
                        _s.send_message(str(chat_id), text, **kw), _l
                    ).result(timeout=15)
                except Exception as e:
                    logger.debug(f"[TaskAgent] промежуточное сообщение "
                                 f"не ушло: {e}")
                    return
                try:
                    self.memory.add_message("assistant", text, user_id,
                                            chat_id)
                except Exception as e:
                    logger.debug(f"[TaskAgent] строки хода не в STM: {e}")
            return _notify
        return None

    def _get_turn_gate(self) -> ChatTurnGate:
        gate = self.__dict__.get("turn_gate")
        if gate is None:
            with _TURN_GATE_INIT_LOCK:
                gate = self.__dict__.get("turn_gate")
                if gate is None:
                    gate = self.turn_gate = ChatTurnGate()
        return gate

    @staticmethod
    def stm_key(chat_id, user_id) -> str:
        """Ключ чата в STM — то же правило, что у MemoryManager.add_message
        (chat_id, иначе user_id): ход пользователя и правки STM должны
        ключеваться так же, как реально пишется история (пустой chat_id —
        отдельный ключ "", а не user_id)."""
        return str(chat_id if chat_id is not None else user_id)

    def _set_stm_anchor(self, frame: dict):
        """Якорь STM хода — последняя запись до первой записи этого хода:
        по нему _turn_stm_tail отличает записи ЭТОГО хода от чужих
        (_pipeline_failure_reply, правка STM картинки на сервере). Ставится
        при первом синхронном входе (process_message/_generate сервера), а
        не при получении сообщения: пока обработчик ждал лок чата, в STM
        писал предыдущий ход того же чата."""
        if "stm_anchor" in frame:
            return
        try:
            msgs = self.memory.stm.get_messages(chat_id=frame["key"])
            frame["stm_anchor"] = msgs[-1] if msgs else None
        except Exception:
            frame["stm_anchor"] = _NO_ANCHOR

    @contextmanager
    def user_turn(self, chat_key):
        """Ход пользователя в чате chat_key (ключ STM — см. stm_key).

        Пока ход открыт, фоновые сообщения (инициатива, рефлексия, сигнал
        состояния, утро/ночь/погода, напоминания, уроки) в STM этого чата не
        коммитятся — см. ChatTurnGate. Реентерабелен: ход, открытый выше по
        контексту (обработчик Telegram/сервера — contextvars переезжают в
        asyncio.to_thread), подхватывается, новой реплики не заводит."""
        with self._get_turn_gate().user_turn(chat_key) as frame:
            self._set_stm_anchor(frame)
            yield frame

    def begin_user_turn(self, chat_key) -> dict:
        """Открыть ход при ПОЛУЧЕНИИ сообщения (до распознавания фото/файла,
        до модерации) — синхронно, с event loop через asyncio.to_thread или
        user_turn_async. Закрывать end_user_turn в finally."""
        return self._get_turn_gate().begin_turn(chat_key)

    async def begin_user_turn_async(self, chat_key) -> dict:
        # begin_user_turn для event loop: в потоке, отмена ожидания не оставляет открытый ход.
        return await begin_turn_async(self._get_turn_gate(), chat_key)

    def end_user_turn(self, frame: dict):
        """Обработчик закончил (ответ доставлен или ошибка/разрыв). Ход
        закроется, когда выйдет и рабочий поток генерации, если он ещё жив."""
        self._get_turn_gate().release(frame)

    @contextmanager
    def adopt_turn(self, frame: dict):
        """Присоединиться к открытому ходу в этом контексте (рабочий поток
        генерации сервера, куда кадр передан явно)."""
        with self._get_turn_gate().adopt(frame):
            self._set_stm_anchor(frame)
            yield frame

    @asynccontextmanager
    async def user_turn_async(self, chat_key):
        """Ход пользователя для корутины-обработчика (Telegram): открыт от
        получения сообщения до конца ДОСТАВКИ ответа (split-части с паузами,
        скриншоты) — фоновое сообщение не встаёт между частями ответа.
        asyncio.to_thread внутри подхватывает ход через contextvars."""
        gate = self._get_turn_gate()
        frame = await begin_turn_async(gate, chat_key)
        try:
            with gate.adopt(frame):
                yield frame
        finally:
            gate.release(frame)

    def _turn_stm_tail(self, chat_key) -> Optional[List[Dict]]:
        """Записи STM, сделанные ТЕКУЩИМ ходом пользователя (кадр хода из
        текущего контекста; всё после якоря), или None — хода нет / якорь потерян (буфер
        перечитан из БД или вытеснен лимитом). Сравнение по идентичности:
        get_messages отдаёт сами dict'ы буфера."""
        frame = self._get_turn_gate().current_frame(chat_key)
        if not frame or frame.get("stm_anchor", _NO_ANCHOR) is _NO_ANCHOR:
            return None
        anchor = frame["stm_anchor"]
        try:
            msgs = self.memory.stm.get_messages(chat_id=str(chat_key))
        except Exception:
            return None
        if anchor is None:
            return list(msgs)
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i] is anchor:
                return list(msgs[i + 1:])
        return None

    # ── Fast-path режима управления: команда → лесенка парсеров ──

    def _cc_fast_path(self, cc_text: str, user_input: str, user_id, chat_id,
                      user_name, turn_lang) -> Optional[str]:
        """Команда управления из того, что человек НАПИСАЛ (cc_text —
        normalize_command от raw_user_text), а не из составного user_input
        с OCR/файлом: «Нажми Удалить аккаунт» на скриншоте — не команда.
        Составная команда («открой додо и нажми на пепперони фреш») идёт
        шагами (_cc_run_steps). Ответ — строка (история уже записана) или
        None: не команда, сообщение уходит обычным путём."""
        if not cc_text:
            return None
        # Секреты команды («введи Kotik2019 в поле пароль», «ivan / K…») —
        # маской хода и в KnownSecrets ДО лесенки: отказ резолва (нет поля,
        # не разобрал) возвращается раньше маски в её конце, а реплика и
        # причина отказа уходят в STM и flavor-провайдеру
        try:
            self._cc_hist_note_user_text(cc_text, contacts=False)
        except Exception as e:
            logger.warning(f"[CompControl] маска команды не поставлена: {e}")
        try:
            steps = [s for s in split_compound_command(cc_text) if s] or [cc_text]
        except Exception as e:
            logger.debug(f"[CompControl] разбиение составной команды упало: {e}")
            steps = [cc_text]
        goal_out: List[str] = []
        reply = self._cc_run_steps(steps, chat_id, turn_lang,
                                   user_input=user_input, goal_out=goal_out)
        if reply is None and len(steps) > 1:
            # Разбиение ошиблось (первый кусок — не команда): целиком фраза
            # могла быть одной командой («найди чёрный и белый чай»)
            reply = self._cc_run_steps([cc_text], chat_id, turn_lang,
                                       user_input=user_input,
                                       goal_out=goal_out)
        if goal_out:
            # Цель — агенту сразу; строки шагов до неё («Открыл …») — первыми
            ta_reply = self._task_agent_turn(
                user_input, user_id, chat_id, user_name, turn_lang,
                goal=goal_out[0], feed_text=cc_text)
            return "\n".join(x for x in (reply, ta_reply) if x)
        if reply is None:
            return None
        self.memory.add_message("user", user_input, user_id, chat_id, user_name)
        self.memory.add_message("assistant", reply, user_id, chat_id)
        if self.proactive and chat_id:
            self.proactive.record_user_response(chat_id)
        return reply

    def _cc_run_steps(self, steps: List[str], chat_id, turn_lang,
                      user_input: str = "", done: Optional[List[str]] = None,
                      page_site: Optional[str] = None,
                      goal_out: Optional[list] = None):
        """Шаги составной команды — лениво: следующий шаг резолвится только
        после исполнения предыдущего (второй шаг целится в страницу,
        открытую первым). Шаг, требующий подтверждения, уходит в pending
        вместе с хвостом (action["rest_steps"], action["chain_site"]) — после
        «да» цепочку продолжает pending-ветка. Неудачный шаг обрывает
        цепочку. Ответ — сводка по всем шагам одним сообщением; None —
        первый же шаг не команда. done — строки уже исполненных шагов (для
        продолжения после «да»); page_site — сайт, открытый прошлым шагом.
        goal_out — список: шаг оказался целью для агента задач — туда цель
        (с хвостом фразы), ответ — строки шагов до неё; агента запускает
        вызывающий."""
        lines = list(done or [])
        cc = self.computer_control
        for i, step in enumerate(steps):
            _stopped = getattr(cc, "stop_requested", None)
            if i and callable(_stopped) and _stopped(str(chat_id or "")):
                break  # «стоп» пришёл до лока хода (cc_turn_enter)
            res = self._cc_ladder(step, chat_id, turn_lang,
                                  user_input=user_input, page_site=page_site)
            if res is None:
                if not lines:
                    return None
                from app.features import cc_texts
                lines.append(cc_texts.t("chain_not_command", turn_lang,
                                        step=step))
                break
            if "reply" in res:
                lines.append(res["reply"])
                if not res.get("ok", True):
                    break
                continue
            action = res["action"]
            rest = [s for s in steps[i + 1:] if s]
            if is_goal_task(action):
                # Цель от LLM-яруса — агенту сразу, без «Берусь за задачу?»
                # (решение: просмотр сайтов безопасен, необратимое спросит
                # сам агент; первое сообщение — «Беру: …, «стоп» —
                # прервать»). Хвост составной фразы — часть цели: «найди
                # пиццу и закажи её» терял «закажи её»
                from app.features import cc_texts
                if not self.task_agent:
                    lines.append(cc_texts.t("tasks_disabled", turn_lang))
                    break
                goal = " ".join([str(action["goal"])] + rest)[:400]
                if goal_out is not None:
                    goal_out.append(goal)
                    return "\n".join(ln for ln in lines if ln)
                cc.set_pending(chat_id, dict(action, goal=goal))
                lines.append(cc_texts.t("task_goal_confirm", turn_lang,
                                        goal=goal))
                break
            if not res.get("direct") and cc.needs_confirm(action):
                if rest:
                    action["rest_steps"] = rest
                    action["chain_site"] = (self._cc_chain_site(step, action)
                                            or page_site)
                cc.set_pending(chat_id, action)
                lines.append(cc.confirm_question(action, lang=turn_lang))
                if rest:
                    # Человек подтверждает и хвост цепочки — пусть его видит
                    from app.features import cc_texts
                    lines.append(cc_texts.t(
                        "chain_tail", turn_lang,
                        steps=", ".join(f"«{s}»" for s in rest)))
                break
            ok, line = self._cc_execute_reply(action, chat_id, turn_lang)
            if not ok and isinstance(action.get("confirm_required"), dict):
                # Гейт execute: ничего не нажато (маршрут — до рискованного
                # шага) — вопрос вместо «не удалось», хвост цепочки — в pending
                if rest:
                    action["rest_steps"] = rest
                    action["chain_site"] = (self._cc_chain_site(step, action)
                                            or page_site)
                _q = self._cc_gate_ask(action, chat_id, turn_lang)
                if _q:
                    line = _q
                    if rest:
                        from app.features import cc_texts
                        line += "\n" + cc_texts.t(
                            "chain_tail", turn_lang,
                            steps=", ".join(f"«{s}»" for s in rest))
            lines.append(line)
            if not ok:
                break
            page_site = self._cc_chain_site(step, action) or page_site
        return "\n".join(ln for ln in lines if ln)

    def _cc_gate_ask(self, action: dict, chat_id, turn_lang,
                     user_id=None) -> Optional[str]:
        """Отказ гейта подтверждения (execute без токена «да») → pending +
        вопрос с тем, что реально нажмётся; None — отказа не было."""
        cc = self.computer_control
        follow = getattr(cc, "gate_followup", None)
        try:
            got = follow(action, lang=turn_lang) if callable(follow) else None
        except Exception as e:
            logger.debug(f"[CompControl] вопрос после гейта не собран: {e}")
            got = None
        if not got:
            return None
        pend, question = got
        cc.set_pending(chat_id, pend, user_id=user_id)
        return question

    @staticmethod
    def _cc_chain_site(step: str, action: dict) -> Optional[str]:
        """Каким словом назвать сайт, открытый шагом цепочки — для «…и
        включи музыку» (поиск на этом сайте, а не запуск приложения).
        None — шаг сайт не открывал."""
        if not isinstance(action, dict) or action.get("kind") not in (
                "url", "nav", "multi"):
            return None
        if action.get("search_site"):
            return str(action["search_site"])
        try:
            names = parse_open_many(step)
        except Exception:
            names = None
        if names:
            return str(names[-1])
        return None

    def _cc_execute_reply(self, action: dict, chat_id, turn_lang):
        """Исполнение действия fast-path → (ok, реплика). Чтение отвечает
        самим прочитанным текстом, остальное — «Готово, …»/«Не удалось …»."""
        cc = self.computer_control
        # Секрет ввода и текст приватной страницы — в историю маской
        # (реплики ниже их повторяют: «ввёл «…» в поле …», прочитанное)
        self._cc_hist_note_typed(action)
        ok, detail = cc.execute(action, chat_id, router=self.router)
        from app.features import cc_texts
        if not ok and isinstance(action.get("confirm_required"), dict):
            # Гейт подтверждения — не сбой: вопрос соберёт _cc_run_steps
            return False, detail
        if action.get("kind") == "read":
            if ok:
                self._cc_hist_note_read(action, detail)
            return ok, (detail if ok else
                        cc_texts.t("read_failed", turn_lang, detail=detail))
        return ok, self._cc_reply(
            action, ok, detail,
            cc_texts.t("done", turn_lang,
                       what=cc.describe_done(action, lang=turn_lang)) if ok else
            cc_texts.t("failed", turn_lang,
                       what=cc.describe(action, lang=turn_lang), detail=detail),
            lang=turn_lang)

    def _cc_scroll_goal_reply(self, goal: str, chat_id, turn_lang) -> dict:
        """«пролистай до X» / «найди X на странице» / «докрути до конца» —
        ограниченный доскролл до цели с фото места. Чтение + прокрутка,
        ничего не нажимается — без подтверждения, выполняется сразу.
        → {"reply", "ok"} для лесенки."""
        from app.features import cc_texts
        sg = None
        sg_err = None
        try:
            sg, sg_err = self.computer_control.scroll_to_goal(
                goal, None, chat_id=str(chat_id or ""))
        except Exception as e:
            logger.debug(f"[CompControl] fast-path доскролл до цели не "
                         f"удался: {e}")
            sg_err = cc_texts.t("scroll_failed", turn_lang)
        if sg is None:
            if sg_err == cc_texts.t("stopped", turn_lang):
                # «стоп» — не сбой: честное «остановлено» без реплики-ошибки
                return {"ok": False, "reply": sg_err}
            return {"ok": False, "reply": self._cc_reply(
                None, False, sg_err,
                sg_err or cc_texts.t("scroll_failed", turn_lang),
                lang=turn_lang)}
        if sg["found"]:
            if sg.get("edge") == "bottom":
                _tmpl = cc_texts.t("scroll_goal_bottom", turn_lang)
            elif sg.get("edge") == "top":
                _tmpl = cc_texts.t("scroll_goal_top", turn_lang)
            else:
                _tmpl = cc_texts.t("scroll_goal_found", turn_lang,
                                   goal=sg["goal"])
            if sg.get("shot"):
                self._pending_photos.setdefault(str(chat_id), []).append({
                    "data": sg["shot"],
                    "caption": cc_texts.t("scroll_goal_caption", turn_lang,
                                          goal=sg["goal"], host=sg["host"])
                    if not sg.get("edge") else
                    cc_texts.t("scroll_goal_edge_caption", turn_lang,
                               host=sg["host"])})
            return {"ok": True, "reply": self._cc_reply(
                None, True, None, _tmpl, lang=turn_lang)}
        return {"ok": False, "reply": self._cc_reply(
            None, False, None,
            cc_texts.t("scroll_goal_miss", turn_lang, goal=sg["goal"]),
            lang=turn_lang)}

    def _cc_page_view_reply(self, site: Optional[str], shot_asked: bool,
                            full: bool, chat_id, turn_lang,
                            user_input: str) -> dict:
        """«что на странице?» / «пришли скриншот» / «покажи всю страницу» —
        отчёт об открытой странице: текстом — список элементов, плюс
        скриншот в pending-фото бакет (платформа досылает картинку следом).
        Как чтение: ничего не меняет, без подтверждения. → {"reply", "ok"}."""
        from app.features import cc_texts
        pv = None
        pv_err = None
        try:
            pv, pv_err = self.computer_control.page_view_report(
                site, chat_id=str(chat_id or ""), full_page=full)
        except Exception as e:
            logger.debug(f"[CompControl] fast-path отчёт о странице не "
                         f"удался: {e}")
            pv_err = cc_texts.t("page_view_failed", turn_lang)
        from app.features.cc_privacy import scrub_url
        # Адрес в отчёте — без токенов/кодов (reset?token=…): отчёт уходит
        # в ответ, в историю и, на обычной странице, облачной модели.
        # Приватная страница (вход/оплата/банк или private_hosts) — отчёт
        # только человеку, в историю заглушка, LLM его не видит
        pv_url = scrub_url(pv.get("url") or "") if pv is not None else ""
        pv_private = (self._cc_private_host(pv.get("url"), pv.get("host"))
                      if pv is not None else None)
        if pv is not None and pv.get("full") and pv.get("shots"):
            # Оглавление текстом + кадры-куски альбомом (партии по 10 —
            # лимит media group Telegram; остаток — по «ещё»)
            pv_desc = page_view_full_text(
                pv_url, pv["host"], pv.get("outline"),
                truncated=pv.get("truncated", False), lang=turn_lang)
            if pv_private:
                self._cc_hist_note_private(pv_desc, pv["host"] or pv_private,
                                           "page_view")
            pv_line = self._cc_reply(
                None, True, None,
                cc_texts.t("page_full_line", turn_lang), lang=turn_lang)
            pv_reply = f"{pv_line}\n\n{pv_desc}"
            shots = pv["shots"]
            first, extra = shots[:10], shots[10:]
            self._pending_photos.setdefault(str(chat_id), []).extend(
                {"data": s} for s in first)
            if extra:
                self._pending_more_photos[str(chat_id)] = {
                    "photos": [{"data": s} for s in extra],
                    "ts": time.time()}
                pv_reply += "\n\n" + cc_texts.t(
                    "page_full_more", turn_lang, n=len(first),
                    total=len(shots))
            return {"ok": True, "reply": pv_reply}
        if pv is not None:
            pv_reply = page_view_text(pv_url, pv["host"], pv["items"],
                                      lang=turn_lang)
            # Отвечает персона своим голосом: данные страницы — в промпт;
            # LLM молчит — честный фолбэк на шаблонный список. Приватная
            # страница (вход/оплата/банк) — только шаблон: её текст в
            # облачную/веб-чат модель не уходит (ни сейчас, ни историей)
            if pv_private:
                self._cc_hist_note_private(pv_reply, pv["host"] or pv_private,
                                           "page_view")
            else:
                pv_reply = self._persona_page_view_reply(
                    user_input, pv_reply) or pv_reply
            if pv["shot"]:
                self._pending_photos.setdefault(str(chat_id), []).append({
                    "data": pv["shot"],
                    "caption": cc_texts.t("page_shot_caption", turn_lang,
                                          host=pv["host"])})
            elif shot_asked:
                # Скриншот просили явно, а кадр не получился — честно
                # говорим, список элементов всё равно дан
                pv_reply += "\n\n" + cc_texts.t("page_shot_failed", turn_lang)
            return {"ok": True, "reply": pv_reply}
        return {"ok": False,
                "reply": pv_err or cc_texts.t("page_view_failed", turn_lang)}

    def _cc_ladder(self, text: str, chat_id, turn_lang, user_input: str = "",
                   page_site: Optional[str] = None) -> Optional[dict]:
        """Один шаг команды через лесенку regex-парсеров, последним — LLM-
        ярус. text — нормализованная команда (без OCR/файлов). → None — не
        команда; {"reply", "ok"} — шаг уже обслужен (чтение/отчёт/доскролл/
        отказ резолва); {"action", "direct"} — действие к подтверждению или
        исполнению (direct — без подтверждения: чтение, переключение
        вкладки). page_site — сайт, открытый прошлым шагом цепочки."""
        cc = self.computer_control
        cid = str(chat_id or "")
        # «пролистай до X» — своим блоком ДО лесенки клика: внутри неё
        # «пролистай до напитков» перехватило бы автолистание
        # (parse_scroll_request) и крутило бы до «стоп», а человек при
        # удалённом управлении страницу не видит
        if cc.click:
            cc_sg = parse_scroll_to_goal(text)
            if cc_sg:
                return self._cc_scroll_goal_reply(cc_sg, chat_id, turn_lang)

        # «включи X на <сайте>» (поиск на сайте) проверяем ДО «открой X»:
        # иначе «<фильм> на <сайте>» уйдёт в резолв как имя сайта
        cc_action = None
        cc_err = None
        cc_pair = parse_search_on_site(text)
        if cc_pair:
            try:
                cc_action = cc.resolve_search(*cc_pair)
            except Exception as e:
                logger.debug(f"[CompControl] fast-path поиск на сайте не удался: {e}")
        # Шаг цепочки после открытия сайта: «(открой ютуб) и включи музыку» —
        # поиск на ЭТОМ сайте, а не запуск приложения «Музыка». Медиа/клавиши
        # («включи звук», «нажми пробел») — дальше по лесенке как есть
        m_play = _CHAIN_PLAY_RE.match(text) if page_site and cc_action is None \
            else None
        if m_play and not parse_media_request(text) \
                and not parse_key_request(text):
            try:
                # «включи X» — сразу первый результат, «найди X» — выдача
                cc_action = cc.resolve_search(m_play.group("query"), page_site,
                                              direct=bool(m_play.group("play")))
            except Exception as e:
                logger.debug(f"[CompControl] поиск на сайте цепочки не удался: {e}")
        # «открой на site.ru/827 студентам — …»: явный адрес в фразе —
        # открываем его, даже когда вокруг длинный текст; хвост по
        # сепараторам « - »/«→» — путь кликами по странице (nav-действие).
        # До клика и open_many: те обе длинную фразу отвергнут по длине
        if cc_action is None:
            cc_nav = parse_open_with_url(text)
            if cc_nav:
                try:
                    cc_action = cc.resolve_nav(*cc_nav)
                except Exception as e:
                    logger.debug(f"[CompControl] fast-path явный URL не удался: {e}")
        # «нажми X» / «скачай X» / «введи X в поле Y» / «отправь» / «открой X
        # на этой странице» … — агентный клик/ввод по снапшоту страницы;
        # подтверждение покажет, что именно нажмётся. Неудача — честный
        # отказ шаблоном: LLM-путь это выполнить не может, а изобразить
        # выполнение — может, поэтому туда не пускаем. «Не наша» команда
        # ввода (resolve_type → (None, None)) идёт дальше. Порядок парсеров
        # значим: медиа ДО клавиш, клавиши ДО клика («нажми esc» — не цель
        # «esc»), стирание ДО корзины/закрытия («удали X»), корзина ДО
        # клика и инвентаря, операции вкладки ДО закрытия-кликом («закрой
        # вкладку»), закрытие и hover ДО generic-клика
        if cc_action is None and cc.click:
            cc_parsed = self._cc_parse_page_command(text)
            if cc_parsed:
                resolver = {"download": cc.resolve_download,
                            "type": cc.resolve_type,
                            "scroll": cc.resolve_scroll,
                            "cart": cc.resolve_cart,
                            "send": cc.resolve_send,
                            "key": cc.resolve_key,
                            "hover": cc.resolve_hover,
                            "slider": cc.resolve_slider,
                            "tab_op": cc.resolve_tab_op}.get(
                    cc_parsed[2], cc.resolve_click)
                try:
                    cc_action, cc_err = resolver(cc_parsed[0], cc_parsed[1],
                                                 self.router, chat_id=cid)
                except Exception as e:
                    logger.debug(f"[CompControl] fast-path клик/скачивание не удалось: {e}")
                    from app.features import cc_texts
                    cc_err = cc_texts.t("ladder_action_failed", turn_lang)
                if cc_action is None and cc_err:
                    return {"ok": False, "reply": self._cc_reply(
                        None, False, cc_err, cc_err, lang=turn_lang)}
        # «перейди на вкладку X» / «какие вкладки открыты» — ничего не меняют
        # на страницах, сразу, без подтверждения. Явная форма со словом
        # «вкладку» при промахе — честный отказ со списком; мягкая («перейди
        # на X») при промахе молча идёт дальше (может, это «открой X»)
        cc_direct = False
        if cc_action is None and cc.click:
            if parse_tab_list_query(text):
                try:
                    return {"ok": True, "reply": cc.list_open_tabs_text()}
                except Exception as e:
                    logger.debug(f"[CompControl] список вкладок не удался: {e}")
                    from app.features import cc_texts
                    return {"ok": False,
                            "reply": cc_texts.t("ladder_tabs_failed",
                                                turn_lang)}
            cc_tab = parse_tab_switch(text)
            if cc_tab:
                try:
                    cc_action, cc_err = cc.resolve_tab_switch(
                        cc_tab[0], cc_tab[1], chat_id=cid)
                    # Переключение — сразу; фолбэк на открытие сайта
                    # (url-действие) идёт обычным путём с подтверждением
                    cc_direct = bool(cc_action) and \
                        cc_action.get("kind") == "tab_switch"
                except Exception as e:
                    logger.debug(f"[CompControl] fast-path переключение "
                                 f"вкладки не удалось: {e}")
                    from app.features import cc_texts
                    cc_err = cc_texts.t("ladder_tab_switch_failed", turn_lang)
                if cc_action is None and cc_err:
                    return {"ok": False, "reply": self._cc_reply(
                        None, False, cc_err, cc_err, lang=turn_lang)}
        # «прочитай последнее сообщение (на <сайте>)» — чтение со страницы:
        # без подтверждения, прочитанный текст — сразу ответом
        if cc_action is None and cc.click:
            cc_read = parse_read_request(text)
            if cc_read:
                try:
                    cc_action, cc_err = cc.resolve_read(*cc_read, chat_id=cid)
                except Exception as e:
                    logger.debug(f"[CompControl] fast-path чтение не удалось: {e}")
                    from app.features import cc_texts
                    cc_err = cc_texts.t("ladder_read_failed", turn_lang)
                if cc_action is None and cc_err:
                    return {"ok": False, "reply": cc_err}
        # «увеличь/уменьши/сбрось масштаб» — обратимая настройка вкладки
        if cc_action is None and cc.click:
            cc_zoom = parse_zoom_request(text)
            if cc_zoom is not None:
                try:
                    cc_action, cc_err = cc.resolve_zoom(*cc_zoom, chat_id=cid)
                except Exception as e:
                    logger.debug(f"[CompControl] fast-path зум не удался: {e}")
                    from app.features import cc_texts
                    cc_err = cc_texts.t("ladder_zoom_failed", turn_lang)
                if cc_action is None and cc_err:
                    return {"ok": False, "reply": self._cc_reply(
                        None, False, cc_err, cc_err, lang=turn_lang)}
        # «что на странице?» / «пришли скриншот» / «покажи всю страницу»
        if cc_action is None and cc.click:
            cc_pv = parse_page_view_request(text)
            if cc_pv is not None:
                return self._cc_page_view_reply(
                    cc_pv[0], cc_pv[1], cc_pv[2], chat_id, turn_lang,
                    user_input or text)
        if cc_action is None:
            # «X and Y» делится, только если обе части — известные цели
            # («Barnes and Noble» — одно название)
            cc_names = parse_open_many(
                text, known=getattr(cc, "is_known_target", None))
            if cc_names:
                try:
                    cc_action = cc.resolve_many(cc_names)
                except Exception as e:
                    logger.debug(f"[CompControl] fast-path резолв не удался: {e}")
        tag_origin(cc_action, "fast")
        # Секрет в самой команде («введи пароль Kotik2019 …»), которую regex
        # не разобрал: в облачный LLM-ярус такой текст не уходит — просим
        # сказать по шаблону, который разбирает regex-путь
        if cc_action is None and cc.click and command_has_secret(text):
            for v in command_secret_values(text):
                try:
                    self._cc_hist_add_mask("secret", v)
                except Exception:
                    pass
            logger.info("[CompControl] команда с секретом не разобрана "
                        "regex-ом — LLM-ярус пропущен")
            from app.features import cc_texts
            return {"ok": False,
                    "reply": cc_texts.t("secret_rephrase", turn_lang)}
        # LLM-ярус разбора (последний): ни один regex-парсер не сматчился —
        # модель классифицирует фразу в JSON-действие, дальше те же
        # резолверы и тот же confirm/allowlist. Только для фраз, похожих на
        # команду: на «как дела?» лишний вызов модели до основного ответа
        # лишь тормозит. Ошибка резолва — честный отказ
        if cc_action is None and cc.click and len(text) <= 400 \
                and looks_like_command(text, turn_lang):
            try:
                cc_action, cc_err = cc.resolve_intent_llm(
                    text, self.router, chat_id=cid)
            except Exception as e:
                logger.debug(f"[CompControl] LLM-разбор команды не удался: {e}")
            tag_origin(cc_action, "intent_llm")
            if is_goal_task(cc_action):
                # Цель-задача из свободной фразы — всегда с подтверждением
                cc_action["force_confirm"] = True
            self._cc_mark_model_url(cc, cc_action, text)
            kind = cc_action.get("kind") if cc_action else None
            if kind == "scroll_goal":
                return self._cc_scroll_goal_reply(
                    str(cc_action["goal"]), chat_id, turn_lang)
            if kind == "page_view":
                return self._cc_page_view_reply(
                    cc_action.get("site"), bool(cc_action.get("screenshot")),
                    bool(cc_action.get("full")), chat_id, turn_lang,
                    user_input or text)
            if cc_action is None and cc_err:
                return {"ok": False, "reply": self._cc_reply(
                    None, False, cc_err, cc_err, lang=turn_lang)}
        if not cc_action:
            return None
        if page_site and cc_action.get("kind") == "app":
            # Шаг цепочки после открытия сайта не запускает программу
            # («…и включи музыку» ≠ Music.app): цель — на открытой странице
            obj = re.sub(r"^\S+\s*", "", text).strip() or text
            try:
                cc_action, cc_err = cc.resolve_click(obj, PAGE_REF, self.router,
                                                     chat_id=cid)
            except Exception as e:
                logger.debug(f"[CompControl] клик шага цепочки не удался: {e}")
                from app.features import cc_texts
                cc_action, cc_err = None, cc_texts.t("ladder_action_failed",
                                                     turn_lang)
            if cc_action is None:
                from app.features import cc_texts
                return {"ok": False, "reply": self._cc_reply(
                    None, False, cc_err,
                    cc_err or cc_texts.t("ladder_not_found", turn_lang),
                    lang=turn_lang)}
            tag_origin(cc_action, "fast")
        # В лог — без секретов: ввод маской (_describe_safe), текст команды
        # ввода — только длиной
        from app.features.cc_privacy import redact_inline
        shown = (f"{len(text)} симв." if cc_action.get("kind") == "type"
                 else redact_inline(text, 40))
        logger.info(f"[CompControl] fast-path: '{shown}' → "
                    f"{'задача' if is_goal_task(cc_action) else getattr(cc, '_describe_safe', cc.describe)(cc_action)}")
        return {"action": cc_action,
                "direct": cc_direct or cc_action.get("kind") == "read"}

    @staticmethod
    def _cc_mark_model_url(cc, cc_action, text: str) -> None:
        """LLM-ярус, action=open: адрес выбрала модель. Домен не из
        sites/allow_domains или адрес с query/fragment (выдуманный «ЛК банка»
        с телефоном в параметрах) — via_search: гейт требует «да» при любом
        confirm, как у маркера и агента (_open_needs_confirm). Домен, который
        пользователь сам назвал в фразе, без параметров — его выбор."""
        if not isinstance(cc_action, dict):
            return
        from urllib.parse import urlsplit
        from app.features.task_agent import _open_needs_confirm
        low = str(text or "").lower()
        items = (cc_action.get("items") or []
                 if cc_action.get("kind") == "multi" else [cc_action])
        for a in items:
            if not isinstance(a, dict) or a.get("kind") not in ("url", "nav"):
                continue
            url = str(a.get("value") or "")
            try:
                parts = urlsplit(url)
                host = (parts.hostname or "").lower()
            except ValueError:
                parts, host = None, ""
            if (parts is not None and host and not parts.query
                    and not parts.fragment and not a.get("via_search")
                    and re.search(r"(?<![\w.-])" + re.escape(
                        host[4:] if host.startswith("www.") else host)
                        + r"(?![\w-])", low)):
                continue
            if _open_needs_confirm(cc, {}, a, url):
                a["via_search"] = True
        if any(isinstance(a, dict) and a.get("via_search") for a in items):
            cc_action["via_search"] = True

    @staticmethod
    def _cc_parse_page_command(text: str):
        """Каскад regex-парсеров команд странице → (цель, сайт/PAGE_REF,
        вид резолвера) или None. Порядок значим (см. _cc_ladder)."""
        cc_page_goal = parse_open_on_page(text)
        if cc_page_goal:
            return cc_page_goal, PAGE_REF, "click"
        cc_dl = parse_download_request(text)
        if cc_dl and not _DL_ELSEWHERE_RE.search(str(cc_dl[0])):
            # «скачай отчёт с сайта X» — цель на другом сайте (агенту через
            # LLM-ярус), а не разовое скачивание на текущей вкладке
            return cc_dl[0], cc_dl[1], "download"
        cc_type = parse_type_request(text)
        if cc_type:
            # Сайт/поле/текст разберёт resolve_type по снапшоту
            return cc_type, None, "type"
        cc_send = parse_send_request(text)
        if cc_send:
            # «отправь» — Enter в поле, сайт как у клика
            return None, cc_send[1], "send"
        # «пауза»/«тише»/«громче» — медиа-команды плеера клавишами
        cc_media = parse_media_request(text)
        if cc_media:
            return cc_media, None, "key"
        # «нажми пробел/энтер/эскейп» — клавиша в страницу без выбора элемента
        cc_key = parse_key_request(text)
        if cc_key:
            return cc_key[0], cc_key[1], "key"
        # «удали 5 символов» — стирание серией Backspace (тот же key-резолвер)
        cc_erase = parse_erase_request(text)
        if cc_erase:
            return cc_erase[0], cc_erase[1], "key"
        # «промотай страницу» / «стоп»: листание в фоне. «стоп» без активного
        # листания резолвер вернёт (None, None) — фраза уйдёт в диалог
        cc_scroll = parse_scroll_request(text)
        if cc_scroll:
            return ((cc_scroll[0], cc_scroll[2], cc_scroll[3], cc_scroll[4]),
                    cc_scroll[1], "scroll")
        # «убери X из корзины» / «убавь/прибавь X» — корзина сайта
        cc_cart = parse_cart_request(text)
        if cc_cart:
            return cc_cart, None, "cart"
        # «поставь слайдер X на N» — числовой хвост отличает от клика
        cc_slider = parse_slider_request(text)
        if cc_slider:
            return cc_slider[0], cc_slider[1], "slider"
        # «обнови/перезагрузи/закрой вкладку (X)»
        cc_tab_op = parse_tab_op(text)
        if cc_tab_op:
            return cc_tab_op[1], cc_tab_op[0], "tab_op"
        # «закрой окно/попап» — закрытие (целевое или крестик)
        cc_close = parse_close_request(text)
        if cc_close:
            return cc_close[0], cc_close[1], "click"
        # «наведи (курсор) на X» — hover без клика
        cc_hover = parse_hover_request(text)
        if cc_hover:
            return cc_hover[0], cc_hover[1], "hover"
        cc_click = parse_click_request(text)
        if cc_click:
            return cc_click[0], cc_click[1], "click"
        return None

    def process_message(self, user_input: str, user_id: str = "default",
                        chat_id: str = None, user_name: str = None,
                        reply_context: str = None,
                        reply_to_bot_message_id: Optional[int] = None,
                        on_token=None,
                        raw_user_text: Optional[str] = None,
                        from_skin: bool = False) -> str:
        """Обработка сообщения; парная к START строка END — по её наличию в
        логе видно, дошла ли генерация до конца и сколько заняла (без неё
        не отличить «ответ сгенерирован, но не доставлен» от «завис»).
        from_skin — реплика из скина веб-интерфейса: режим управления в этом
        ходе недоступен (см. _SKIN_TURN)."""
        t0 = time.monotonic()
        # Вложенный вызов внутри хода из скина (агент задач передаёт реплику
        # дальше) флаг не снимает: возвращаем прежнее значение, а не False
        prev_skin = getattr(_SKIN_TURN, "on", False)
        _SKIN_TURN.on = prev_skin or bool(from_skin)
        try:
            # Ход пользователя открыт ДО записи его реплики в STM и закрыт
            # после записи ответа: фоновая инициатива/ритм в этом окне в STM
            # не пишут (см. user_turn) — порядок user → ответ не рвётся.
            # Контекст браузера режима управления (вкладка, листание) — этого
            # чата на весь ход: ключ тот же, что у режима (chat_id или user_id).
            # Область диалога — свой тред веб-чата у этого чата (иначе сайт
            # показал бы модели промпты других чатов персоны)
            _cc = getattr(self, "computer_control", None)
            with self.user_turn(self.stm_key(chat_id, user_id)), (
                    _cc.chat_scope(chat_id or user_id)
                    if _cc is not None and hasattr(_cc, "chat_scope")
                    else nullcontext()), \
                    dialog_scope(self.stm_key(chat_id, user_id)):
                reply = self._process_message_impl(
                    user_input, user_id=user_id, chat_id=chat_id, user_name=user_name,
                    reply_context=reply_context,
                    reply_to_bot_message_id=reply_to_bot_message_id,
                    on_token=on_token, raw_user_text=raw_user_text,
                    from_skin=_SKIN_TURN.on,
                )
        except BaseException:
            logger.info(f"[BotInstance] process_message END (исключение): "
                        f"{time.monotonic() - t0:.1f}s | chat_id={chat_id}")
            raise
        finally:
            _SKIN_TURN.on = prev_skin
        logger.info(
            f"[BotInstance] process_message END: {time.monotonic() - t0:.1f}s | "
            f"ответ {len(reply) if isinstance(reply, str) else 'нет'} симв. | chat_id={chat_id}"
        )
        return reply

    def _process_message_impl(self, user_input: str, user_id: str = "default",
                              chat_id: str = None, user_name: str = None,
                              reply_context: str = None,
                              reply_to_bot_message_id: Optional[int] = None,
                              on_token=None,
                              raw_user_text: Optional[str] = None,
                              from_skin: bool = False) -> str:
        from app.features import side_tasks

        # Что пользователь реально НАПИСАЛ (текст сообщения/подпись), в отличие
        # от user_input, который для фото/документов — составной текст с OCR/
        # содержимым файла ("The user sent an image...\n{ocr}"). Подтверждение
        # pending-действия (см. cc fast-path ниже) обязано смотреть только сюда:
        # слово «да»/«нет» внутри распознанного текста фото не должно решать
        # судьбу отложенного клика/shell-команды. По умолчанию (обычный текст)
        # raw_user_text совпадает с user_input — вызывающая сторона передаёт
        # его отдельно только для составного ввода (см. telegram_bot.py,
        # app/api/server.py).
        if raw_user_text is None:
            raw_user_text = user_input
        # Язык пользователя в этом ходе — для изолированных реплик
        # (CC-результаты, настройка обучения): в них нет истории диалога
        try:
            turn_lang = detect_dialogue_language(
                raw_user_text, self.memory.stm.get_last(8, chat_id=chat_id), user_id)
        except Exception:
            turn_lang = detect_language(raw_user_text)

        # Очищаем pending-состояние ЭТОГО чата от предыдущего вызова (атрибуты per-chat:
        # process_message выполняется конкурентно в потоках для разных чатов, и общие
        # атрибуты давали гонки — чужой фидбек/вопрос уезжал не в тот чат).
        self._pending_list_messages[str(chat_id)] = []
        # Хвост расщеплённого ответа от предыдущего вызова тоже гасим
        self._pending_split_messages[str(chat_id)] = []
        # Скриншоты страницы от предыдущего вызова — тоже
        self._pending_photos[str(chat_id)] = []
        # Каким был последний ответ-вопрос: 'frequency' | 'continue' | None.
        # Нужно telegram-слою, чтобы зарегистрировать отправленное сообщение как «вопрос бота»
        # для reply-to-логики обучения (пользователь может ответить reply-ом на этот вопрос).
        self._pending_question_kind[str(chat_id)] = None
        # Переспрос «Записать «X» в список дел?» отвечается только СЛЕДУЮЩИМ
        # ходом спросившего: забираем его сразу, до ранних возвратов
        # (переключатель режима, «почини браузер», режим управления), а решаем
        # по нему ниже (_list_offer_turn) — иначе «ок» через несколько ходов
        # записало бы старое
        list_offer = self._take_list_offer(chat_id, user_id)
        # Уведомления о карантине веб-чатов: доносим до пользователя коротким
        # служебным сообщением вслед за ответом — сайт в карантине молча
        # пропускается, иначе деградация не видна. Текст зависит от природы
        # блокировки: капча (challenge — нужны руки пользователя), лимит
        # сообщений (ratelimit — есть время восстановления), отказ сайта
        # (refused — перегрузка/тариф).
        try:
            from app.features import web_llm as _wl
            for _alert in _wl.pop_quarantine_alerts():
                _site = str(_alert.get("site") or "?")
                _kind = str(_alert.get("kind") or "challenge")
                if _kind == "ratelimit":
                    _until = float(_alert.get("until") or 0)
                    _when = timeutil.from_ts(_until).strftime("%H:%M") \
                        if _until else "позже"
                    self._pending_list_messages[str(chat_id)].append(
                        f"⚠️ {_site}: закончился бесплатный лимит сообщений — "
                        f"восстановится ≈ в {_when}. До тех пор пропускаю "
                        "этот чат, отвечаю через другие модели.")
                elif _kind == "login" and "возраст" in str(_alert.get("reason") or ""):
                    self._pending_list_messages[str(chat_id)].append(
                        f"⚠️ {_site} просит подтвердить возраст — сделай это "
                        "в браузере бота (скажи «почини браузер», чтобы окно "
                        "стало видимым). Пока пропускаю этот чат и отвечаю "
                        "через другие модели; как подтвердишь — подхвачу сам.")
                elif _kind == "login":
                    self._pending_list_messages[str(chat_id)].append(
                        f"⚠️ {_site} выкинул бота из аккаунта — зайди в него "
                        "в браузере бота (скажи «почини браузер», чтобы окно "
                        "стало видимым). Пока пропускаю этот чат и отвечаю "
                        "через другие модели; как войдёшь — подхвачу сам "
                        "в течение пары минут.")
                elif _kind == "refused":
                    self._pending_list_messages[str(chat_id)].append(
                        f"⚠️ {_site} временно отклоняет сообщения "
                        "(перегрузка или лимит тарифа) — чат в карантине "
                        "(~30 мин), отвечаю через другие модели.")
                else:
                    self._pending_list_messages[str(chat_id)].append(
                        f"⚠️ {_site} просит подтверждение «я не робот» — этот чат "
                        "в карантине (~30 мин), отвечаю через другие модели. "
                        "Открой браузер бота и пройди проверку — карантин снимется "
                        "сам при следующем обращении.")
        except Exception:
            pass
        # Готовый ответ, минующий основной LLM-вызов (фидбек теста, реплики
        # setup/continue обучения). Локальная переменная, не атрибут: process_message
        # выполняется конкурентно для разных чатов, общий атрибут утёк бы между ними.
        skip_llm_answer = None
        # Вопрос о секции открытой страницы («что находится в X?»): живой текст
        # секции заполняется в cc fast-path ниже и уезжает в LLM контекстом
        # (context_parts_out) — список/ответ формулирует модель, не шаблон
        page_section_note = None

        # Сырой текст реплики в лог не пишем: в режиме управления это бывает
        # пароль ответом агенту/слоту сценария — только длина; иначе без
        # секретов (маска логов процесса — app.core.log_privacy)
        from app.core.log_privacy import control_mode_active, input_for_log
        logger.info("[BotInstance] process_message START: "
                    + input_for_log(user_input, control_mode_active(
                        self, chat_id or user_id))
                    + f" | chat_id={chat_id}")

        # «перейди в режим управления» / «выйди из режима управления» —
        # переключатель computer control. Работает всегда и раньше всех
        # fast-path: иначе «выйди…» мог бы съесть CC-парсер, а «перейди…» —
        # отвечаться LLM. Только для авторизованного пользователя (владелец/
        # allowlist) — иначе перехвата нет вообще, фраза уходит в обычный
        # диалог: для чужого режима управления «не существует», а не честно
        # отказывает и не палит, что фича есть.
        # Ключ режима — chat_id, иначе user_id (веб-API без chat_id): так же
        # его читает ответ API (control_mode в ChatResponse). Команды режима —
        # только из того, что человек написал (raw_user_text), не из OCR/файла
        cc_mode_key = str(chat_id or user_id or "")
        # Ключ и язык хода — менеджеру управления (на поток хода): execute и
        # опросы браузера веб-чата без chat_id видят «стоп» по тому же ключу,
        # что ставит cc_turn_enter; служебные тексты — на языке хода
        if getattr(self, "computer_control", None):
            try:
                self.computer_control.set_turn(cc_mode_key, turn_lang)
            except Exception as e:
                logger.debug(f"[BotInstance] ход для режима управления: {e}")
        # Реплика из скина при включённом режиме управления или «перейди в
        # режим управления»: исполнять её нельзя (_cc_allowed в этом ходе
        # False), а молча уйти в обычный диалог — значит дать персоне
        # «открыть» сайт на словах. Владельцу — подсказка, где режим работает
        if (from_skin and cc_mode_key
                and getattr(self, "computer_control", None)
                and self._cc_user_allowed(user_id)
                and (self.control_mode_on(cc_mode_key)
                     or parse_control_mode(raw_user_text) is True)):
            from app.features import cc_texts
            _skin_reply = cc_texts.t("skin_no_control", turn_lang)
            logger.info(f"[BotInstance] реплика из скина в режиме управления "
                        f"не исполняется (chat {cc_mode_key})")
            self.memory.add_message("user", user_input, user_id, chat_id, user_name)
            self.memory.add_message("assistant", _skin_reply, user_id, chat_id)
            if self.proactive:
                self.proactive.record_user_response(chat_id)
            return _skin_reply
        if cc_mode_key and self._cc_allowed(user_id, chat_id):
            _mode = parse_control_mode(raw_user_text)
            # Новый ход: «стоп» прошлого хода отработал, флаг снимаем.
            # control_mode_on здесь же гасит режим после простоя
            try:
                if self.computer_control:
                    self.computer_control.stop_clear(cc_mode_key)
            except Exception:
                pass
            if self.control_mode_on(cc_mode_key):
                self._cc_mode_touch(cc_mode_key)
            _idle_note = self._cc_pop_idle_notice(cc_mode_key, turn_lang)
            if _idle_note and _mode is not True:
                # Честно сообщаем отдельным сообщением вслед за ответом: иначе
                # команда молча ушла бы в обычный диалог
                self._pending_list_messages.setdefault(str(chat_id), []).append(
                    _idle_note)
            if _mode is not None:
                _cm_reply = self._control_mode_switch(cc_mode_key, _mode,
                                                      lang=turn_lang)
                self.memory.add_message("user", user_input, user_id, chat_id, user_name)
                self.memory.add_message("assistant", _cm_reply, user_id, chat_id)
                if self.proactive:
                    self.proactive.record_user_response(chat_id)
                return _cm_reply

        # «почини браузер» — rescue пула H: веб-чаты перезапускаются в
        # видимом Chrome, чтобы пользователь прошёл капчу руками. Тоже
        # только для авторизованных — иначе кто угодно в чате мог бы поднять
        # видимое окно браузера с сессиями владельца.
        if cc_mode_key and self._cc_allowed(user_id, chat_id) \
                and _RESCUE_BROWSER_RE.match(raw_user_text):
            try:
                from app.features import browser_actions as _ba
                _ok = _ba.rescue_pool_h()
            except Exception:
                _ok = False
            from app.features import cc_texts
            _reply = cc_texts.t("rescue_ok" if _ok else "rescue_fail", turn_lang)
            self.memory.add_message("user", user_input, user_id, chat_id, user_name)
            self.memory.add_message("assistant", _reply, user_id, chat_id)
            if self.proactive:
                self.proactive.record_user_response(chat_id)
            return _reply
        # Следующая реплика после «почини браузер» — «готово»: rescue, которому
        # чинить нечего, завершается сразу, а не по истечении срока
        if cc_mode_key and self._cc_allowed(user_id, chat_id):
            try:
                from app.features import web_llm as _wl
                _wl.finish_idle_rescue()
            except Exception:
                pass

        # Быстрый путь computer_control: перехват «да»/«нет» на pending-действие
        # и голая команда «открой X» — оба обслуживаются шаблонно, весь тяжёлый
        # LLM-пайплайн (rewrite/поиск/LTM/генерация, ~10+ сек) пропускается.
        # Работает только в режиме управления («перейди в режим управления»)
        # И только для авторизованного пользователя: режим — на весь чат
        # (общий браузер с сессиями владельца), но исполнять команды в нём
        # должен только владелец/allowlist — иначе любой участник группового
        # чата, где кто-то один включил режим, мог бы им управлять.
        if (self.computer_control and cc_mode_key
                and self.control_mode_on(cc_mode_key)
                and self._cc_allowed(user_id, chat_id)):
            # Команда для лесенки — нормализованный текст, который человек
            # написал сам: «Коннор, можешь открыть ютуб?» → «открой ютуб»
            cc_text = normalize_command(raw_user_text, self._address_names())
            # Автор хода: им подписывается pending, который создаст этот ход
            # (подтвердить сможет только он)
            try:
                self.computer_control.note_requester(chat_id, user_id)
            except Exception:
                pass
            # Сценарии — ДО pending-confirm и fast-path парсеров: ответы слотов
            # («гавайскую») и «отмена» при живом прогоне не должны уходить
            # в команды странице; «запомни сценарий X» и имя сценария —
            # тоже раньше «открой X»
            if self.scenario_manager:
                from app.features import cc_texts
                sc_reply = None
                try:
                    # Сценарию — только то, что человек НАПИСАЛ: OCR фото/текст
                    # файла в составном user_input не должны ни запускать
                    # сценарий, ни вписываться в поле сайта ответом на слот
                    sc_text = raw_user_text or ""
                    # Ввод шагов сценария → маска истории (менеджер мог
                    # смениться живым переключением настроек)
                    self._cc_hist_hook_cc()
                    # Живой прогон агента: реплика — ответ ему («пепперони»,
                    # «да»), а не имя сценария. Раньше она уходила в pending
                    # «Запустить сценарий?», и следующее «да» съедал агент
                    ta_busy_run = bool(self.task_agent and self.task_agent.active(
                        chat_id or user_id))
                    if ta_busy_run and not self.scenario_manager.active(chat_id):
                        pass
                    elif self.scenario_manager.active(chat_id):
                        if self.scenario_manager.parse_cancel(sc_text):
                            sc_reply = self.scenario_manager.cancel(chat_id)
                        elif not sc_text.strip() and raw_user_text != user_input:
                            sc_reply = cc_texts.t("scenario_text_only",
                                                  turn_lang)
                        else:
                            # Ответ на слот-секрет («Что ввести в поле
                            # «Пароль»?») — маской в STM и в KnownSecrets
                            # ДО записи реплики; ввод сценария — хуком
                            # on_typed менеджера (describe_done ввода)
                            self._cc_sc_note_answer(chat_id, sc_text)
                            sc_reply = self.scenario_manager.feed(
                                chat_id, sc_text, self.router)
                    else:
                        # «начни записывать сценарий (X)» — явные скобки
                        # записи; «сохрани сценарий» внутри неё берёт трассу
                        # с момента старта (обрабатывает record_reply).
                        # Без обращения и «пожалуйста», как в подсказке вне
                        # режима: «Коннор, запомни сценарий утро» иначе
                        # не разбиралась и уходила в правило
                        sc_cmd = self._scenario_command_text(sc_text)
                        sc_start = self.scenario_manager.parse_start_record(
                            sc_cmd)
                        if sc_start is not None:
                            sc_reply = self.scenario_manager.record_start(
                                chat_id, sc_start)
                        elif self.scenario_manager.parse_stop_record(sc_cmd):
                            sc_reply = self.scenario_manager.record_stop(chat_id)
                        else:
                            sc_save = self.scenario_manager.parse_save_request(sc_cmd)
                            if sc_save is not None:
                                sc_reply = self.scenario_manager.record_reply(
                                    chat_id, sc_save, self.router)
                            else:
                                sc_match = self.scenario_manager.match_scenario(
                                    sc_text, self._address_names())
                                if sc_match and sc_match[1]:
                                    sc_name = sc_match[0]
                                    logger.info(f"[Scenarios] fast-path: "
                                                f"'{sc_text[:40]}' → «{sc_name}»")
                                    sc_reply = self.scenario_manager.start(
                                        sc_name, chat_id, self.router)
                                elif sc_match:
                                    # Имя сценария лишь встретилось во фразе —
                                    # запуск только после «да» этого же человека
                                    self.computer_control.set_pending(
                                        chat_id, {"kind": "scenario",
                                                  "name": sc_match[0],
                                                  "origin": "scenario"},
                                        user_id=user_id)
                                    sc_reply = cc_texts.t(
                                        "scenario_confirm_start", turn_lang,
                                        name=sc_match[0])
                except Exception as e:
                    logger.warning(f"[Scenarios] fast-path упал: {e}")
                    self.scenario_manager.cancel(chat_id)
                    sc_reply = cc_texts.t("scenario_broken", turn_lang,
                                          err=str(e)[:80])
                if sc_reply is not None:
                    self.memory.add_message("user", user_input, user_id, chat_id, user_name)
                    self.memory.add_message("assistant", sc_reply, user_id, chat_id)
                    if self.proactive and chat_id:
                        self.proactive.record_user_response(chat_id)
                    return sc_reply
            # Агент-автопилот: живой прогон забирает ответы на свои вопросы
            # и «да/нет» на свои подтверждения; «задача: …» — явный запуск.
            # До pending-confirm и fast-path парсеров — по той же причине,
            # что и сценарии («пепперони» — ответ, а не команда странице)
            ta_chat = chat_id or user_id
            ta_live = bool(self.task_agent and self.task_agent.active(ta_chat))
            ta_names = self._address_names()
            if not ta_live and self.task_agent and raw_user_text is not None \
                    and not parse_task_request(raw_user_text, ta_names):
                # «да»/«продолжай» на итог только что закончившейся задачи —
                # возобновить её, а не отдать реплику обычному чату (персона
                # «продолжала» на словах). Только написанное человеком
                ta_live = self.task_agent.reopen(ta_chat, raw_user_text,
                                                 user_id=user_id,
                                                 names=ta_names)
            if ta_live and raw_user_text \
                    and hasattr(self.task_agent, "ask_switch") \
                    and self._ta_foreign_command(raw_user_text, ta_chat):
                # Явная посторонняя команда («открой ютуб», новая «задача:
                # …») при ждущем прогоне — не ответ агенту: «бросить задачу?»
                sw = self.task_agent.ask_switch(ta_chat, raw_user_text,
                                                user_id=user_id)
                if sw:
                    # Команда может нести пароль («задача: войди, пароль …»)
                    # — маски хода до записи, как у обычной команды
                    try:
                        self._cc_hist_note_user_text(raw_user_text,
                                                     contacts=False)
                    except Exception as e:
                        logger.warning(f"[TaskAgent] маска команды не "
                                       f"поставлена: {e}")
                    self.memory.add_message("user", user_input, user_id,
                                            chat_id, user_name)
                    self.memory.add_message("assistant", sw, user_id, chat_id)
                    return sw
            # Запуск задачи — только из написанного человеком: «задача: …»
            # на скриншоте/в файле агента не запускает
            ta_goal = (parse_task_request(raw_user_text or "", ta_names)
                       if self.task_agent and not ta_live else None)
            if ta_live or ta_goal:
                ta_reply = self._task_agent_turn(
                    user_input, user_id, chat_id, user_name, turn_lang,
                    goal=ta_goal, feed_text=raw_user_text or "")
                _pop = getattr(self.task_agent, "pop_switch", None)
                ta_next = _pop(ta_chat) if callable(_pop) else None
                if ta_next:
                    # «да» на «бросить задачу?» — прогон снят, теперь команда,
                    # ради которой его бросили (обычным путём хода)
                    return ta_reply + "\n" + self._process_message_impl(
                        ta_next, user_id=user_id, chat_id=chat_id,
                        user_name=user_name, raw_user_text=ta_next)
                return ta_reply
            # Pending — только свой: в группе «да» другого участника чужое
            # действие не исполняет (get_pending с user_id его не видит)
            cc_pending = self.computer_control.get_pending(chat_id, user_id=user_id)
            if not cc_pending and self.computer_control.pending_expired_recently(
                    chat_id, user_id=user_id) and classify_confirmation(
                    raw_user_text, self._address_names()) == "YES":
                # Голое «да» после истечения TTL: не молчаливая болтовня,
                # а честное «повтори» — иначе человек думает, что сделано
                cc_reply = self._cc_phrase("cc_confirm_expired", turn_lang)
                self.memory.add_message("user", user_input, user_id, chat_id, user_name)
                self.memory.add_message("assistant", cc_reply, user_id, chat_id)
                if self.proactive and chat_id:
                    self.proactive.record_user_response(chat_id)
                return cc_reply
            if cc_pending:
                cc_verdict = classify_confirmation(
                    raw_user_text, self._address_names())
                cc_reply = None
                if cc_pending.get("choices"):
                    # Список «какой сайт открыть?»: номер — выбор варианта
                    # (и согласие), «да» — первый; «нет» — как обычно
                    from app.features.computer_control import parse_choice
                    cc_n = parse_choice(raw_user_text, self._address_names())
                    cc_max = len(cc_pending["choices"])
                    if cc_n is not None and not 1 <= cc_n <= cc_max:
                        # Pending живёт: человек выбирает, просто промахнулся
                        from app.features import cc_texts
                        cc_reply = cc_texts.t("site_choice_range", turn_lang,
                                              n=cc_n, max=cc_max)
                        self.memory.add_message("user", user_input, user_id, chat_id, user_name)
                        self.memory.add_message("assistant", cc_reply, user_id, chat_id)
                        if self.proactive and chat_id:
                            self.proactive.record_user_response(chat_id)
                        return cc_reply
                    if cc_n is not None:
                        cc_verdict = "YES"
                    if cc_verdict == "YES":
                        self.computer_control.pick_choice(
                            cc_pending, (cc_n or 1) - 1)
                if cc_verdict == "YES":
                    self.computer_control.stats["confirmed"] += 1
                    # Pending снимаем ДО исполнения: иначе следующее «да»
                    # (на любой другой вопрос) исполнит действие повторно
                    self.computer_control.clear_pending(chat_id)
                    # Источник для аудита: исполнено по «да»; откуда пришло
                    # действие (fast/marker/…) — в pending_from
                    if cc_pending.get("origin") not in (None, "pending"):
                        cc_pending["pending_from"] = cc_pending["origin"]
                    cc_pending["origin"] = "pending"
                    for _it in cc_pending.get("items") or ():
                        if isinstance(_it, dict):
                            _it["origin"] = "pending"
                    if cc_pending.get("kind") == "scenario" and self.scenario_manager:
                        # «Запустить сценарий «X»?» (имя лишь встретилось во
                        # фразе) — «да» запускает сценарий
                        try:
                            cc_reply = self.scenario_manager.start(
                                str(cc_pending.get("name") or ""), chat_id,
                                self.router)
                        except Exception as e:
                            logger.warning(f"[Scenarios] запуск по «да» упал: {e}")
                            self.scenario_manager.cancel(chat_id)
                            from app.features import cc_texts
                            cc_reply = cc_texts.t("scenario_broken", turn_lang,
                                                  err=str(e)[:80])
                        self.memory.add_message("user", user_input, user_id, chat_id, user_name)
                        self.memory.add_message("assistant", cc_reply, user_id, chat_id)
                        if self.proactive and chat_id:
                            self.proactive.record_user_response(chat_id)
                        return cc_reply
                    if is_goal_task(cc_pending) and self.task_agent:
                        # «Берусь за задачу «X»?» (цель от LLM-яруса) — «да»
                        # запускает агента, а не execute
                        return self._task_agent_turn(
                            user_input, user_id, chat_id, user_name,
                            turn_lang, goal=str(cc_pending.get("goal") or ""),
                            announce=False)
                    # «да» владельца pending — источник токена подтверждения:
                    # без него гейт execute рискованное действие не исполнит
                    _grant = getattr(self.computer_control,
                                     "grant_confirmation", None)
                    if callable(_grant):
                        _grant(cc_pending, "pending", by=user_id)
                    cc_ok, cc_detail = self.computer_control.execute(
                        cc_pending, chat_id, router=self.router)
                    from app.features import cc_texts
                    # Маршрут дошёл до рискованного элемента, которого не было
                    # в вопросе, — стоп на нём и новый вопрос с его подписью
                    cc_reply = self._cc_gate_ask(cc_pending, chat_id,
                                                 turn_lang, user_id=user_id)
                    if cc_reply is None:
                        cc_reply = self._cc_reply(
                            cc_pending, cc_ok, cc_detail,
                            cc_texts.t("done", turn_lang, what=self.computer_control.describe_done(
                                cc_pending, lang=turn_lang))
                            if cc_ok else
                            cc_texts.t("failed", turn_lang, what=self.computer_control.describe(
                                cc_pending, lang=turn_lang), detail=cc_detail), lang=turn_lang)
                    if cc_ok and cc_pending.get("rest_steps"):
                        # Составная команда: хвост шагов — после этого «да»
                        cc_reply = self._cc_run_steps(
                            list(cc_pending["rest_steps"]), chat_id, turn_lang,
                            user_input=user_input, done=[cc_reply],
                            page_site=cc_pending.get("chain_site"))
                elif cc_verdict == "NO":
                    self.computer_control.stats["declined"] += 1
                    self.computer_control.clear_pending(chat_id)
                    from app.features import cc_texts
                    cc_reply = cc_texts.t("declined", turn_lang)
                    # «стоп»/«хватит» при идущем листании — это и отказ, и
                    # остановка: иначе страница крутилась бы дальше
                    if self.computer_control.stop_scroll_if_active(
                            raw_user_text or "", chat_id=cc_mode_key):
                        cc_reply = cc_texts.t("declined_scroll", turn_lang)
                if cc_reply is not None:
                    self.memory.add_message("user", user_input, user_id, chat_id, user_name)
                    self.memory.add_message("assistant", cc_reply, user_id, chat_id)
                    if self.proactive and chat_id:
                        self.proactive.record_user_response(chat_id)
                    return cc_reply
                # UNKNOWN — не согласие: вопрос, болтовня или новая команда
                # («ок, а теперь нажми войти»). Отложенное действие больше
                # не актуально — снимаем, сообщение идёт обычным потоком
                self.computer_control.clear_pending(chat_id, user_id=user_id)

            # «ещё» / «покажи остальное» — досылка остатка полностраничного
            # альбома («покажи всю страницу» режется на партии по 10).
            # Только при живом остатке (TTL 10 мин) — иначе бытовое «ещё»
            # уходит обычным путём
            _more_ph = self._pending_more_photos.get(str(chat_id))
            if _more_ph and _MORE_PHOTOS_RE.match(raw_user_text or ""):
                if time.time() - float(_more_ph.get("ts") or 0) > 600:
                    self._pending_more_photos.pop(str(chat_id), None)
                else:
                    _rest = _more_ph["photos"]
                    _batch, _rest = _rest[:10], _rest[10:]
                    if _rest:
                        _more_ph["photos"] = _rest
                        _more_ph["ts"] = time.time()
                    else:
                        self._pending_more_photos.pop(str(chat_id), None)
                    self._pending_photos.setdefault(
                        str(chat_id), []).extend(_batch)
                    from app.features import cc_texts
                    _mr = cc_texts.t("page_more_next" if _rest
                                     else "page_more_last", turn_lang,
                                     n=len(_batch))
                    self.memory.add_message("user", user_input, user_id,
                                            chat_id, user_name)
                    self.memory.add_message("assistant", _mr, user_id,
                                            chat_id)
                    if self.proactive and chat_id:
                        self.proactive.record_user_response(chat_id)
                    return _mr

            # Лесенка команд управления: нормализованный текст ТОГО, что
            # человек написал (не OCR/файл), составная команда — шагами
            # (_cc_fast_path → _cc_run_steps → _cc_ladder)
            cc_fp = self._cc_fast_path(cc_text, user_input, user_id, chat_id,
                                       user_name, turn_lang)
            if cc_fp is not None:
                return cc_fp
            # «что находится в X?» / «что в разделе X?» — вопрос о содержимом
            # секции открытой страницы: текст секции читаем со страницы и
            # подаём в общий LLM-поток контекстом — список формулирует модель.
            # Не fast-path ответ: return тут нет. Секция не нашлась / страницу
            # не открывали — молча обычный диалог (вопрос мог быть не о странице)
            if self.computer_control.click:
                cc_pq = (parse_page_question(raw_user_text)
                         or parse_page_question(cc_text))
                if cc_pq:
                    try:
                        _pq = self.computer_control.read_page_section(*cc_pq)
                    except Exception as e:
                        logger.debug(f"[CompControl] чтение секции страницы не удалось: {e}")
                        _pq = None
                    if _pq:
                        _pq_text, _pq_host, _pq_q = _pq
                        logger.info(f"[CompControl] секция «{_pq_q}» прочитана "
                                    f"({len(_pq_text)} симв., {_pq_host})")
                        page_section_note = (
                            f"The user asked about the \"{_pq_q}\" section of the web page "
                            f"currently open in the browser ({_pq_host}). Here is the section's "
                            f"actual content, read live from the page just now:\n---\n"
                            f"{_pq_text}\n---\n"
                            "Answer STRICTLY from this content: list the items (with prices, "
                            "if shown). Do not invent items that are not listed there — if the "
                            "user expects something that is missing, say the section does not "
                            "show it right now.")
            # не резолвится — обычный путь через LLM

        # Ответ на переспрос «Записать «X» в список дел?» / «Добавить «X» в
        # инвентарь?»: «да» — делаем без LLM, «нет» — снимаем; другая
        # реплика снимает вопрос молча и идёт обычным путём
        offer_reply = self._list_offer_turn(
            list_offer, raw_user_text, chat_id, cc_mode_key, turn_lang,
            reply_to_bot_message_id)
        if offer_reply is not None:
            self.memory.add_message("user", user_input, user_id, chat_id, user_name)
            self.memory.add_message("assistant", offer_reply, user_id, chat_id)
            if self.proactive and chat_id:
                self.proactive.record_user_response(chat_id)
            return offer_reply

        # Английским эвристикам (правило, «call me X») — написанное без
        # обращения в начале: в веб-чате имя не срезано («connor call me
        # max»). Русские идут по всему вводу, как раньше
        typed_h = self._strip_address(raw_user_text)

        # «Запомни сценарий …» вне режима управления: сценарии живут только
        # в режиме, правилом навсегда (исправление → Rule ниже) это стать не
        # должно. Только для фраз, которые и правда ушли бы в извлечение
        # правила: «запиши сценарий ролика», «remember the scenario where…» —
        # обычные просьбы. Допущенному к режиму — подсказка, где сценарии
        # записываются; для остальных режима «не существует» — обычный ответ
        # без правила. «В режиме» — для того, кого режим обслуживает:
        # недопущенный в чате с включённым режимом его блок не проходит
        scenario_outside_mode = bool(
            _looks_like_correction(user_input, typed_h)
            and self._is_scenario_save_command(raw_user_text)
            and not (cc_mode_key and self.control_mode_on(cc_mode_key)
                     and self._cc_allowed(user_id, chat_id)))
        if (scenario_outside_mode and getattr(self, "computer_control", None)
                and getattr(self, "scenario_manager", None)
                and self._cc_allowed(user_id, chat_id)):
            from app.features import cc_texts
            sc_hint = cc_texts.t("scenario_outside_mode", turn_lang)
            self.memory.add_message("user", user_input, user_id, chat_id, user_name)
            self.memory.add_message("assistant", sc_hint, user_id, chat_id)
            if self.proactive and chat_id:
                self.proactive.record_user_response(chat_id)
            return sc_hint

        # Берём историю до добавления нового сообщения — для контекста rewriter'а
        history_for_rewrite = self.memory.stm.get_last(_REWRITE_HISTORY, chat_id=chat_id)
        logger.info(f"[BotInstance] history_for_rewrite: {len(history_for_rewrite)} messages")

        # Серия подряд идущих ответов бота с финальным вопросом (conversation_style):
        # по истории ДО текущего сообщения; ниже решает, нужна ли регенерация
        from app.features.conversation_style import count_question_streak
        question_streak = count_question_streak(history_for_rewrite)

        # Живой контекст персоны (state/world): считаем ДО добавления сообщения
        # в STM — по последней реплике истории ещё видна пауза отсутствия
        # пользователя, и приветствие-дневник собирается честно
        living_context = None
        if self.living is not None and chat_id:
            living_context = self._build_living_context(
                chat_id, history_for_rewrite, user_message=user_input)

        # Стилевой модификатор помощи по intellect tier — опциональный аддон
        # (features.side_tasks.help_detect): при выключенном флаге LLM-детекция
        # не запускается и локальная LLM не дёргается на каждое сообщение
        help_style_future = side_tasks.submit_help_style_if_enabled(self, user_input)

        # Переписываем запрос: разрешаем местоимения и анафору — опциональный
        # аддон (features.side_tasks.query_rewrite); выключен — исходный текст
        persona_context = self._get_persona_context_for_search()
        ru_rewritten = side_tasks.rewrite_query_if_enabled(
            self, user_input, history_for_rewrite, persona_context=persona_context
        )
        # Реплика в лог — как START: в режиме управления только длина
        # (пароль без слова-секрета: «ivan / Kotik2019!»), иначе без секретов
        _log_hide = control_mode_active(self, chat_id or user_id)
        logger.info("[BotInstance] rewrite_query: "
                    + input_for_log(user_input, _log_hide) + " -> "
                    + input_for_log(ru_rewritten, _log_hide))

        # Лёгкий режим: отвечать будет локальная модель ИЛИ флаг
        # features.light_context принудительно включён в YAML персоны — слабая
        # модель тонет в большом промпте, поэтому урезаем всё необязательное
        # (короткая история, минимум фактов, без RAG/веба/self-memory/файлов).
        light_mode = self.router.is_local_primary() or self.features.get("light_context") is True
        if light_mode:
            logger.info(
                "[BotInstance] light-режим контекста "
                f"(local-провайдер: {self.router.is_local_primary()}, "
                f"features.light_context: {self.features.get('light_context') is True})"
            )

        # Запускаем веб-поиск в фоне (параллельно с памятью)
        # QueryEnhancer преобразует запрос в короткую поисковую форму через LLM —
        # только при включённом аддоне features.side_tasks.search_query_enhance,
        # иначе поиск идёт сырым ru_rewritten
        web_future = None
        # При прочитанной секции страницы веб-поиск не нужен: ответ целиком
        # в живом тексте секции, поисковая выдача только сместит фокус ответа
        # Гейт уместности: обращение по имени, реплика-связка или вопрос о
        # самой персоне («что сегодня делал?») внешних данных не требуют —
        # поиск по имени персоны как по обычному слову увёл бы фокус ответа
        # на выдачу (SOURCE PRIORITY) вместо дневника персоны
        search_skip = search_skip_reason(user_input, self._address_names())
        if search_skip and getattr(self, "_web_search_enabled", False):
            logger.info(f"[BotInstance] веб-поиск пропущен ({search_skip}): "
                        + input_for_log(user_input, control_mode_active(
                            self, chat_id or user_id)))
        if (self._web_search_enabled and chat_id not in self._web_search_disabled_chats
                and not self._is_docs_only_request(user_input)
                and search_skip is None
                and page_section_note is None):
            # Собираем контекст персоны для QueryEnhancer
            persona_context = self._get_persona_context_for_search()
            # Берём последние 6 сообщений для контекста
            history_for_search = self.memory.stm.get_last(6, chat_id=chat_id)
            search_args = (ru_rewritten, 5,
                           side_tasks.search_enhance_enabled(self), None,
                           history_for_search, persona_context,
                           side_tasks.translate_verify_enabled(self))
            # Пул контекст не копирует: область диалога (свой тред веб-чата
            # для улучшения запроса с историей чата) переносим явно
            _ctx = contextvars.copy_context()
            if self._web_race_enabled():
                # Гонка AI Mode + DDG-сниппеты (web_search_race): ~5 с
                # вместо 16-23 с у обычного поиска с загрузкой страниц
                web_future = self._web_pool.submit(_ctx.run, self._race_search,
                                                   search_args)
            else:
                web_future = self._web_pool.submit(_ctx.run, self._search_web,
                                                   *search_args)

        # Путь маркеров LLM (режим управления): реплика ложится в STM раньше,
        # чем process_markers поставит pending ввода — секреты из неё
        # маскируются заранее, по тексту (см. _cc_hist_note_user_text); email/
        # телефон обычной реплики — не секрет (contacts=False): маской он
        # станет, только если маркер введёт его в поле (_cc_hist_after_markers)
        if self.computer_control and chat_id and self.control_mode_on(chat_id):
            try:
                self._cc_hist_note_user_text(raw_user_text or user_input,
                                             contacts=False)
                if ru_rewritten != (raw_user_text or user_input):
                    self._cc_hist_note_user_text(ru_rewritten, contacts=False)
            except Exception as e:
                logger.warning(f"[CompControl] маска истории не поставлена: {e}")
        try:
            # В STM сохраняем переписанную русскую версию (с разрешёнными местоимениями)
            self.memory.add_message("user", ru_rewritten, user_id, chat_id, user_name,
                                    light_mode=light_mode)
            if light_mode:
                stm_messages, ltm_facts, stm_relevant = self.memory.get_context(
                    user_id, chat_id, ltm_query=ru_rewritten,
                    stm_recent_n=6, ltm_limit=3, stm_relevant_limit=0,
                )
            else:
                stm_messages, ltm_facts, stm_relevant = self.memory.get_context(
                    user_id, chat_id, ltm_query=ru_rewritten
                )
            file_context = None
            if self.file_db and not light_mode:
                if self._is_full_doc_request(user_input):
                    full_text = self.file_db.get_full_document(user_id)
                    if full_text:
                        file_context = f"Full text of the uploaded document:\n{full_text}"
                else:
                    file_chunks = self.file_db.search(user_id=user_id, query=user_input, limit=5)
                    if file_chunks:
                        file_context = "Context from uploaded files:\n" + "\n---\n".join(file_chunks)
            web_context = None
            if web_future is not None:
                try:
                    # search_web (LLM-enhance + поисковик + загрузка страниц) регулярно
                    # занимает больше 10с — при меньшем таймауте результат теряется
                    results = web_future.result(timeout=25)
                    if results:
                        web_context = self._format_web_results(results)
                        # В light-режиме веб-выдача (полные тексты страниц) без ограничения
                        # по размеру — режем жёстко, иначе слабая модель теряет нить.
                        if light_mode and web_context and len(web_context) > 1500:
                            web_context = web_context[:1500] + "\n[...truncated]"
                except FuturesTimeoutError:
                    web_future.cancel()
                    logger.info("  [WebSearch] Таймаут ожидания результатов, ищем без веба")
                except Exception:
                    pass
            context_parts_out = []
            if ltm_facts:
                context_parts_out.append("\n".join(ltm_facts))
            if file_context:
                context_parts_out.append(file_context)
            # В группе добавляем факты других участников, сказанные публично в этом чате
            if chat_id and str(chat_id) != str(user_id) and not light_mode:
                chat_facts_block = self.memory.get_chat_facts_block(chat_id, exclude_user_id=user_id)
                if chat_facts_block:
                    context_parts_out.append(chat_facts_block)

            # Исправления → правила: пользователь поправляет бота — формулируем
            # правило локальной LLM и сохраняем; оно запинится в промпт ниже.
            # «Запомни сценарий …» вне режима управления — не правило (см. выше)
            if _looks_like_correction(user_input, typed_h) and not scenario_outside_mode:
                rule = self._extract_rule_from_correction(user_input)
                if rule:
                    self.memory.ltm.save_facts(
                        f"Rule: {rule}", user_id, origin_chat=chat_id, user_name=user_name
                    )
                    logger.info(f"[Rules] Новое правило для {user_id}: {rule}")

            # «Зови меня X» / «call me X» — сохраняем как факт Name (UPDATE-категория
            # заменяет старый); «не зови меня X» — правило выше, имя не трогаем.
            # Английское — только из написанного (подписи), не из файла/OCR
            alias = _extract_alias(user_input, typed_h)
            if alias:
                self.memory.ltm.save_facts(
                    f"Name: {alias}", user_id, origin_chat=chat_id, user_name=user_name
                )
                logger.info(f"[Alias] {user_id} попросил называть его «{alias}»")

            # Правила от пользователя пиним ВСЕГДА, не полагаясь на семантический
            # поиск — иначе бот повторит ту же ошибку в другом контексте
            user_rules = self.memory.ltm.get_facts_by_category(user_id, "Rule", chat_id=chat_id)
            if user_rules:
                context_parts_out.append(
                    "Rules from the user (always follow them, they override habits):\n"
                    + "\n".join(f"  - {r}" for r in user_rules[-10:])
                )

            # Портрет из досье (интересы + стиль) — одна короткая строка, чтобы
            # автоанализ диалога работал и в обычных ответах, а не только когда
            # бот пишет первым
            if not light_mode and chat_id:
                try:
                    dossier_line = self._get_dossier_context_line(chat_id, user_id)
                    if dossier_line:
                        context_parts_out.append(dossier_line)
                except Exception as _de:
                    logger.debug(f"[Dossier] Строка контекста недоступна: {_de}")
            # Секция открытой страницы («что находится в X?») — реальный текст,
            # прочитанный со страницы в fast-path; ответ строится только из него
            if page_section_note:
                context_parts_out.append(page_section_note)
            memory_text = "\n\n".join(context_parts_out) if context_parts_out else None
            has_files = file_context is not None

            # Окружение пользователя (город, его локальное время, погода) — одна
            # строка, кешируется; добавляется и в light-режиме (она крошечная)
            env_context = None
            try:
                from app.features import env_context as _env_ctx
                env_context = _env_ctx.get_env_line()
            except Exception as _ee:
                logger.debug(f"[Env] Строка окружения недоступна: {_ee}")

            # Получаем блок личной памяти бота
            self_memory_block = None
            if self.self_memory and not light_mode:
                self_memory_block = self.self_memory.get_context_block()

            # Формируем блок релевантного STM-контекста
            stm_relevant_text = None
            if stm_relevant:
                parts = []
                for msg in stm_relevant:
                    role_ru = msg.get("user_name", "User") if msg["role"] == "user" else "Assistant"
                    ts = _format_msg_ts(msg.get("timestamp"))
                    ts_tag = f" [{ts}]" if ts else ""
                    parts.append(f"  {role_ru}{ts_tag}: {msg['content'][:200]}")
                stm_relevant_text = "\n".join(parts)

            # Reminder: перехватываем перед todo (напомни через N ...)
            reminder_context = None
            is_reminder_request = False
            # В режиме управления напоминания/дела/инвентарь молчат (ветки
            # ниже пропускаются) — честная пометка модели, иначе она
            # отвечает «напомню»/«записал», ничего не сделав
            if chat_id and self.control_mode_on(chat_id):
                reminder_context = self._cc_mode_feature_note(user_input)

            # Напоминания: ответы на висящие вопросы, отмена/список текстом,
            # новая просьба — см. _reminder_turn
            if self.reminder_manager and chat_id \
                    and not self.control_mode_on(chat_id):
                reminder_context, is_reminder_request = self._reminder_turn(
                    user_input, chat_id, user_id, user_name, reply_to_bot_message_id)

            # Learning-контекст: режим обучения («научи меня X»)
            learning_context = None
            is_learning_request = False
            # В режиме управления обучение молчит (как и остальные фичи-слова)
            if self.learning_manager and chat_id \
                    and not self.control_mode_on(chat_id):
                # Ответил ли пользователь reply-ом на один из последних «вопросов» бота
                # (частота уроков / «продолжаем?» / тест)? Это даёт обучению приоритет:
                # если да — сообщение трактуется как ответ на этот вопрос.
                reply_to_question = self.learning_manager.is_reply_to_question(chat_id, reply_to_bot_message_id)
                # Явно другая фича (напоминание/todo/инвентарь) — даже без reply у неё
                # приоритет над обучением, чтобы «напомни через 5 минут» не создавало
                # фейковый урок через parse_frequency("5 минут")=300с.
                explicit_other_feature = (
                    is_reminder_request
                    or is_inventory_add_request(user_input)
                    or is_inventory_remove_request(user_input)
                    or is_todo_request(user_input)
                    or is_todo_done_request(user_input)
                    or is_todo_list_request(user_input)
                )

                # Сбрасываем счётчик молчания — но только если сообщение не про другую
                # фичу (напоминание не должно сбрасывать молчание курса). Только
                # курсов автора: в группе чужие реплики чужой курс не держат.
                if not explicit_other_feature:
                    self.learning_manager.record_user_activity(chat_id, user_id)

                # 0. Просьба остановить обучение («хватит уроков», «хватит учить
                #    испанскому») или ответ на «какой курс остановить?» — раньше
                #    всего остального: иначе курс шёл бы вечно у того, кто пишет
                #    боту о другом (молчания нет — «продолжаем?» не спросят).
                _stop = None
                if not explicit_other_feature:
                    _stop = self.learning_manager.handle_stop_request(chat_id, user_id, user_input)
                # 1. Если ждём ответа на «как часто?» — парсим частоту.
                #    Чтобы не перехватывать напоминания/todo/инвентарь, активируемся только если:
                #    (a) это reply на бот-вопрос о частоте, ИЛИ
                #    (b) сообщение не похоже ни на какую другую фичу (explicit_other_feature=False).
                #    Без reply берём только явную частоту (parse_setup_answer):
                #    «я спал 5 часов» не должно создать курс раз в 5 часов.
                setup = self.learning_manager.get_setup_state(chat_id, user_id)
                _setup_delay, _setup_take = None, False
                if _stop is None and setup and setup.get("subject") and not explicit_other_feature \
                        and (reply_to_question or _looks_like_frequency_answer(user_input)):
                    _setup_delay, _setup_take = self.learning_manager.parse_setup_answer(
                        user_input, is_reply=reply_to_question)
                if _stop is not None:
                    skip_llm_answer = self.learning_manager.render_stop_reply(
                        _stop, user_language=turn_lang)
                    # «Какой остановить?» — вопрос: reply на него распознается
                    self._pending_question_kind[str(chat_id)] = \
                        "stop_choice" if _stop["kind"] == "which" else None
                    is_learning_request = True
                elif setup and not setup.get("subject") and not explicit_other_feature:
                    # «Научи меня» без темы — бот спросил «чему?». Короткий ответ
                    # (или reply) — это тема; иначе человек пишет о другом:
                    # вопрос снимаем, сообщение идёт в обычный ответ
                    subject = ""
                    if (reply_to_question or (len(user_input.split()) <= 5 and "?" not in user_input)) \
                            and not _NOT_A_TOPIC_RE.match(user_input.strip()):
                        subject = extract_subject(user_input)
                    if subject:
                        self.learning_manager.begin_setup(chat_id, subject, user_id or "default",
                                                          user_name or "User")
                        learning_context = (
                            f"The user wants you to teach them \"{subject}\". "
                            "Ask them briefly and in your own style how often to send lessons "
                            "(for example: once a day, every 2 hours). The course starts after their reply."
                        )
                        self._pending_question_kind[str(chat_id)] = "frequency"
                        is_learning_request = True
                    else:
                        self.learning_manager.clear_setup(chat_id, user_id)
                elif _setup_take:
                    topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                    subject = setup.get("subject", "")
                    delay = _setup_delay
                    if delay:
                        self.learning_manager.commit_session(chat_id, delay, topic_id, user_id=user_id)
                        delay_text = self.learning_manager.format_delay(delay)
                        # Изолированный вызов (без STM/истории) — см. docstring
                        # render_setup_reply про то, почему это не идёт через
                        # общий self.persona.prepare_messages(...). Язык триггерного
                        # сообщения передаём явно: в изолированном вызове реплик
                        # пользователя нет, иначе модель ответит на языке персоны.
                        skip_llm_answer = self.learning_manager.render_setup_reply(
                            subject, "confirmed", delay_text,
                            user_language=turn_lang)
                    else:
                        skip_llm_answer = self.learning_manager.render_setup_reply(
                            subject, "reask", user_language=turn_lang)
                    # Если частоту не поняли (reask) — бот переспрашивает, это «вопрос».
                    # Если поняли (confirmed) — это не вопрос, а подтверждение старта.
                    self._pending_question_kind[str(chat_id)] = "frequency" if not delay else None
                    is_learning_request = True
                else:
                    # 2/3. Один из параллельных курсов ждёт ответа — на тест ИЛИ на
                    # «продолжаем?». Но если сообщение явно про другую фичу (напоминание/
                    # todo/инвентарь) — не перехватываем, даём той фиче приоритет.
                    _pending = None
                    if not explicit_other_feature:
                        _pending = self.learning_manager.resolve_pending_target(chat_id, user_input)

                    _pending_handled = False
                    if _pending and _pending["_pending_kind"] == "continue":
                        # Ответ на «продолжаем?» принимаем двумя путями:
                        # (a) настоящий Telegram-reply на само сообщение с вопросом —
                        #     однозначный сигнал от пользователя, поэтому содержимое можно
                        #     разобрать умным классификатором (LLM, с OFFTOPIC-веткой);
                        # (b) обычное КОРОТКОЕ сообщение с однозначным да/нет по regex —
                        #     в личных чатах reply почти не используют, и без этого пути
                        #     вопрос висел бы до авто-остановки курса в _loop(), хотя человек
                        #     по сути ответил. Лимит длины важен: длинное сообщение, пусть
                        #     и начинающееся с «да», обычно несёт свой вопрос/тему — его
                        #     нельзя съедать ответом на «продолжаем?».
                        _session_id = _pending.get("session_id")
                        decision = None
                        if reply_to_question:
                            _smart = self.learning_manager.classify_continue_answer_smart(user_input)
                            # OFFTOPIC — это reply на вопрос, но не по теме: вопрос не
                            # трогаем, сообщение уходит в обычную обработку ниже.
                            if _smart != "OFFTOPIC":
                                decision = _smart
                        elif _is_plain_yes_no(user_input):
                            fast = classify_continue_answer(user_input)
                            if fast in ("YES", "NO"):
                                decision = fast
                        if decision:
                            self.learning_manager.resolve_continue(chat_id, decision, session_id=_session_id)
                            # Изолированный вызов (без STM/истории) — см. docstring
                            # render_continue_reply про то, почему это не идёт через
                            # общий self.persona.prepare_messages(...). Язык передаём
                            # явно — реплик пользователя в изолированном вызове нет.
                            skip_llm_answer = self.learning_manager.render_continue_reply(
                                chat_id, decision, session_id=_session_id,
                                user_language=turn_lang)
                            # При UNKNOWN бот переспрашивает «да/нет» — это «вопрос».
                            self._pending_question_kind[str(chat_id)] = "continue" if decision == "UNKNOWN" else None
                            is_learning_request = True
                            _pending_handled = True
                        # иначе — сообщение не про «продолжаем?»: падаем в обычную
                        # обработку ниже, вопрос остаётся висеть НЕТРОНУТЫМ
                    elif _pending and _pending["_pending_kind"] == "quiz":
                        _session_id = _pending.get("session_id")
                        feedback = self.learning_manager.submit_quiz_answer(chat_id, user_input, session_id=_session_id, is_reply=reply_to_question)
                        if feedback is not None:
                            # Реальная попытка ответить (пусть неверная) — фидбек уже
                            # сгенерирован в образе персоны в _evaluate_quiz. Возвращаем
                            # напрямую, минуя основной LLM-вызов, чтобы персонаж не
                            # «достроил» к оценке новый урок.
                            learning_context = None
                            skip_llm_answer = feedback
                            is_learning_request = True
                            _pending_handled = True
                        # feedback is None — офф-топ: ученик сменил тему, тест остаётся
                        # открытым. Сообщение уходит в обычную генерацию ответа, без
                        # пометки is_learning_request — персона ответит по сути вопроса.

                    if not _pending_handled:
                        # 4. Новая просьба об обучении — классификатор намерения
                        intent = side_tasks.classify_learning_intent_if_enabled(self, user_input)
                        if intent == "LEARN":
                            subject = extract_subject(user_input)
                            self.learning_manager.begin_setup(chat_id, subject, user_id or "default", user_name or "User")
                            if subject:
                                learning_context = (
                                    f"The user wants you to teach them \"{subject}\". "
                                    "Ask them briefly and in your own style how often to send lessons "
                                    "(for example: once a day, every 2 hours). The course starts after their reply."
                                )
                            else:
                                # «Научи меня» без темы — сначала «чему?» (ответ
                                # разберёт ветка настройки без темы выше)
                                learning_context = (
                                    "The user wants you to teach them something but did not say what. "
                                    "Ask them briefly and in your own style which topic they want to learn."
                                )
                            is_learning_request = True
                            # Ответ ниже — вопрос «как часто?»: отмечаем, чтобы telegram-слой
                            # зарегистрировал его message_id и reply пользователя распознался
                            # как ответ о частоте (иначе reply-gate сработает только на
                            # переспросах, а на первом вопросе — нет).
                            self._pending_question_kind[str(chat_id)] = "frequency"
                        else:
                            # 5. Сессия(и) активна, но это не команда/тест/setup/continue.
                            # Пользователь, скорее всего, отвечает на контрольные вопросы прошлого урока
                            # или просто пишет по теме. Запрещаем персоне самой продолжать курс:
                            # следующий урок придёт по расписанию как отдельный файл.
                            # Курсов может быть несколько параллельно — перечисляем все темы,
                            # т.к. get_session(chat_id) не возвращает одну сессию однозначно.
                            active_sessions = self.learning_manager.get_sessions(chat_id)
                            if active_sessions:
                                subjects = [s.get("subject", "") for s in active_sessions if s.get("subject")]
                                subjects_str = "«" + "», «".join(subjects) + "»" if subjects else ""
                                courses_note = (
                                    f"on the topic {subjects_str}" if len(subjects) == 1
                                    else f"on several topics at once: {subjects_str}"
                                )
                                learning_context = (
                                    f"A learning course is running in this chat {courses_note}. "
                                    "The user is writing within the learning conversation — possibly answering "
                                    "the previous lesson's review questions (on one of the topics) or discussing the topic. "
                                    "React ONLY to the user's message, in your own style. "
                                    "STRICT RULES:\n"
                                    "— DO NOT generate a new lesson, new topic, review questions or a quiz "
                                    "on any of the topics.\n"
                                    "— DO NOT mix study material into your reply — the next lesson "
                                    "will arrive later on schedule as a separate file message.\n"
                                    "— React briefly: answer/explain/comment, nothing more."
                                )
                                # is_learning_request здесь НАМЕРЕННО не ставим: само
                                # сообщение — обычный разговор при фоново активном курсе,
                                # а не учебно-административное действие (setup/continue/
                                # тест/новый курс — там флаг стоит). Гейты todo/inventory-
                                # маркеров ниже пропускают обработку при флаге, чтобы LLM
                                # не создавал сущности из учебных реплик; если выставить
                                # его на КАЖДОЕ сообщение при активном курсе, то «добавь
                                # в дела X» / «добавь в инвентарь Y» во время курса молча
                                # перестают работать, а сырые маркеры [TODO_ADD:...]
                                # утекают пользователю в ответ.
                                # get_nag_guard: персона сама (без инструкции) любит напоминать
                                # о незакрытых контрольных вопросах почти в каждом ответе —
                                # отсюда навязчивые «который раз за сессию», «паттерн
                                # подтверждён» и т.п. Кулдаун 4ч на курс: пока не истекло с
                                # последнего такого напоминания — явный запрет повторять;
                                # как истекло — ничего не добавляем, оставляя персоне свободу
                                # упомянуть (или нет) по своему усмотрению.
                                nag_guard = self.learning_manager.get_nag_guard(chat_id)
                                if nag_guard:
                                    learning_context += "\n\n" + nag_guard
                                # Изредка (кулдаун + вероятность внутри) добавляем к этой же
                                # инструкции подсказку органично закрепить пройденный материал —
                                # словом/фразой для языковых курсов или уместной отсылкой без
                                # объяснений для остальных тем. Не отдельный контекст, а
                                # дополнение к нему — иначе он никогда бы не сработал, ведь
                                # guard выше занимает learning_context на КАЖДОМ сообщении.
                                reinforcement = self.learning_manager.get_reinforcement_hint(chat_id)
                                if reinforcement:
                                    learning_context += "\n\n" + reinforcement

            # Todo-контекст: определяем, является ли запрос todo-запросом
            # Может работать параллельно с напоминанием (напр. "напомни через час X и добавь в список дел")
            todo_context = None
            # Список дел, который увидит модель: номера [TODO_DONE:N] и «вычеркни
            # 2» сверяются с ним, а не со списком на момент разбора ответа
            # (панель дел в вебе могла удалить пункт, пока шла генерация)
            todo_seen = None
            extracted_task = None
            extracted_done_index = None

            # Какие эвристики фич сработали на этом сообщении
            _fired_intents = set()
            if self.todo_manager and chat_id \
                    and not self.control_mode_on(chat_id):
                if is_todo_done_request(user_input):
                    _fired_intents.add("todo_remove")
                elif is_todo_list_request(user_input):
                    _fired_intents.add("todo_show")
                elif is_todo_request(user_input):
                    _fired_intents.add("todo_add")
            if (self.inventory_manager and not is_reminder_request
                    and not self.control_mode_on(chat_id)):
                if is_inventory_add_request(user_input):
                    _fired_intents.add("inventory_add")
                elif is_inventory_remove_request(user_input):
                    _fired_intents.add("inventory_remove")

            # Арбитр намерений: при конфликте триггеров («убери из списка дел задачу» —
            # todo_remove И inventory_remove одновременно) локальная LLM выбирает
            # одно намерение. Без конфликта классификатор не вызывается — бесплатно.
            if len(_fired_intents) > 1:
                winner = self._classify_intent(user_input, sorted(_fired_intents))
                if winner == "CHAT":
                    logger.info(f"[Intent] Конфликт {_fired_intents} → CHAT (локальная LLM)")
                    _fired_intents = set()
                elif winner:
                    logger.info(f"[Intent] Конфликт {_fired_intents} → {winner} (локальная LLM)")
                    _fired_intents = {winner}
                # локальная LLM недоступна — сработавшие эвристики остаются как есть

            if self.todo_manager and chat_id:
                if "todo_remove" in _fired_intents:
                    # Запрос на удаление/завершение дела
                    extracted_done_index = extract_todo_done_index(user_input)
                    todo_seen = self.todo_manager.get_tasks(chat_id)
                    current_todo = self.todo_manager.get_list(chat_id)
                    todo_context = current_todo or "The todo list is empty."
                elif "todo_show" in _fired_intents:
                    # Просьба показать список: только контекст со списком, без
                    # extracted_task — добавление не срабатывает, LLM показывает список
                    todo_seen = self.todo_manager.get_tasks(chat_id)
                    todo_context = self.todo_manager.get_list(chat_id) or "The todo list is empty."
                elif "todo_add" in _fired_intents:
                    extracted_task = extract_task(user_input)
                    if extracted_task:
                        extracted_task = self._reformulate_task(extracted_task)
                    todo_seen = self.todo_manager.get_tasks(chat_id)
                    current_todo = self.todo_manager.get_list(chat_id)
                    todo_context = current_todo or "The todo list is empty."

            # Inventory-контекст: вещи бота
            # (пропускаем если это напоминание — чтобы LLM не добавил мусор в инвентарь)
            inventory_context = None
            extracted_inventory_item = None
            extracted_inventory_remove = None
            inventory_events = []  # События для LLM-реакции (использование, просрочка)
            # Блок инвентаря несёт инструкцию маркеров — только там, где маркеры
            # разбираются (ниже: не напоминание и не учебно-административный
            # ход), иначе сырой [INVENTORY_ADD] утёк бы в ответ
            inventory_markers_on = not is_reminder_request and not is_learning_request
            if (not is_reminder_request and self.inventory_manager
                    and not (chat_id and self.control_mode_on(chat_id))):
                inv_block = self.inventory_manager.get_context_block()
                if inv_block and inventory_markers_on:
                    inventory_context = inv_block
                # Проверяем запрос на добавление/удаление
                if "inventory_add" in _fired_intents:
                    extracted_inventory_item = extract_inventory_item(user_input)
                elif "inventory_remove" in _fired_intents:
                    extracted_inventory_remove = extract_inventory_remove(user_input)

                # Проверяем, не сказал ли пользователь что бот использовал предмет
                # (например: "ты использовал X", "ты съел Y", "ты выпил Z", "давай съедим Z")
                used_item = self._extract_user_reported_usage(user_input)
                if used_item:
                    # Проверяем что предмет действительно есть в инвентаре
                    if self.inventory_manager.has_item(used_item):
                        result = self.inventory_manager.use_item(used_item)
                        inventory_events.append(f"The item '{used_item}' was used and is no longer in the inventory.")
                    else:
                        # Пробуем найти похожий предмет (по части названия)
                        found = self._find_inventory_item_by_substring(used_item)
                        if found:
                            result = self.inventory_manager.use_item(found)
                            inventory_events.append(f"The item '{found}' was used and is no longer in the inventory.")
                        else:
                            inventory_events.append(f"The user mentions using '{used_item}', but there is no such item in the inventory.")

                # Проверяем просроченные предметы
                expired = self.inventory_manager.remove_expired_items()
                for exp_name in expired:
                    inventory_events.append(f"The item '{exp_name}' has spoiled/expired and disappeared from the inventory.")

                # Обновляем контекст инвентаря после всех изменений
                inv_block = self.inventory_manager.get_context_block()
                if inv_block and inventory_markers_on:
                    inventory_context = inv_block

            # Ранний возврат: готовый ответ, минующий LLM (фидбек теста, не генерируем новый контент)
            if skip_llm_answer:
                answer = self._clean_response(skip_llm_answer)
                answer = self._save_assistant_reply(answer, user_id, chat_id)
                if self.proactive and chat_id:
                    self.proactive.record_user_response(chat_id)
                return answer

            # Предпочитаемое имя: последний факт категории Name заменяет
            # telegram-имя в форматировании — так работает «зови меня X»
            name_facts = self.memory.ltm.get_facts_by_category(user_id, "Name", chat_id=chat_id)
            if name_facts:
                preferred = name_facts[-1].partition(":")[2].strip()
                if preferred and len(preferred) <= 40:
                    user_name = preferred

            # Блоки аддонов персоны в промпт. В light-режиме аддоны не
            # вызываются: слабая модель тонет в большом промпте
            addon_results = {}
            addon_blocks = []
            if self.addons and not light_mode:
                turn = TurnInfo(user_input=user_input, history=stm_messages,
                                user_id=user_id, chat_id=chat_id,
                                persona_name=self.persona_name, context=self.context)
                for addon in self.addons:
                    try:
                        res = addon.build_context(turn)
                    except Exception as e:
                        logger.warning(f"[Addons] {addon.name}: build_context упал: {e}")
                        continue
                    addon_results[addon.name] = res
                    if res is not None and res.prompt_block:
                        addon_blocks.append(res.prompt_block)

            # Стилевой модификатор помощи по intellect tier: детекция
            # стартовала фоном в начале process_message — здесь только
            # забираем результат (обычно уже готов)
            help_style_block = None
            if help_style_future is not None:
                try:
                    help_style_block = help_style_future.result(timeout=10)
                except Exception as e:
                    logger.debug(f"[HelpStyle] модификатор не собран: {e}")

            # Платформенное правило финальных вопросов (conversation_style):
            # нота последней в системном блоке. Не подмешивается в учебные
            # сообщения — там вопросы пользователю часть механики курса
            from app.features.conversation_style import build_style_note
            conv_style_note = None
            if not learning_context:
                conv_style_note = build_style_note(self.conversation_style.frequency)

            # Computer control: инструкция о маркерах — только в режиме
            # управления (иначе LLM изображает «Открыл», ничего не открыв)
            # и только авторизованному: инструкция несёт хост/URL открытой
            # владельцем страницы (instruction_block), неавторизованному
            # её подмешивать нельзя — утечка того, что он открыл
            cc_prompt = None
            if self.computer_control and chat_id \
                    and self.control_mode_on(chat_id) \
                    and self._cc_allowed(user_id, chat_id):
                cc_prompt = self.computer_control.instruction_block(lang=turn_lang)

            messages = self.persona.prepare_messages(
                user_input, memory_text, history=stm_messages,
                user_id=user_id, user_name=user_name, web_context=web_context,
                has_files=has_files, self_memory_block=self_memory_block,
                reply_context=reply_context, stm_relevant=stm_relevant_text,
                todo_context=todo_context,
                reminder_context=reminder_context,
                inventory_context=inventory_context,
                inventory_events=inventory_events,
                learning_context=learning_context,
                addon_blocks=addon_blocks,
                env_context=env_context,
                living_context=living_context,
                help_style_context=help_style_block,
                conversation_style_context=conv_style_note,
                computer_control_context=cc_prompt
            )
            settings = self.persona.get_settings()
            # Когда есть учебный контекст (анонс/пересказ урока, фидбек) — ответ выходит
            # длиннее обычной реплики, для которой рассчитан persona.get_settings(). Берём
            # более щедрый max_tokens, чтобы не упираться в лимит и не дёргать догенерацию.
            if learning_context:
                settings = dict(settings)
                settings["max_tokens"] = max(int(settings.get("max_tokens", 2000)), 3000)
            if light_mode:
                # Локальная модель: длинная генерация — медленно и чаще «уезжает»,
                # ограничиваем размер ответа. Догенерация в light-режиме отключена
                # (модель дублирует реплику вместо продолжения): короткие обрывы
                # ловит гвард мусорных ответов ниже, а с num_ctx 8192 потолок
                # 1200 токенов не давит на контекст.
                settings = dict(settings)
                settings["max_tokens"] = min(int(settings.get("max_tokens", 2000)), 1200)
            # Стриминг (on_token задан): токены уходят подписчику сырыми,
            # финальный ответ после _clean_response/маркеров возвращается как обычно.
            # Веб/API сюда не передаёт on_token — /api/chat/stream сам «печатает»
            # финальный reply порциями, чтобы клиент не показывал сырой стрим.
            if on_token is not None:
                answer = self.router.get_response_stream(
                    messages, on_token, **settings)
            else:
                answer = self.router.get_response(
                    messages, **settings)
            if not answer:
                logger.error("Все LLM-провайдеры недоступны, ответ не сгенерирован")
                return "Сейчас все LLM-провайдеры недоступны. Попробуй позже."

            # Защита от обрыва по max_tokens (persona.get_settings() рассчитан на обычную
            # реплику; когда learning_context просит анонсировать/пересказать урок, ответ
            # выходит длиннее и может упереться в лимит) — просим модель дописать.
            # В light-режиме догенерация отключена: слабая локальная модель на повторном
            # заходе плодит варианты реплики вместо продолжения.
            # Для webchat — тоже отключена: «continue» засоряет непрерывный чат
            # служебными репликами (их видно в ленте), а веб-модель вместо строгого
            # продолжения выдаёт новую вариацию ответа — склейка даёт дубли.
            _webchat_answered = str(getattr(self.router, "_last_provider", "") or "").startswith("webchat")
            _continuations = 0
            while not (light_mode or _webchat_answered) and _looks_truncated(answer) and _continuations < 2:
                follow_up_messages = messages + [
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": "You stopped mid-sentence. Continue strictly from where you left off — do not repeat what was already written and do not start over. Continue in the same language as the reply."},
                ]
                cont = self.router.get_response(
                    follow_up_messages, **settings)
                if not cont:
                    break
                answer = answer + cont
                _continuations += 1

            # Очистка ответа от мета-рассуждений и Markdown
            answer = self._clean_response(answer)

            # Починка ответа аддонами до garbage-гарда (напр. маркер «[Ф1»,
            # оборванный лимитом длины, иначе ломает баланс скобок и гард
            # выбрасывает целиком валидный ответ)
            answer = self._addons_repair(answer, addon_results)

            # Страховка от оборванной/мусорной генерации (маленькие локальные
            # модели иногда выдают обрывки маркеров — «[», «[16.» — или пустоту):
            # один повторный заход, иначе нейтральная заглушка вместо мусора.
            def _is_garbage(t: str) -> bool:
                t = (t or "").rstrip()
                return (not re.search(r"[0-9A-Za-zА-Яа-яЁё]", t)
                        or t.count("[") != t.count("]")  # оборванный маркер
                        # короткий обрыв посреди слова («Анализ запроса. Тре») —
                        # нет знака завершения фразы. Только light-режим (там
                        # нет догенерации); в обычном короткая реплика без
                        # точки в конце — разговорный стиль, а не мусор
                        or (light_mode and len(t) < 60
                            and not _SENTENCE_END_RE.search(t)))
            if _is_garbage(answer):
                logger.warning(f"[BotInstance] Мусорный ответ ({answer!r}) — регенерация")
                retry = self.router.get_response(
                    messages, **settings)
                if retry:
                    answer = self._addons_repair(self._clean_response(retry), addon_results)
                else:
                    answer = ""
                if _is_garbage(answer):
                    answer = "Не удалось сформулировать ответ — попробуй переформулировать."

            # Предохранитель conversation_style: ответ закончился вопросом сверх
            # лимита серии — одна регенерация с усиленным напоминанием (модель
            # сама оставит вопрос, если он нужен по смыслу). Регенерированный
            # текст ниже проходит ту же обработку маркеров, что и обычный.
            # Учебные сообщения пропускаем — там вопросы часть механики курса.
            if not learning_context:
                from app.features.conversation_style import (
                    should_regenerate, regenerate_without_tail_question)
                if should_regenerate(self.conversation_style, answer, question_streak):
                    logger.info(
                        f"[ConvStyle] Финальный вопрос сверх лимита "
                        f"(streak={question_streak}, mode={self.conversation_style.frequency}) — регенерация")
                    new_answer = regenerate_without_tail_question(
                        self.router, messages, answer, settings, lang=turn_lang)
                    if new_answer:
                        answer = self._clean_response(new_answer) or answer

            # Финальная чистка ответа аддонами (напр. маркеры источников [ФN])
            for addon in self.addons:
                try:
                    answer = addon.postprocess(answer, addon_results.get(addon.name))
                except Exception as e:
                    logger.warning(f"[Addons] {addon.name}: postprocess упал: {e}")

            # Обработка todo-маркера
            # (пропускаем для учебно-административных сообщений — setup/continue/тест/
            # новый курс, там is_learning_request=True; обычный разговор при активном
            # курсе флаг не выставляет, и todo во время курса работает как обычно)
            if self.todo_manager and chat_id and todo_context and not is_learning_request:
                answer = self._process_todo_marker(
                    answer, chat_id, user_name or "User",
                    fallback_task=extracted_task,
                    fallback_done_index=extracted_done_index,
                    user_text=user_input, user_id=user_id, lang=turn_lang,
                    seen_tasks=todo_seen,
                )

            # Обработка inventory-маркеров (добавление/удаление/использование через маркеры)
            # (пропускаем если это напоминание или учебно-административное сообщение —
            # там LLM не должен добавлять в инвентарь; обычный разговор при активном
            # курсе сюда проходит — инвентарь во время курса работает как обычно)
            if inventory_markers_on and self.inventory_manager:
                answer = self._process_inventory_markers(
                    answer, extracted_inventory_item, extracted_inventory_remove,
                    user_name or "user", user_text=user_input, chat_id=chat_id,
                    user_id=user_id, lang=turn_lang)

            # Маркеры управления компьютером (open_url/open_app/run_task): срезка +
            # pending на подтверждение (или немедленное исполнение при confirm: false).
            # Те же пропуски, что у инвентаря — административные ответы маркеров не несут;
            # и только в режиме управления — иначе LLM-маркер не исполняем
            if (self.computer_control and chat_id
                    and self.control_mode_on(chat_id)
                    and not is_reminder_request and not is_learning_request):
                if self._cc_allowed(user_id, chat_id):
                    # Недоверенный текст в этом ходе (секция страницы, веб-
                    # выдача, файлы, цитата, OCR/документ в составном вводе):
                    # маркер мог подсказать он, а не человек — process_markers
                    # отбросит маркеры, если сам человек действия не просил
                    _cc_untrusted = bool(
                        page_section_note or web_context or file_context
                        or reply_context
                        or (raw_user_text or "") != (user_input or ""))
                    answer, cc_notices = self.computer_control.process_markers(
                        answer, chat_id, user_id=user_id,
                        untrusted=_cc_untrusted, user_text=raw_user_text,
                        lang=turn_lang)
                    try:
                        # Ввод маркера в поле логина/почты — маской и в уже
                        # записанной реплике этого хода
                        self._cc_hist_after_markers(chat_id, user_id)
                    except Exception as e:
                        logger.warning(f"[CompControl] маска ввода маркера "
                                       f"не поставлена: {e}")
                    for _cc_note in cc_notices:
                        self._pending_lists(chat_id).append(_cc_note)
                else:
                    # Неавторизованный: маркер — служебный сигнал системе, не текст
                    # для пользователя, поэтому вырезаем; но не исполняем НИКАК
                    # (ни сразу, ни через pending) — режим для него не существует
                    answer = MARKER_RE.sub("", answer)
                    answer = re.sub(r"\n{3,}", "\n\n", answer).strip()
            if self._punish_enabled:
                answer = self._parse_punishment(answer, user_id)

            # Автопредложение записать сценарий: закрывающая реплика
            # («спасибо»/«готово») после цепочки действий → один раз
            # предлагаем «запомни сценарий …». Только в режиме управления,
            # только авторизованному — иначе чужому «спасибо» подсказка
            # выдаст, что в чате есть режим управления.
            if (self.scenario_manager and chat_id
                    and self.control_mode_on(chat_id)
                    and self._cc_allowed(user_id, chat_id)):
                try:
                    _sc_offer = self.scenario_manager.maybe_offer(chat_id, user_input)
                    if _sc_offer:
                        answer = f"{answer}\n\n{_sc_offer}"
                except Exception as e:
                    logger.debug(f"[Scenarios] maybe_offer не удался: {e}")

            # Сохраняем ответ (при split_messages — по частям, хвост в pending)
            answer = self._save_assistant_reply(answer, user_id, chat_id)

            # Эпизодическая память (self_memory) — побочная LLM-запись,
            # пока веб-вкладка ЭТОГО чата активна счётчики заморожены
            # (у чата другой персоны/Telegram-чата — своя отметка, см. presence)
            if self.self_memory and not web_presence.is_active(
                    self.context, chat_id or user_id):
                self.self_memory.tick(stm_messages, user_id, user_input)

            # Мир персоны: детекция новых NPC/мест из диалога (в фоне)
            if self.living is not None and chat_id:
                try:
                    self.living.on_user_message(str(chat_id), stm_messages)
                except Exception as e:
                    logger.debug(f"[Living] Диалоговый тик не удался: {e}")

            # Обратная связь proactive: если ждём ответа на инициативу — фиксируем успех
            # (record_user_response сам обновляет досье — отдельный вызов
            # record_incoming_message здесь гнал бы счётчик анализа вдвое быстрее)
            if self.proactive and chat_id:
                self.proactive.record_user_response(chat_id)

            return answer
        except Exception as e:
            # Единая обработка сбоя пайплайна: реплика пользователя уже
            # записана в STM, поэтому любое необработанное исключение здесь
            # (например, в process_markers или в маркерах дел/инвентаря) не
            # должно долетать до вызывающего без ответа — иначе в истории
            # остаётся «вопрос без ответа», и следующий запрос уходит в
            # модель с рассинхронизированным контекстом. Пишем traceback в
            # лог и возвращаем понятную реплику-ошибку и в STM, и вызывающему;
            # индикаторы/локи снимает finally вызывающей стороны.
            logger.error(
                f"[BotInstance] Сбой пайплайна (chat {chat_id}): {e}",
                exc_info=True)
            return self._pipeline_failure_reply(user_id, chat_id)
        finally:
            # Ничего не делаем — пул живёт всё время жизни бота
            pass

    def _pipeline_failure_reply(self, user_id: str, chat_id) -> str:
        """Реплика-ошибка при сбое пайплайна: на языке пользователя, с записью
        в STM — чтобы в истории не осталось «вопроса без ответа».

        В STM пишем только если ответа там ещё нет: сбой мог случиться и ПОСЛЕ
        _save_assistant_reply (обратная связь proactive, living-тик) — тогда
        настоящий ответ уже сохранён, и второй записью историю портить нельзя."""
        lang = None
        try:
            lang = self.chat_user_language(chat_id)
        except Exception:
            pass
        text = ("Sorry, something broke while I was processing your message. "
                "Try again — or rephrase it."
                if lang == "en" else
                "Извини, у меня что-то сломалось при обработке сообщения. "
                "Попробуй ещё раз — или сформулируй иначе.")
        try:
            # Проверяем именно ТЕКУЩУЮ реплику: смотрим отметку answer_saved
            # в кадре текущего хода, а не последнюю запись STM — иначе за
            # ответ приняли бы чужую запись ассистента в хвосте (инициатива,
            # напоминание) или ответ на прошлую реплику.
            key = self.stm_key(chat_id, user_id)
            frame = self._get_turn_gate().current_frame(key)
            if frame is not None:
                tail = self._turn_stm_tail(key)
                # Реплика этого хода в STM не попала (сбой раньше записи) —
                # «вопроса без ответа» нет, одиночная ошибка историю не чинит.
                # Якорь потерян (tail None) — считаем, что реплика записана
                user_written = tail is None or any(
                    m.get("role") == "user" for m in tail)
                if user_written and not frame.get("answer_saved"):
                    self._save_assistant_reply(text, user_id, chat_id)
            else:
                last = self.memory.stm.get_last(1, chat_id=key)
                if not last or last[-1].get("role") != "assistant":
                    self._save_assistant_reply(text, user_id, chat_id)
        except Exception as e:
            logger.error(f"[BotInstance] Реплика-ошибка не записана в STM: {e}")
        return text

    def chat_user_language(self, chat_id: str) -> Optional[str]:
        """Язык пользователя чата по последним репликам STM ('ru'/'en'/None).
        Для служебных текстов вне LLM-пайплайна (список дел и т.п.)."""
        try:
            return detect_dialogue_language(
                "", self.memory.stm.get_last(8, chat_id=chat_id))
        except Exception:
            return None

    def _build_living_context(self, chat_id: str, history: List[Dict],
                              user_message: str = "") -> Optional[str]:
        """Собирает living-контекст для prepare_messages: приветствие-дневник
        при долгой паузе + текущее состояние персоны. Пауза считается по
        последней реплике истории ДО добавления текущего сообщения в STM.
        user_message — текущая реплика: последний факт жизни включается,
        только когда у него есть топическая зацепка (реактивная подача)."""
        if self.living is None:
            return None
        parts = []
        try:
            last_ts = None
            for msg in reversed(history or []):
                ts = msg.get("timestamp")
                if isinstance(ts, (int, float)) and ts > 0:
                    last_ts = float(ts)
                    break
            if last_ts:
                absence_h = (time.time() - last_ts) / 3600
                if absence_h >= 12:
                    entries = self.living.state_engine.entries_since(
                        chat_id, time.time() - absence_h * 3600)
                    return_ctx = self.living.summarizer.build_return_context(
                        chat_id, entries, absence_h,
                        user_language=detect_dialogue_language("", history))
                    if return_ctx:
                        parts.append(return_ctx)
                        self.living.state_engine.mark_consumed(
                            [e["id"] for e in entries])
            state_ctx = self.living.get_living_context(
                chat_id, topic_text=user_message)
            if state_ctx:
                parts.append(state_ctx)
        except Exception as e:
            logger.debug(f"[Living] Контекст не собран: {e}")
        return "\n\n".join(p for p in parts if p) or None

    def _reminder_turn(self, user_input: str, chat_id, user_id, user_name,
                       reply_to_bot_message_id=None) -> Tuple[Optional[str], bool]:
        """Напоминания в обычном сообщении → (контекст для LLM, про напоминания ли
        оно). Порядок: ответы на висящие вопросы («какое отменить?», «какое
        перенести?», «на когда перенести?») → отмена и список текстом → ответ на
        «когда напомнить?» → новая просьба. Отмена и список никогда не создают
        напоминание. Вопрос «когда?» живёт PENDING_REMIND_TTL_SEC и принадлежит
        тому, кого спросили; отказ, новая полная просьба или посторонняя реплика
        его снимают."""
        rm = self.reminder_manager
        reminder_context = None
        is_reminder_request = False
        pending_task = rm.get_pending_remind(chat_id, user_id)
        logger.info(f"[Reminder] pending_remind для chat={chat_id}: {pending_task!r}")

        # Конфликт ожиданий: одновременно висит вопрос обучения «как часто уроки?».
        # Ответ о периодичности («раз в день», «каждые 2 часа») принадлежит тому, кто
        # спросил ПОЗЖЕ — человек отвечает на последний заданный вопрос. Если свежее
        # setup обучения — уступаем: напоминание НЕ потребляем (остаётся pending,
        # на него можно ответить следующим сообщением), сообщение разберёт
        # learning-блок ниже. Без уступки напоминание съело бы такой ответ,
        # и setup курса завис бы навсегда.
        _yield_to_learning = False
        if pending_task and self.learning_manager:
            _setup = self.learning_manager.get_setup_state(chat_id, user_id)
            # Уступаем, только если обучение правда возьмёт ответ (та же
            # проверка, что в learning-блоке) — иначе сообщение не
            # досталось бы ни напоминанию, ни курсу
            _learn_reply = self.learning_manager.is_reply_to_question(
                chat_id, reply_to_bot_message_id)
            if _setup and _setup.get("subject") and (
                _learn_reply
                or (_looks_like_frequency_answer(user_input)
                    and self.learning_manager.parse_setup_answer(user_input)[1])
            ):
                _remind_at = rm.get_pending_remind_asked_at(chat_id) or 0
                _yield_to_learning = (_setup.get("asked_at") or 0) > _remind_at
                if _yield_to_learning:
                    logger.info(f"[Reminder] chat={chat_id}: ответ уступаю обучению (его вопрос свежее)")

        low = user_input.lower()
        cancel_req = parse_cancel_reminder(user_input)
        list_req = cancel_req is None and is_list_reminders_request(user_input)
        # Новая полная просьба («напомни через 5 минут купить хлеб») во время
        # «когда напомнить?» — новое напоминание, а не ответ: раньше время
        # бралось из неё, а текст — старый
        fresh_full = False
        if pending_task and ("напом" in low or re.search(r"\bremind", low)):
            _rec = parse_recurring(user_input)
            _parsed = parse_reminder(user_input)
            fresh_full = bool((_rec and _rec[0]) or (_parsed and _parsed[0]))

        # Ответ на «какое именно напоминание отменить?»
        if not _yield_to_learning and rm.get_pending_cancel_choice(chat_id, user_id):
            is_reminder_request = True
            result = rm.resolve_cancel_choice(chat_id, user_input)
            if result and result.get("gone"):
                reminder_context = _reminder_cancel_context({"none": True})
            elif result:
                reminder_context = _reminder_cancel_context(result)
            elif is_pending_decline(user_input):
                rm.clear_pending_remind(chat_id)
                reminder_context = ("The user decided not to cancel any reminder. Nothing was "
                                    "cancelled. Acknowledge briefly in your own style.")
            else:
                reminder_context = (
                    "The user is choosing which reminder to cancel, but the answer does not "
                    "match any of them. Ask again: reply with the number or words from the "
                    "task. In your own style, briefly. Do NOT say anything was cancelled."
                )

        # Ответ на «какое именно напоминание перенести?» (несколько активных,
        # подсказки не было — сдвиг уже запомнен в pending)
        elif not _yield_to_learning and rm.get_pending_postpone_choice(chat_id, user_id):
            is_reminder_request = True
            # Единый источник истины — reminder_manager: id кандидатов
            # и сдвиг хранятся там же, где заведены (begin_pending_
            # postpone_choice), тонкий вызов без своей логики разбора
            result = rm.resolve_postpone_choice(chat_id, user_input)
            if result and result.get("gone"):
                reminder_context = (
                    "The user was choosing which reminder to move, but there are no "
                    "active reminders anymore. Say there is nothing to move — "
                    "in your own style, briefly."
                )
            elif result:
                reminder_context = _postpone_result_context(result)
            else:
                reminder_context = (
                    "The user is choosing which reminder to move, but the answer does "
                    "not match any of them. Ask again: reply with the number or words "
                    "from the task. In your own style, briefly. "
                    "Do NOT say anything was moved."
                )

        # Ответ на «на когда перенести напоминание?» (перенос без времени).
        # Без этой ветки ответ ушёл бы в общий LLM, и модель могла бы
        # «подтвердить» перенос, который нигде не применён.
        elif not _yield_to_learning and rm.get_pending_postpone(chat_id, user_id):
            is_reminder_request = True
            shift = parse_postpone(f"перенеси напоминание {user_input}")
            p_delay = p_abs = None
            p_rel = False
            if shift and not shift.get("unknown"):
                p_delay = shift.get("seconds")
                p_abs = shift.get("abs")
                p_rel = bool(shift.get("relative_to_trigger"))
            if p_delay is None and p_abs is None:
                # Ответ вида «через 10 минут» / «в 18:00» / голое «18:30»
                parsed_shift = parse_reminder("напомни " + user_input)
                if not parsed_shift:
                    parsed_shift = parse_reminder("напомни в " + user_input)
                if parsed_shift:
                    _, p_delay = parsed_shift
                else:
                    p_delay = parse_frequency(user_input)
            if p_delay is not None or p_abs is not None:
                rm.clear_pending_remind(chat_id)
                # Подсказка задачи — только если пользователь переформулировал
                # весь запрос («перенеси напоминание приготовить еду на час»),
                # а не просто ответил на вопрос («на 15 минут» — там подсказки нет,
                # а extract вытащил бы мусор вроде «через час»).
                p_hint = (
                    extract_postpone_hint(user_input)
                    if parse_postpone(user_input) else None
                )
                reminder_context = self._postpone_handled_context(
                    chat_id,
                    rm.postpone_reminder(
                        chat_id, seconds=p_delay, abs_time=p_abs,
                        relative_to_trigger=p_rel,
                        task_hint=p_hint,
                    ),
                    seconds=p_delay, abs_time=p_abs, relative_to_trigger=p_rel,
                    user_id=user_id,
                )
            else:
                reminder_context = (
                    "The user is answering the question about when to move the "
                    "reminder to, but the time could not be understood. Ask again: "
                    "to what time should the reminder be moved "
                    "(for example, \"in 10 minutes\", \"at 18:30\")? "
                    "In your own style, briefly. "
                    "Do NOT say the reminder was moved — it was NOT."
                )

        # «Отмени напоминание (про X / 2 / все)», «какие у меня напоминания?» —
        # до создания: раньше любой текст с «напом» заводил новое напоминание
        elif cancel_req is not None or list_req:
            is_reminder_request = True
            if pending_task and cancel_req is not None and not cancel_req["all"] \
                    and not cancel_req["ref"] and not cancel_req["hint"]:
                # «отмени напоминание» в ответ на «когда напомнить?» — отказ
                # от создаваемого, а не отмена существующего
                rm.clear_pending_remind(chat_id)
                reminder_context = _reminder_declined_context(pending_task)
            else:
                if pending_task:
                    rm.clear_pending_remind(chat_id)  # тема сменилась — вопрос «когда?» снят
                if cancel_req is not None:
                    result = rm.cancel_request(chat_id, cancel_req)
                    if result.get("not_found"):
                        # Модель покажет список — номер дальше по нему
                        rm.note_listed(chat_id, result.get("active") or [])
                    if result.get("ambiguous"):
                        rm.begin_pending_cancel_choice(
                            chat_id, [r.get("id") for r in result["ambiguous"]], user_id=user_id)
                    reminder_context = _reminder_cancel_context(result)
                else:
                    active = rm.get_active(chat_id)
                    # Номер в «отмени напоминание 2» — строка этого списка
                    rm.note_listed(chat_id, active)
                    reminder_context = (
                        "The user asked which reminders they have. Active reminders: "
                        f"{_fmt_reminder_list(active)}. Present this list — numbered, tasks and "
                        "times exactly as given; you may mention a reminder can be cancelled by "
                        "its number. In your own style, briefly. Do NOT invent reminders."
                    ) if active else (
                        "The user asked which reminders they have. There are NO active "
                        "reminders. Say so briefly in your own style."
                    )

        elif pending_task and not _yield_to_learning and not fresh_full:
            if is_pending_decline(user_input):
                # «не надо» / «отмена» на «когда напомнить?»
                rm.clear_pending_remind(chat_id)
                reminder_context = _reminder_declined_context(pending_task)
                return reminder_context, True
            # Пытаемся вытащить время из ответа пользователя.
            # Сначала — повторяющееся расписание («каждый день в 12»).
            rec_pending = parse_recurring("напомни " + user_input)
            rem_delay = None
            if rec_pending:
                rec_task, rec_schedule = rec_pending
                rm.clear_pending_remind(chat_id)
                rem_task = self._reformulate_task(rec_task or pending_task)
                topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                rm.add_reminder(
                    chat_id, user_name or "User", rem_task, 0, topic_id,
                    schedule=rec_schedule,
                    user_id=user_id, username=get_username(user_id),
                )
                task_display = f" '{rem_task}'" if rem_task else ""
                reminder_context = (
                    f"The user asked to be reminded{task_display} — "
                    f"{format_schedule(rec_schedule)}. The reminder is scheduled — "
                    f"confirm this in your own style, briefly."
                )
                is_reminder_request = True
                parsed_pending = None
            else:
                parsed_pending = parse_reminder("напомни " + user_input)
                if not parsed_pending:
                    # Голый час («8», «18:30») — тот же фолбэк с предлогом,
                    # что и в ветке переноса напоминания
                    parsed_pending = parse_reminder("напомни в " + user_input)
            if not is_reminder_request and parsed_pending:
                _, rem_delay = parsed_pending
            if rem_delay is None and not is_reminder_request:
                # Пробуем парсер частоты из learning
                rem_delay = parse_frequency(user_input)
            if rem_delay:
                rm.clear_pending_remind(chat_id)
                rem_task = self._reformulate_task(pending_task)
                topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                delay_text = rm.format_delay(rem_delay)
                rm.add_reminder(
                    chat_id, user_name or "User", rem_task, rem_delay, topic_id,
                    user_id=user_id, username=get_username(user_id),
                )
                task_display = f" '{rem_task}'" if rem_task else ""
                reminder_context = (
                    f"The user specified the time for the reminder{task_display} — in {delay_text}. "
                    f"The reminder is scheduled — confirm this in your own style, briefly."
                )
                is_reminder_request = True
            elif not is_reminder_request:
                if looks_like_time_attempt(user_input):
                    # Похоже на время, но не поняли — переспрашиваем
                    reminder_context = (
                        f"The user is answering the question about the time for the reminder \"{pending_task}\", "
                        "but the time could not be understood. Ask again: how soon to remind "
                        "(for example, \"in 2 hours\", \"tomorrow at 12\", \"every day at 9\"). "
                        "In your own style, briefly. "
                        "Do NOT confirm that any reminder was scheduled — it was NOT."
                    )
                    is_reminder_request = True
                else:
                    # Посторонняя реплика — не ответ на «когда?»: вопрос снят,
                    # сообщение идёт обычным разговором
                    rm.clear_pending_remind(chat_id)
                    logger.info(f"[Reminder] chat={chat_id}: не ответ на «когда?» — "
                                f"вопрос про {pending_task!r} снят")

        # Новая просьба: «напомни …», «перенеси напоминание …». Отсюда —
        # и полная просьба, пришедшая во время вопроса «когда?» (fresh_full)
        elif "напом" in low or re.search(r"\bremind", low):
            if fresh_full:
                rm.clear_pending_remind(chat_id)
            is_reminder_request = True
            # Перенос существующего напоминания («перенеси/отложи/сдвинь
            # напоминание ...») — строго ДО обычного парсера: иначе весь текст
            # уедет в pending-задачу нового напоминания, реального переноса не
            # будет, а бот словами его «подтвердит».
            postpone = parse_postpone(user_input)
            if postpone and postpone.get("unknown"):
                rm.begin_pending_postpone(chat_id, user_id=user_id)
                reminder_context = (
                    "The user asked to move/reschedule a reminder, but did not say "
                    "to when. Ask: to what time should the reminder be moved "
                    "(for example, \"in 10 minutes\", \"at 18:30\")? "
                    "In your own style, briefly. Do NOT say anything was rescheduled yet."
                )
            elif postpone:
                reminder_context = self._postpone_handled_context(
                    chat_id,
                    rm.postpone_reminder(
                        chat_id,
                        seconds=postpone.get("seconds"),
                        abs_time=postpone.get("abs"),
                        relative_to_trigger=postpone.get("relative_to_trigger", False),
                        task_hint=extract_postpone_hint(user_input),
                    ),
                    seconds=postpone.get("seconds"),
                    abs_time=postpone.get("abs"),
                    relative_to_trigger=postpone.get("relative_to_trigger", False),
                    user_id=user_id,
                )
            # Сначала — повторяющееся расписание («каждый день в 9», «по пятницам в 18:00»)
            rec = parse_recurring(user_input) if not postpone else None
            if rec:
                rem_task, rec_schedule = rec
                if rem_task:
                    rem_task = self._reformulate_task(rem_task)
                topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                rm.add_reminder(
                    chat_id, user_name or "User", rem_task, 0, topic_id,
                    schedule=rec_schedule,
                    user_id=user_id, username=get_username(user_id),
                )
                task_display = f" '{rem_task}'" if rem_task else ""
                reminder_context = (
                    f"The user asked to be reminded{task_display} — {format_schedule(rec_schedule)}. "
                    f"The reminder is already scheduled — just confirm this in your own style, briefly."
                )
            elif not postpone:
                parsed = parse_reminder(user_input)
                logger.info(f"[Reminder] parse_reminder({user_input[:60]!r}) -> {parsed}")
                if parsed:
                    rem_task, rem_delay = parsed
                    # Переформулирование задачи через LLM
                    if rem_task:
                        rem_task = self._reformulate_task(rem_task)
                    topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                    delay_text = rm.format_delay(rem_delay)
                    rm.add_reminder(
                        chat_id, user_name or "User", rem_task, rem_delay, topic_id,
                        user_id=user_id, username=get_username(user_id),
                    )
                    task_display = f" '{rem_task}'" if rem_task else ""
                    reminder_context = (
                        f"The user asked to be reminded{task_display} in {delay_text}. "
                        f"The reminder is already scheduled — just confirm this in your own style, briefly."
                    )
                elif is_reminder_create_request(user_input):
                    # Время не указано — переспрашиваем и ЗАПОМИНАЕМ задачу (как в /remind),
                    # иначе следующее сообщение "через 10 минут" не с чем будет связать.
                    rem_task = self._reformulate_task(user_input)
                    rm.begin_pending_remind(chat_id, rem_task, user_id=user_id)
                    reminder_context = (
                        f"The user asked to be reminded \"{rem_task}\", but did not specify how soon. "
                        "Ask when to remind them — in your own style, briefly."
                    )
                else:
                    # «напоминание пришло вовремя», «ты мне напомнил?» — разговор
                    # о напоминаниях, а не просьба: обычный ответ
                    is_reminder_request = False
        return reminder_context, is_reminder_request

    def _postpone_handled_context(self, chat_id: str, result: Optional[dict],
                                  seconds: Optional[float] = None,
                                  abs_time: Optional[tuple] = None,
                                  relative_to_trigger: bool = False,
                                  user_id: Optional[str] = None) -> str:
        """LLM-контекст после попытки переноса напоминания.
        ambiguous — запоминаем сдвиг и спрашиваем КАКОЕ напоминание двигать
        (показываем нумерованный список); not_found — такого нет, ничего не
        двинуто; остальное — стандартное подтверждение/отказ."""
        if result and result.get("ambiguous"):
            # id кандидатов — в показанном порядке: reminder_manager хранит
            # их вместе со сдвигом (единый источник истины) и по ним же
            # разберёт ответ в resolve_postpone_choice, даже если список
            # активных успеет измениться до ответа
            self.reminder_manager.begin_pending_postpone_choice(
                chat_id, ids=[c.get("id") for c in result["choices"]],
                seconds=seconds, abs_time=abs_time,
                relative_to_trigger=relative_to_trigger, user_id=user_id,
            )
            choices = _fmt_reminder_choices(result["choices"])
            return (
                "No reminder was moved. NOTHING was rescheduled. "
                f"There are several active reminders: {choices}. "
                "Your reply MUST be a question asking which one to move, "
                "showing the numbered list above. "
                "In your own style, briefly."
            )
        if result and result.get("not_found"):
            choices = _fmt_reminder_choices(result.get("choices") or [])
            listing = f" Active reminders: {choices}." if choices else ""
            return (
                "No reminder was moved. NOTHING was rescheduled. "
                f"The user asked to move a reminder matching \"{result.get('hint')}\", "
                f"but no reminder matches it.{listing} "
                "Say that no such reminder was found (mention what does exist, if anything). "
                "In your own style, briefly."
            )
        return _postpone_result_context(result)

    def _reformulate_task(self, raw_task: str) -> str:
        """
        Очищает сырой текст задачи через локальную LLM.
        'что пора купить хлеб' -> 'Купить хлеб'
        'мне сделать апдейт' -> 'Сделать апдейт'
        """
        if not raw_task or len(raw_task.strip()) < 2:
            return raw_task

        if not self._local_router or not self._local_router.is_available(task="todo_cleanup"):
            return raw_task.strip()

        try:
            response = self._local_router.get_response(
                messages=[
                    {"role": "system", "content": (
                        "Clean up the task text: remove pronouns and clutter. "
                        "The answer is only the short task text, phrased as an infinitive. "
                        "STRICTLY keep the language of the original text: "
                        "do NOT translate — Russian stays Russian, English stays English.\n"
                        + user_language_line(detect_language(raw_task))
                    )},
                    {"role": "user", "content": raw_task.strip()},
                ],
                temperature=0.0,
                max_tokens=60,
                task="todo_cleanup",
            )

            if response:
                cleaned = response.strip().strip('"\'""«»')

                # Жёсткая валидация — локальная модель часто возвращает мусор
                # 1. Не длиннее исходного + 20 символов
                if len(cleaned) > len(raw_task) + 20:
                    logger.info(f"[Task] Переформулирование отклонено (длиннее оригинала): '{cleaned[:60]}'")
                    return raw_task.strip()

                # 2. Не длиннее 100 символов
                if len(cleaned) > 100:
                    logger.info(f"[Task] Переформулирование отклонено (слишком длинный): '{cleaned[:60]}'")
                    return raw_task.strip()

                # 3. Не содержит слов из системного промпта (модель эхо)
                _FORBIDDEN_WORDS = (
                    "clean up", "pronoun", "clutter", "infinitive",
                    "only the short", "task text", "short task",
                    "rephrase", "meta-note", "markdown",
                )
                lower = cleaned.lower()
                for word in _FORBIDDEN_WORDS:
                    if word in lower:
                        logger.info(f"[Task] Переформулирование отклонено (эхо промпта): '{cleaned[:60]}'")
                        return raw_task.strip()

                # 4. Минимум 2 символа
                if len(cleaned) >= 2:
                    logger.info(f"[Task] Переформулировано: '{raw_task}' -> '{cleaned}'")
                    return cleaned

        except Exception as e:
            logger.debug(f"[Task] Переформулирование не удалось: {e}")

        return raw_task.strip()

    def _extract_rule_from_correction(self, user_text: str) -> Optional[str]:
        """
        Если реплика — исправление бота или просьба запомнить правило/предпочтение,
        формулирует короткое правило через ЛОКАЛЬНУЮ LLM. Иначе возвращает None.
        """
        if not self._local_router or not self._local_router.is_available(task="rule_extract"):
            return None
        try:
            resp = self._local_router.get_response(
                messages=[
                    {"role": "system", "content": (
                        "Determine whether the message is a correction of the bot or a request to remember "
                        "a rule/preference (how to address the user, what to do or not do). "
                        "If yes — formulate the rule as ONE short sentence (up to 12 words), "
                        "without explanations or quotes. If it is ordinary conversation or a question — answer exactly NO.\n"
                        + user_language_line(detect_dialogue_language(user_text))
                    )},
                    {"role": "user", "content": user_text[:400]},
                ],
                temperature=0.0, max_tokens=60,
                task="rule_extract",
            )
            if not resp:
                return None
            rule = resp.strip().strip('"\'""«»').strip()
            if not rule or rule.upper().startswith("NO") or not (5 <= len(rule) <= 150):
                return None
            return rule
        except Exception as e:
            logger.debug(f"[Rules] Извлечение правила не удалось: {e}")
            return None

    def _classify_intent(self, user_text: str, candidates: list) -> Optional[str]:
        """
        Арбитр намерений при конфликте эвристик: локальная LLM выбирает ОДНО
        намерение из candidates (snake_case: todo_add, inventory_remove...).
        Возвращает winner (snake_case), "CHAT" (ничего не подходит) или None
        (локальная модель недоступна — вызывающий оставляет все сработавшие эвристики).
        """
        if not self._local_router or not self._local_router.is_available(task="intent_router"):
            return None

        intent_desc = {
            "todo_add": "TODO_ADD — write down a new task in the todo list",
            "todo_remove": "TODO_REMOVE — remove/cross out a task from the todo list",
            "todo_show": "TODO_SHOW — show/read the current todo list",
            "inventory_add": "INVENTORY_ADD — give/hand an item to the bot for its inventory",
            "inventory_remove": "INVENTORY_REMOVE — take away/discard an item from the bot's inventory",
        }
        valid_outputs = [c.upper() for c in candidates]
        options_text = "\n".join(f"- {intent_desc[c]}" for c in candidates)

        verdict = self._local_router.classify(
            system_prompt=(
                "You are an intent classifier. Determine what the user is asking to do.\n"
                f"Options:\n{options_text}\n"
                "- CHAT — ordinary conversation, none of the above.\n"
                "Pay attention to the object of the action: \"todo list\" is TODO, \"inventory/you have/to you\" is INVENTORY. "
                "Answer with one word.\n"
                + user_language_line(detect_dialogue_language(user_text))
            ),
            user_prompt=f"User message: \"{user_text}\"",
            valid_outputs=valid_outputs + ["CHAT"],
            temperature=0.0,
            max_tokens=10,
            task="intent_router",
        )
        if not verdict:
            return None
        if verdict == "CHAT":
            return "CHAT"
        return verdict.lower()

    def _confirm_intent(
        self, user_text: str, candidate: str, intent: str
    ) -> str:
        """
        Подтверждает через локальную LLM, что эвристически извлечённый кандидат —
        это явная просьба пользователя, а не огрызок из обычной реплики.

        intent: 'inventory_add' | 'inventory_remove' | 'todo_add'.
        Возвращает:
          'ADD'  — локальная LLM подтвердила намерение;
          'SKIP' — локальная LLM отклонила;
          'ASK'  — локальная LLM недоступна → переспросить пользователя.
        """
        if not self._local_router or not self._local_router.is_available(task="intent_router"):
            logger.info(f"[Intent] Локальная LLM недоступна, переспрос для '{candidate[:40]}'")
            return "ASK"

        intent_desc = {
            "inventory_add": "add an item to the character's inventory",
            "inventory_remove": "discard/remove an item from the inventory",
            "todo_add": "write down a task in the todo list",
            "todo_remove": "mark a task as done and remove it from the todo list",
        }.get(intent, "perform an action")

        system_prompt = (
            "You are an intent classifier. Determine whether the user EXPLICITLY asks to "
            f"{intent_desc}, or it is just a remark/story.\n"
            "RULES:\n"
            "- The user hands an item to the character (\"here's X\", \"take X\", \"this X is for you\", "
            "\"put on X\", \"I'm giving you X\"; in Russian: «держи X», «возьми X», «вот тебе X», "
            "«надень X», «дарю X») — this is ADD.\n"
            "- A story about the past (\"I bought X\", \"I was given X\", \"I got X\"; in Russian: "
            "«я купил X», «мне подарили X», «получил X») — this is SKIP.\n"
            "- The candidate has already been extracted from the message automatically — judge whether "
            "the user is deliberately asking for this.\n"
            "Answer with ONE word: ADD or SKIP.\n"
            + user_language_line(detect_dialogue_language(user_text))
        )
        user_prompt = (
            f"User message: \"{user_text}\"\n"
            f"Extracted candidate: \"{candidate}\"\n"
            f"Is this an explicit request to {intent_desc}? Answer ADD or SKIP."
        )

        try:
            verdict = self._local_router.classify(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                valid_outputs=["ADD", "SKIP"],
                temperature=0.0,
                max_tokens=5,
                task="intent_router",
            )
        except Exception as e:
            logger.warning(f"[Intent] Ошибка классификации: {e}")
            return "ASK"

        if verdict is None:
            logger.info(f"[Intent] Локальная LLM не распознала ответ для '{candidate[:40]}'")
            return "ASK"

        logger.info(f"[Intent] {intent}: '{candidate[:40]}' -> {verdict}")
        return verdict

    def _persona_page_view_reply(self, user_input: str, page_text: str) -> Optional[str]:
        """«Что ты видишь на странице» — ответ голосом персоны: шаблонный
        список элементов уходит в LLM как ДАННЫЕ, ответ пишет назначенный
        провайдер ответа (llm.answer_provider; не назначен — обычная цепочка).
        None — LLM недоступна: caller отправляет шаблонный список как есть.
        Приватную страницу caller сюда не шлёт; здесь — вторая линия: URL
        без токенов, email/телефоны/карты/токены в подписях — маской."""
        try:
            from app.features.cc_privacy import redact_inline
            page_text = redact_inline(page_text)
            persona_prompt = self.persona.system_prompt.strip()
            messages = [
                {"role": "system", "content": (
                    f"{persona_prompt}\n\n---\n"
                    "The user asked what you see on the page. Below is the page "
                    "content (element list). Answer in your character, briefly "
                    "and to the point — tell what's there in your own words; "
                    "do NOT copy the list verbatim, do NOT invent elements that "
                    "aren't there. Answer in the language of the user's "
                    "question.\n"
                    + user_language_line(detect_dialogue_language(user_input)))},
                {"role": "user", "content": f"Question: {user_input}\n\n"
                                           f"Page content:\n{page_text}"},
            ]
            settings = self.persona.get_settings()
            ans = self.router.get_response(
                messages, temperature=0.5,
                max_tokens=min(int(settings.get("max_tokens", 2000)), 400),
                top_p=settings.get("top_p", 0.9),
                force_provider=self.router.answer_provider)
            return self._clean_response(ans) if ans else None
        except Exception as e:
            logger.debug(f"[BotInstance] Ответ о странице голосом персоны "
                         f"не удался: {e}")
            return None

    def _cc_reply(self, action: Optional[dict], ok: bool,
                  detail: Optional[str], template: str,
                  lang: Optional[str] = None) -> str:
        """Ответ о результате CC-команды: flavor-реплика в характере персоны
        (банк фраз с плейсхолдерами → живой ответ: llm.answer_provider, не
        назначен — google, канал cc),
        при недоступности — честный шаблон (template). При ok=False detail
        передаётся в любом случае, чтобы суть ошибки не терялась."""
        # Исполнение по «да» (pending) идёт мимо _cc_execute_reply: маски
        # истории для ввода/чтения ставим и здесь (повторно — без дублей)
        try:
            self._cc_hist_note_typed(action)
            if ok and isinstance(action, dict) and action.get("kind") == "read":
                self._cc_hist_note_read(action, detail)
        except Exception as e:
            logger.debug(f"[BotInstance] маска истории не поставлена: {e}")
        try:
            from app.features import flavor_text
            flavored = flavor_text.cc_reply(self, action, ok, detail, lang=lang)
            if flavored:
                return flavored
        except Exception as e:
            logger.debug(f"[BotInstance] flavor-реплика не удалась: {e}")
        return template

    def _clean_response(self, response: str) -> str:
        # Очищает ответ от лишнего Markdown-форматирования и мета-рассуждений LLM.
        if not response:
            return response
        response = self._strip_meta_reasoning(response)
        response = self._strip_markdown(response)
        response = self._strip_inline_lists(response)
        # Слабые модели копируют метку времени [DD.MM HH:MM] из промпта в ответ
        response = re.sub(
            r"^\s*\[\d{2}\.\d{2}(?:\.\d{4})?\s+\d{1,2}:\d{2}\]\s*", "", response)
        return response.strip()

    def _addons_repair(self, answer: str, addon_results: dict) -> str:
        # Починка сырого ответа аддонами — до garbage-гарда
        for addon in self.addons:
            try:
                answer = addon.repair(answer, addon_results.get(addon.name))
            except Exception as e:
                logger.warning(f"[Addons] {addon.name}: repair упал: {e}")
        return answer

    @staticmethod
    def _strip_inline_lists(text: str) -> str:
        """Удаляет секции 'Список дел:' и 'Инвентарь:' из ответа LLM.
        Эти списки отправляются отдельным сообщением через _pending_list_messages."""
        # Вырезаем секцию "Список дел:"/"Todo list:" и все её пункты до пустой строки или конца текста
        text = re.sub(r'\n*(?:Список дел|Todo list):\n.*?(?=\n\s*\n|\Z)', '', text, flags=re.DOTALL)
        # Вырезаем секцию "Инвентарь:"/"Inventory:" и все её пункты до пустой строки или конца текста
        text = re.sub(r'\n*(?:Инвентарь|Inventory):\n.*?(?=\n\s*\n|\Z)', '', text, flags=re.DOTALL)
        # Схлопываем лишние пустые строки, оставшиеся после вырезания
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    @staticmethod
    def _strip_meta_reasoning(text: str) -> str:
        # Удаляет мета-рассуждения LLM: вероятности, запросы, внутренний монолог.
        # Сохраняем блоки кода
        code_blocks = []
        def _save(m):
            code_blocks.append(m.group(0))
            return f'\x00CB{len(code_blocks) - 1}\x00'
        text = re.sub(r'```.*?```', _save, text, flags=re.DOTALL)

        # Вставки в двойных звёздочках — стилистический приём персоны
        # («внутренние процессы» и т.п.), читатель должен их видеть: рендер
        # Telegram сам показывает **x** жирным. Мета-паттерны ниже писались
        # под одиночные *...* — внутри пары ** они матчатся посередине и
        # съедают текст, оставляя висящий **.
        bold_spans = []
        def _save_bold(m):
            bold_spans.append(m.group(0))
            return f'\x00MB{len(bold_spans) - 1}\x00'
        text = re.sub(r'\*\*.+?\*\*', _save_bold, text, flags=re.DOTALL)

        # Мета-фразы инвентаря
        meta_patterns = [
            r'Запрос на добавление предмета в инвентарь\.?\s*',
            r'Запрос на пиццу совпадает с предыдущим контекстом разговора\.?\s*',
            r'Вероятность:\s*\d+%\.?\s*',
            r'Вероятность продолжения темы:\s*\d+%\.?\s*',
            r'Требуется создание описания для [^.]+\.?\s*',
            r'Инвентарь обновл[её]н\.?\s*',
            r'Предмет получен\.?\s*',
            r'Пицца получена\.?\s*',
            r'\*\s*Запрос на [^.]+\*\s*',
            r'\*\s*Вероятность[^*]+\*\s*',
            r'\*\s*Требуется[^*]+\*\s*',
            r'Я принимаю [^.]+ от вас[^.]*\.?\s*',
        ]
        for pattern in meta_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)

        # Убираем лишние пустые строки
        text = re.sub(r'\n{3,}', '\n\n', text)

        # Восстанавливаем bold-вставки и code-блоки
        # (bold раньше code: спан мог сохранить внутри себя плейсхолдер блока)
        for i, span in enumerate(bold_spans):
            text = text.replace(f'\x00MB{i}\x00', span)
        for i, block in enumerate(code_blocks):
            text = text.replace(f'\x00CB{i}\x00', block)
        return text.strip()

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Удаляет Markdown-разметку, которую Telegram не поддерживает.
        Жирный (**), курсив (*), код (`), спойлер (||), подчеркивание (__),
        выделение (==) — остаётся, их конвертит _md_to_html.
        Code-блоки (```...```) не трогаются — их обрабатывает file_sender."""

        # Сохраняем блоки кода, чтобы не повредить их чисткой
        code_blocks = []
        def _save(m):
            code_blocks.append(m.group(0))
            return f'\x00CB{len(code_blocks) - 1}\x00'
        text = re.sub(r'```.*?```', _save, text, flags=re.DOTALL)

        # Пустые маркеры: **\n\n** или *** без контента убрать.
        # (?<!\S) — не трогаем ** между двумя жирными фрагментами
        # («**воду** и **зонт**»): там это закрывающая и открывающая пары,
        # а не пустой маркер — иначе фрагменты склеятся в «**водузонт**»
        text = re.sub(r'(?<!\S)\*{2,}\s*\*{2,}', '', text)
        # Заголовки: #### Заголовок → Заголовок
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
        # Изображения: ![alt](url) → alt
        text = re.sub(r'!\[(.+?)\]\(.+?\)', r'\1', text)
        # Горизонтальная линия: --- → юникод-разделитель
        text = re.sub(r'^-{3,}\s*$', '───────────', text, flags=re.MULTILINE)
        # Горизонтальная линия: *** или ___ → пустая строка; заодно и
        # осиротевшая линия ** — остаток оборванного/испорченного маркера
        text = re.sub(r'^[*_]{2,}\s*$', '', text, flags=re.MULTILINE)
        # Убираем лишние пустые строки
        text = re.sub(r'\n{3,}', '\n\n', text)

        # Восстанавливаем code-блоки
        for i, block in enumerate(code_blocks):
            text = text.replace(f'\x00CB{i}\x00', block)
        return text.strip()

    # Маркеры фич в ответе LLM. Разбираются ВСЕ вхождения (модель может
    # вычеркнуть два пункта и записать третий одним ответом), каждое
    # вырезается из видимого текста (регэкспы — _marker_re). Пробелы правятся
    # только на месте разреза: маркер на своей строке уходит вместе со
    # строкой, в начале/конце строки — без пробелов вокруг, внутри фразы —
    # один пробел между словами. Остальной текст не трогаем
    @staticmethod
    def _cut_markers(pattern, text: str) -> Tuple[str, List[str]]:
        # Все вхождения маркера: (текст без них, содержимое каждого)
        found, pieces, pos = [], [], 0
        for m in pattern.finditer(text):
            found.append(m.group(1).strip())
            pieces.append(text[pos:m.start()])
            pos = m.end()
        if not found:
            return text, found
        pieces.append(text[pos:])
        acc = pieces[0]
        for i, right in enumerate(pieces[1:], 1):
            if i < len(pieces) - 1 and not right.strip(" \t"):
                continue  # между маркерами одни пробелы — один разрез
            core_l, core_r = acc.rstrip(" \t"), right.lstrip(" \t")
            line_start = not core_l or core_l.endswith("\n")
            line_end = not core_r or core_r.startswith("\n")
            if line_start and line_end:
                acc = core_l + (core_r[1:] if core_r.startswith("\n") else core_r)
            elif line_start or line_end:
                acc = core_l + core_r
            else:
                glued = acc == core_l and right == core_r
                sep = "" if glued or core_r[0] in ",.;:!?)»…" else " "
                acc = core_l + sep + core_r
        return acc, found

    def _parse_punishment(self, response: str, user_id: str) -> str:
        # Парсит маркеры наказания, выполняет действия.
        if "[PUNISH:BLOCK]" in response:
            response = response.replace("[PUNISH:BLOCK]", "").strip()
            self._block_user(user_id)
            logger.info(f"Пользователь {user_id} заблокирован (PUNISH:BLOCK)")

        response, facts = self._cut_markers(_PUNISH_FACT_MARK_RE, response)
        for fact_text in facts:
            self.inject_fact(fact_text, user_id)
            logger.info(f"Пользователь {user_id} — подставной факт: {fact_text}")

        return response.strip() if facts else response

    # ── переспрос про дела/инвентарь (без локальной модели) ──

    def _take_list_offer(self, chat_id, user_id) -> Optional[dict]:
        # Висящий переспрос этого чата для user_id — забирается (см. list_offers)
        offers = getattr(self, "list_offers", None)
        return offers.take(chat_id, user_id) if offers is not None else None

    def note_list_message(self, chat_id, text: str, message_ids) -> None:
        """Платформа отправила досылаемое сообщение (список/вопрос): если
        это переспрос — запоминаем его message_id, reply на него — ответ."""
        offers = getattr(self, "list_offers", None)
        if offers is not None and message_ids:
            offers.note_message(chat_id, text, message_ids)

    def _bot_asked_since(self, chat_id, ts: float) -> bool:
        """После ts бот задал в чате другой вопрос: обучение («продолжаем?»,
        тест, «как часто?») или инициатива. «Да» тогда — скорее ответ на
        него, а не на переспрос про дела."""
        lm = getattr(self, "learning_manager", None)
        if lm is not None:
            try:
                for s in lm.get_sessions(chat_id):
                    if max(s.get("continue_asked_at") or 0, s.get("quiz_set_at") or 0) > ts:
                        return True
                setup = lm.get_setup_state(chat_id)
                if setup and (setup.get("asked_at") or 0) > ts:
                    return True
            except Exception as e:
                logger.debug(f"[ListOffer] вопросы обучения не прочитаны: {e}")
        pm = getattr(self, "proactive", None)
        if pm is not None and hasattr(pm, "last_initiative_at"):
            try:
                if pm.last_initiative_at(chat_id) > ts:
                    return True
            except Exception as e:
                logger.debug(f"[ListOffer] инициатива не прочитана: {e}")
        return False

    def _ask_list_offer(self, chat_id, user_id, user_name: str, kind: str,
                        value, lang: Optional[str] = None,
                        task: Optional[str] = None) -> None:
        """Переспрос «Записать «X» в список дел?» (локальной модели нет):
        вопрос — отдельным сообщением вслед за ответом, ответ на него ловит
        _list_offer_turn. Один вопрос на ход: «да» сразу на два неоднозначно."""
        offers = getattr(self, "list_offers", None)
        if offers is None:
            return
        bucket = self._pending_lists(chat_id)
        cur = offers.peek(chat_id)
        if cur and cur.get("question") in bucket:
            # Бакет списков живёт один ход: вопрос в нём — задан в этом ходе
            return
        question = list_offers.text(f"ask_{kind}", lang, v=value, t=task)
        offers.begin(chat_id, kind, value, user_id=user_id,
                     user_name=user_name, question=question, task=task)
        bucket.append(question)

    def _list_offer_turn(self, offer: Optional[dict], text: str, chat_id,
                         mode_key, lang: Optional[str],
                         reply_to_bot_message_id=None) -> Optional[str]:
        """Решение по переспросу, забранному в начале хода (_take_list_offer)
        → готовый ответ или None (обычный путь). «да» → действие, «нет» →
        короткий отказ; другое — вопрос снят молча. «Да» не наше, если это
        reply на другое сообщение бота или после вопроса бот спросил другое
        (обучение, инициатива)."""
        if offer is None:
            return None
        if mode_key and self.control_mode_on(mode_key):
            return None  # в режиме управления дела и инвентарь молчат
        if reply_to_bot_message_id and \
                reply_to_bot_message_id not in (offer.get("message_ids") or ()):
            logger.info(f"[ListOffer] chat={chat_id}: reply на другое сообщение бота — не ответ")
            return None
        if self._bot_asked_since(chat_id, float(offer.get("asked_at") or 0)):
            logger.info(f"[ListOffer] chat={chat_id}: после вопроса бот спросил другое — не ответ")
            return None
        verdict = list_offers.classify_reply(offer.get("kind"), text,
                                             self._address_names())
        logger.info(f"[ListOffer] chat={chat_id} {offer.get('kind')} → "
                    f"{verdict or 'не ответ, вопрос снят'}")
        if verdict == "NO":
            return list_offers.text("declined", lang)
        if verdict != "YES":
            return None
        return self._apply_list_offer(offer, chat_id, lang)

    def _apply_list_offer(self, offer: dict, chat_id,
                          lang: Optional[str]) -> Optional[str]:
        # «да» на переспрос: то же действие, что по маркеру LLM, + короткое
        # подтверждение; список — отдельным сообщением, как у маркеров
        kind, v = offer.get("kind"), offer.get("value")
        who = offer.get("user_name") or ""
        if kind == "todo_add" and self.todo_manager:
            self._pending_lists(chat_id).append(
                self.todo_manager.add_item(chat_id, who or "User", str(v), lang=lang))
            return list_offers.text("done_todo_add", lang, v=v)
        if kind == "todo_done" and self.todo_manager:
            # Номер сверяем с текстом пункта на момент вопроса: список мог
            # измениться (другой участник, веб) — тогда ничего не вычёркиваем
            task = offer.get("task")
            result = self.todo_manager.remove_item(chat_id, int(v), lang=lang, expect=task)
            if not result:
                return list_offers.text("gone_todo_done", lang, v=v, t=task)
            self._pending_lists(chat_id).append(result)
            return list_offers.text("done_todo_done", lang, v=v)
        inv = self.inventory_manager
        if kind == "inventory_add" and inv:
            same = inv.find_same(v)
            if same:
                return list_offers.text("dup_inventory_add", lang, v=same)
            desc, expires = self._enrich_inventory_item(v, lang=lang)
            inv.add_item(_cap_first(v), desc, source=who or "user", expires=expires)
            if not inv.has_item(_cap_first(v)):
                return list_offers.text("full_inventory_add", lang, v=v)
            self._pending_lists(chat_id).append(inv.get_list_text())
            return list_offers.text("done_inventory_add", lang, v=v)
        if kind == "inventory_remove" and inv:
            name = self._find_inventory_item_by_substring(v)
            if not name:
                return list_offers.text("gone_inventory_remove", lang, v=v)
            inv.remove_item(name)
            self._pending_lists(chat_id).append(inv.get_list_text())
            return list_offers.text("done_inventory_remove", lang, v=name)
        # Фичу выключили, пока висел вопрос, — обычный путь
        return None

    def _process_todo_marker(
        self, response: str, chat_id: str, user_name: str,
        fallback_task: Optional[str] = None,
        fallback_done_index: Optional[int] = None,
        user_text: str = "", user_id=None, lang: Optional[str] = None,
        seen_tasks: Optional[List[str]] = None,
    ) -> str:
        """Парсит маркеры [TODO_ADD:...] и [TODO_DONE:N] (все вхождения),
        обновляет список дел. Список дел отправляется отдельным сообщением
        через _pending_list_messages. Эвристический fallback подтверждается
        локальной LLM (_confirm_intent); без неё — переспрос, и только на
        явную просьбу (_ask_list_offer)."""
        if not self.todo_manager:
            return response

        list_lang = detect_language(user_text)
        response, done_raw = self._cut_markers(_TODO_DONE_MARK_RE, response)
        response, add_raw = self._cut_markers(_TODO_ADD_MARK_RE, response)
        rendered = None
        ask = None

        # Удаление: [TODO_DONE:N]. Номера — по списку, который видела модель
        # (seen_tasks — снимок при сборке промпта): удаляем по убыванию со
        # сверкой текста, иначе после первого удаления остальные съезжают, а
        # пункт, удалённый за время генерации, сдвинул бы номера
        tasks = (seen_tasks if seen_tasks is not None
                 else self.todo_manager.get_tasks(chat_id)) if done_raw else []
        indices = set()
        for raw in done_raw:
            found = resolve_done_marker(raw, tasks)
            if not found:
                logger.info(f"[Todo] [TODO_DONE:{raw[:40]}] не распознан — пропущен")
            indices.update(found)
        for index in sorted(indices, reverse=True):
            # Номера вне списка, который видела модель, нет: без сверки текста
            # удалился бы пункт, добавленный за время генерации
            if not 1 <= index <= len(tasks):
                continue
            result = self.todo_manager.remove_item(chat_id, index, lang=list_lang,
                                                   expect=tasks[index - 1])
            if result:
                rendered = result

        # Fallback удаление через эвристику — подтверждаем через LLM, иначе
        # «готово, прочитал 3 главы» молча удалило бы пункт №3. Маркер,
        # которого не удалось разобрать, fallback не отключает
        if not indices and fallback_done_index is not None:
            verdict = self._confirm_intent(user_text, f"item #{fallback_done_index}", "todo_remove")
            if verdict == "ADD":
                # Пункт — тот, что человек видел под этим номером (если снимок
                # есть); номера вне снимка нет — новый пункт не трогаем
                seen = (seen_tasks[fallback_done_index - 1]
                        if seen_tasks and 1 <= fallback_done_index <= len(seen_tasks) else None)
                if seen_tasks is None or seen is not None:
                    result = self.todo_manager.remove_item(chat_id, fallback_done_index,
                                                           lang=list_lang, expect=seen)
                    if result:
                        rendered = result
            elif verdict == "ASK" and is_explicit_todo_done_request(user_text):
                current = self.todo_manager.get_tasks(chat_id)
                if 1 <= fallback_done_index <= len(current):
                    ask = ("todo_done", fallback_done_index, current[fallback_done_index - 1])

        # Добавление: [TODO_ADD:...]
        tasks_add = [t for t in add_raw if t]
        if not add_raw and fallback_task:
            # Эвристический fallback — подтверждаем через LLM; SKIP — игнорируем
            verdict = self._confirm_intent(user_text, fallback_task, "todo_add")
            if verdict == "ADD":
                tasks_add = [fallback_task]
            elif verdict == "ASK" and is_explicit_todo_request(user_text):
                ask = ask or ("todo_add", fallback_task, None)
        for task in tasks_add:
            rendered = self.todo_manager.add_item(chat_id, user_name, task, lang=list_lang)

        # Итоговый список — один раз, после всех правок
        if rendered:
            self._pending_lists(chat_id).append(rendered)
        if ask:
            self._ask_list_offer(chat_id, user_id, user_name, ask[0], ask[1],
                                 lang=lang or list_lang, task=ask[2])
        return response.strip()

    @staticmethod
    def _parse_inventory_add(raw: str) -> Tuple[str, str, Optional[str]]:
        # «Название:описание:YYYY-MM-DD» — описание и срок необязательны;
        # двоеточие внутри описания разбор не ломает
        parts = raw.split(":")
        name = parts[0].strip()
        expires = None
        if len(parts) > 1 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[-1].strip()):
            expires = parts.pop().strip()
        return name, ":".join(parts[1:]).strip(), expires

    def _process_inventory_markers(self, response: str, fallback_add: Optional[str] = None,
                                   fallback_remove: Optional[str] = None, giver_name: str = "",
                                   user_text: str = "", chat_id=None, user_id=None,
                                   lang: Optional[str] = None) -> str:
        """Парсит маркеры [INVENTORY_ADD:...], [INVENTORY_REMOVE:...], [INVENTORY_USE:...]
        (все вхождения), обновляет инвентарь.
        Инвентарь отправляется отдельным сообщением через _pending_list_messages (per-chat бакет
        по chat_id — иначе при параллельных чатах список уедет не в тот чат).
        Эвристический fallback подтверждается локальной LLM (_confirm_intent), чтобы не добавлять
        предметы из обычных реплик («получил жабку» не должно добавлять «л жабку»); без неё —
        переспрос, и только на явную передачу предмета (_ask_list_offer)."""
        if not self.inventory_manager:
            return response

        inventory_changed = False
        ask = None
        response, use_raw = self._cut_markers(_INV_USE_MARK_RE, response)
        response, add_raw = self._cut_markers(_INV_ADD_MARK_RE, response)
        response, remove_raw = self._cut_markers(_INV_REMOVE_MARK_RE, response)

        # INVENTORY_USE — бот использует предмет (удаляется)
        for name in use_raw:
            if name:
                self.inventory_manager.use_item(name)
                inventory_changed = True

        # INVENTORY_ADD — приоритет: маркер от LLM
        # Формат из промпта: [INVENTORY_ADD:Название:описание:YYYY-MM-DD] (дата опциональна)
        for raw in add_raw:
            name, desc, expires = self._parse_inventory_add(raw)
            if not name:
                continue
            # Дополняем описание/срок через локальную модель, если LLM их не указала
            desc, expires = self._enrich_inventory_item(
                name, desc, expires, lang=detect_language(user_text))
            self.inventory_manager.add_item(name, desc, source=giver_name, expires=expires)
            inventory_changed = True
        # Fallback: эвристика нашла предмет, но маркера нет — подтверждаем через LLM
        if not add_raw and fallback_add:
            verdict = self._confirm_intent(user_text, fallback_add, "inventory_add")
            if verdict == "ADD":
                f_desc, f_expires = self._enrich_inventory_item(
                    fallback_add, lang=detect_language(user_text))
                self.inventory_manager.add_item(fallback_add, f_desc, source=giver_name, expires=f_expires)
                inventory_changed = True
            elif verdict == "ASK":
                # Локальная LLM недоступна — переспрашиваем вместо слепого
                # добавления, но только на явную передачу предмета и не про
                # то, что уже лежит в инвентаре («шоколадку» ~ «Шоколадка»)
                item = explicit_inventory_item(user_text, fallback_add)
                if item and not self.inventory_manager.find_same(item):
                    ask = ("inventory_add", item)
            # SKIP — игнорируем, предмет не создаётся

        # INVENTORY_REMOVE — приоритет: маркер от LLM (пользователь забирает или отменяет)
        for name in remove_raw:
            if name:
                self.inventory_manager.remove_item(name)
                inventory_changed = True
        if not remove_raw and fallback_remove:
            verdict = self._confirm_intent(user_text, fallback_remove, "inventory_remove")
            if verdict == "ADD":
                self.inventory_manager.remove_item(fallback_remove)
                inventory_changed = True
            elif verdict == "ASK":
                # Переспрос — только про предмет, который правда есть в инвентаре
                item = explicit_inventory_item(user_text, fallback_remove)
                found = self._find_inventory_item_by_substring(item) if item else None
                if found:
                    ask = ask or ("inventory_remove", found)

        # Проверяем просроченные предметы
        expired = self.inventory_manager.remove_expired_items()
        if expired:
            inventory_changed = True

        if inventory_changed:
            self._pending_lists(chat_id).append(self.inventory_manager.get_list_text())
        if ask:
            self._ask_list_offer(chat_id, user_id, giver_name, *ask,
                                 lang=lang or detect_language(user_text))

        return response.strip()

    # File helpers

    _FULL_DOC_KEYWORDS = [
        "перескажи", "пересказ", "резюме", "суммаризуй", "суммаризация",
        "краткое содержание", "основная мысль", "главная идея",
        "перепиши текст", "изложи", "выжимка",
        "расскажи содержание", "о чём документ", "о чем документ",
        "расскажи текст", "весь текст", "полный текст",
        "доклад по", "анализ документа", "разбор документа", "проанализируй"
    ]

    def _get_persona_context_for_search(self) -> str:
        # Собирает краткий контекст персоны для QueryEnhancer (имя, роль, ключевые черты).
        data = self.persona.persona_data
        parts = []
        
        name = data.get("name", self.persona_name)
        if name:
            parts.append(f"Persona name: {name}")

        description = data.get("description", "")
        if description:
            parts.append(f"Description: {description}")

        # Из system_prompt берём только первые 500 символов — основная роль и внешность
        system_prompt = data.get("system_prompt", "")
        if system_prompt:
            # Берём начало до первого крупного раздела
            prompt_preview = system_prompt[:500].strip()
            if prompt_preview:
                parts.append(f"Role and character: {prompt_preview}")
        
        return "\n".join(parts) if parts else ""

    def _is_full_doc_request(self, text: str) -> bool:
        # Определяет, просит ли пользователь пересказ/анализ документа целиком.
        lower = text.lower()
        return any(kw in lower for kw in self._FULL_DOC_KEYWORDS)

    _DOCS_ONLY_KEYWORDS = [
        "только документ", "только файл", "только из документ",
        "по документу", "по файлу", "из файла", "из документа",
        "без поиска", "не ищи", "не используй поиск",
        "без интернета", "без веб", "offline",
        "only documents", "no search", "without search",
    ]

    def _is_docs_only_request(self, text: str) -> bool:
        # Пользователь просит ответить только по документам — без веб-поиска.
        lower = text.lower()
        return any(kw in lower for kw in self._DOCS_ONLY_KEYWORDS)

    # Inventory helpers

    _INVENTORY_USAGE_PATTERNS = [
        # Прямые утверждения: "ты съел X", "ты использовал Y"
        re.compile(r"(?:ты|вы)\s+(?:использовал[ао]?|съел[ао]?|выпил[ао]?|применил[ао]?|взял[ао]?|открыл[ао]?|закурил[ао]?|съел[ао]?|съела|съел|поел[ао]?|попил[ао]?|съешь|выпей|используй|примени|съешь|выпей|открой|закури|возьми)\s+(.+)", re.IGNORECASE),
        # "ты уже ..."
        re.compile(r"(?:ты|вы)\s+(?:уже)\s+(?:использовал[ао]?|съел[ао]?|выпил[ао]?|применил[ао]?|взял[ао]?|открыл[ао]?|съел[ао]?|поел[ао]?|попил[ао]?)\s+(.+)", re.IGNORECASE),
        # Предложения совместного действия: "давай съедим X", "давай выпьем Y"
        re.compile(r"(?:давай|давайте)\s+(?:вместе\s+)?(?:съедим|поедим|выпьем|попьем|используем|применим|откроем|возьмем|съедим|выпьем)\s+(.+)", re.IGNORECASE),
        # "X, которая у тебя есть" + контекст совместного использования
        re.compile(r"(?:съедим|поедим|выпьем|попьем|используем|применим|откроем|возьмем)\s+(.+?)(?:\s+котор[аяое]\s+у\s+тебя\s+есть|\s+из\s+инвентаря|\s+что\s+у\s+тебя\s+есть)", re.IGNORECASE),
    ]

    def _find_inventory_item_by_substring(self, text: str) -> Optional[str]:
        """
        Ищет предмет в инвентаре по подстроке.
        Например, 'пиццу' найдет 'Пицца с ананасом и халапеньо'.
        """
        if not self.inventory_manager:
            return None
        text_lower = text.strip().lower()
        items = self.inventory_manager.get_items()
        # Сначала точное совпадение
        for item in items:
            if item.name.lower() == text_lower:
                return item.name
        # Затем по подстроке (предмет содержит запрос)
        for item in items:
            if text_lower in item.name.lower():
                return item.name
        # Затем запрос содержит название предмета
        for item in items:
            if item.name.lower() in text_lower:
                return item.name
        return None

    def _extract_user_reported_usage(self, text: str) -> Optional[str]:
        """
        Извлекает название предмета из сообщения пользователя о том,
        что бот использовал/съел/выпил предмет.
        Например: 'ты использовал меч', 'ты съел яблоко', 'ты выпил зелье'.
        """
        for pattern in self._INVENTORY_USAGE_PATTERNS:
            match = pattern.search(text)
            if match:
                item = match.group(1).strip()
                # Убираем trailing punctuation
                item = re.sub(r"[.!?\s]+$", "", item).strip()
                # Убираем 'пожалуйста' и подобное
                item = re.sub(r"\s+пожалуйста\s*$", "", item, flags=re.IGNORECASE).strip()
                if item and len(item) > 1:
                    return item
        return None

    # Memory helpers

    def get_memory_stats(self, user_id: str = "default", chat_id: str = None) -> dict:
        return self.memory.get_stats(user_id, chat_id)

    def get_dossier_snapshot(self, chat_id: str, user_id: str = None) -> dict:
        """Профиль досье чата (интересы/темы/наблюдения) для веб-UI.
        user_id — только записи этого участника (персональный контекст)."""
        if self._chat_dossier is None:
            from app.features.chat_dossier import ChatDossier
            self._chat_dossier = ChatDossier(context=self.context, router=self.router)
        return self._chat_dossier.get_profile_snapshot(chat_id, user_id=user_id)

    def _get_dossier_context_line(self, chat_id: str, user_id: str) -> Optional[str]:
        """Короткая строка портрета из досье (интересы + пара наблюдений) в
        основной ответ — чтобы бот опирался на неё не только в инициативах.
        Темы сознательно не берём: они ситуативные, в постоянном контексте — шум."""
        snap = self.get_dossier_snapshot(chat_id, user_id=user_id)
        parts = []
        interests = snap["interests"][-8:]
        if interests:
            parts.append("interests: " + ", ".join(interests))
        # Наблюдения не атрибутированы по пользователям — в группе это
        # смешанный портрет, поэтому даём их только в личном чате
        if str(chat_id) == str(user_id):
            notes = snap["personality_notes"][-2:]
            if notes:
                parts.append("style: " + "; ".join(notes))
        if not parts:
            return None
        return "Known about the user (chat analysis): " + "; ".join(parts)

    def get_stm_last_display(self, n: int, chat_id: str) -> list:
        return self.memory.stm.get_last_display(n, chat_id)

    def stm_pop_last_n(self, n: int, chat_id: str) -> int:
        return self.memory.stm.pop_last_n(n, chat_id)

    def clear_memory(self, user_id: str = "default", chat_id: str = None):
        self.memory.clear_stm(chat_id)
        self.memory.clear_ltm(user_id)

    def clear_ltm_only(self, user_id: str = "default"):
        self.memory.clear_ltm(user_id)

    def inject_fact(self, fact_text: str, user_id: str = "default"):
        self.memory.ltm.save_facts(fact_text, user_id)

    def get_ltm_privacy(self, user_id: str) -> str:
        # Режим приватности LTM пользователя: 'smart' (по умолчанию) | 'strict'.
        return self.memory.ltm.get_privacy_mode(user_id)

    def set_ltm_privacy(self, user_id: str, mode: str) -> str:
        # Устанавливает режим приватности LTM. Возвращает установленный режим.
        return self.memory.ltm.set_privacy_mode(user_id, mode)

    def forget_fact(self, query: str, user_id: str) -> Optional[str]:
        # Точечное забывание факта из LTM. Возвращает текст удалённого или None.
        return self.memory.ltm.forget(query, user_id)

    def update_fact(self, old_query: str, new_text: str, user_id: str) -> Optional[str]:
        # Замена факта новым текстом (правка из веб-UI). Возвращает старый текст или None.
        return self.memory.ltm.update_fact(old_query, new_text, user_id)

    def get_relations_text(self, user_id: str, chat_id: str = None) -> str:
        # Социальный граф: связи пользователя и (в группе) других участников.
        from app.core.users import get_user_tag
        lines = []
        own = self.memory.ltm.get_facts_by_category(user_id, "Relation", chat_id=chat_id)
        if own:
            lines.append("Твои связи:")
            lines += [f"  - {r.partition(':')[2].strip()}" for r in own]

        if chat_id and str(chat_id) != str(user_id):
            facts = self.memory.ltm.get_chat_facts(chat_id, exclude_user_id=user_id)
            rel = [f for f in facts if f["category"] == "Relation"]
            if rel:
                if lines:
                    lines.append("")
                lines.append("Связи участников этого чата:")
                for f in rel:
                    name = f["user_name"] or get_user_tag(f["user_id"]) or "Участник"
                    lines.append(f"  - {name}: {f['fact'].partition(':')[2].strip()}")

        return "\n".join(lines) if lines else "Пока ничего не знаю о связях."

    def debug_context(self, user_id: str, chat_id: str = None, query: str = "") -> str:
        # Собирает блоки, которые ушли бы в промпт (отладка для owner'а).
        stm_messages, ltm_facts, stm_relevant = self.memory.get_context(
            user_id, chat_id, ltm_query=query or "контекст"
        )
        parts = [f"== LTM facts ({len(ltm_facts)}) =="]
        parts += ltm_facts or ["(пусто)"]
        rules = self.memory.ltm.get_facts_by_category(user_id, "Rule", chat_id=chat_id)
        parts.append(f"\n== Rules ({len(rules)}) ==")
        parts += rules or ["(пусто)"]
        if chat_id and str(chat_id) != str(user_id):
            block = self.memory.get_chat_facts_block(chat_id, exclude_user_id=user_id)
            parts.append("\n== Chat facts (другие участники) ==")
            parts.append(block or "(пусто)")
        parts.append(f"\n== STM последние ({len(stm_messages)}) ==")
        for m in stm_messages:
            name = m.get("user_name") or m.get("role")
            parts.append(f"[{m['role']}] {name}: {m['content'][:120]}")
        parts.append(f"\n== STM relevant ({len(stm_relevant)}) ==")
        for m in stm_relevant:
            parts.append(f"- {m['content'][:120]}")
        return "\n".join(parts)

    def export_ltm_file(self, user_id: str) -> Optional[str]:
        # Создаёт JSON-файл со всеми фактами LTM пользователя. Путь к файлу или None.
        import tempfile
        facts = self.memory.ltm.get_all_facts_with_meta(user_id)
        if not facts:
            return None
        payload = {
            "user_id": str(user_id),
            "persona": self.persona_name,
            "exported_at": timeutil.now().isoformat(timespec="seconds"),
            "privacy_mode": self.get_ltm_privacy(user_id),
            "facts": facts,
        }
        tmp_dir = tempfile.mkdtemp(prefix="ltm_export_")
        path = os.path.join(tmp_dir, f"ltm_{user_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    def clear_all_memory(self):
        self.memory.clear_stm()
        # Через LTM, а не collection.delete напрямую: clear_all поднимает общую
        # эпоху очистки — идущая фоновая экстракция/консолидация увидит её и
        # не допишет факты в только что очищенную память
        try:
            self.memory.ltm.clear_all()
        except Exception as e:
            logger.warning(f"[{self.persona_name}] Очистка LTM не удалась: {e}")

    def toggle_web_search(self, chat_id: str) -> bool:
        # Переключает web_search для чата. Возвращает новое состояние (True=включён).
        if not self._web_search_enabled:
            return False
        if chat_id in self._web_search_disabled_chats:
            self._web_search_disabled_chats.discard(chat_id)
            return True
        else:
            self._web_search_disabled_chats.add(chat_id)
            return False

    def get_rate_limit_status(self) -> str:
        if self._rate_limit_enabled:
            return self._rate_limit_status(self._rate_limit_individual)
        return "Rate limiter не активен."

    # Proactive messaging helpers

    def _get_last_message_time(self, chat_id: str) -> float:
        """Возвращает timestamp последнего сообщения в чате.
        Сначала смотрит в activity_tracker, потом в STM буфер."""
        # 1. Смотрим в activity tracker (сохраняется между перезапусками)
        if self._activity_tracker:
            ts = self._activity_tracker.get_last_activity(chat_id)
            if ts > 0:
                return ts

        # 2. Fallback: смотрим в STM буфер (текущая сессия)
        try:
            messages = self.memory.stm.get_messages(chat_id=chat_id)
            if messages:
                # Берем время последнего сообщения из STM
                last_msg = messages[-1]
                if isinstance(last_msg, dict) and "timestamp" in last_msg:
                    return float(last_msg["timestamp"])
                # Если timestamp нет, используем время загрузки из метаданных
                if isinstance(last_msg, dict) and "metadata" in last_msg:
                    meta = last_msg["metadata"]
                    if isinstance(meta, dict) and "timestamp" in meta:
                        return float(meta["timestamp"])
        except Exception:
            pass

        # 3. Fallback: смотрим в текущий буфер в памяти
        try:
            if hasattr(self.memory.stm, "buffers") and chat_id in self.memory.stm.buffers:
                buf = self.memory.stm.buffers[chat_id]
                if buf:
                    last = buf[-1]
                    if isinstance(last, dict) and "timestamp" in last:
                        return float(last["timestamp"])
        except Exception:
            pass

        return 0

    def setup_proactive(self, sender: MessageSender):
        # Создаёт ProactiveMessaging с готовым sender (платформа: Telegram / веб-inbox).
        if not self._activity_tracker:
            return
        from app.features.proactive_messaging import ProactiveConfig, ProactiveMessaging
        proactive_config = self.features.get("proactive", {})
        self._sender = sender
        self.proactive = ProactiveMessaging(
            config=ProactiveConfig.from_dict(proactive_config),
            router=self.router,
            persona=self.persona,
            memory=self.memory,
            activity_tracker=self._activity_tracker,
            get_last_message_time=self._get_last_message_time,
            sender=sender,
            context=self.context,
            self_memory=self.self_memory,
            living=self.living,
            intellect=self.intellect,
            turn_gate=self._get_turn_gate(),
        )
        # Создаем досье на чат (общий экземпляр с rhythm — один файл на бота)
        from app.features.chat_dossier import ChatDossier
        if self._chat_dossier is None:
            self._chat_dossier = ChatDossier(context=self.context, router=self.router)
        self.proactive.dossier = self._chat_dossier
        logger.info(f"  [{self.persona_name}] Proactive messaging инициализирован с sender и досье")

        # Живая персона: сигналы инициативы + источники чатов
        if self.living is not None:
            self.living.on_initiative_signal = self.proactive.state_initiative_signal
            self.living.get_known_chats = self._activity_tracker.get_known_chats
            self.living.get_last_message_time = self._get_last_message_time
            self.living.get_last_initiative_time = (
                lambda chat_id: self.proactive._last_initiative_time.get(str(chat_id), 0))
            # дешёвые гейты перед LLM-скорингом инициативы
            self.living.pre_initiative_gate = self.proactive.initiative_cheaply_possible
            logger.info(f"  [{self.persona_name}] Living persona связана с proactive")

    def setup_learning(self, sender: MessageSender):
        # Передаёт sender, роутеры и memory в learning_manager (платформа: Telegram / веб-inbox).
        if not self.learning_manager:
            return
        self.learning_manager.set_sender(sender)
        self.learning_manager.set_routers_persona(self.router, self.persona, self._local_router)
        self.learning_manager.set_memory(self.memory)
        logger.info(f"  [{self.persona_name}] Learning manager инициализирован с sender, роутерами и memory")

    def setup_rhythm(self, sender: MessageSender):
        """Создает RhythmManager с готовым sender (утренние приветствия /
        ночные «пора спать» / погодные предупреждения). Вызывается после
        инициализации Telegram Bot / веб-inbox; no-op при выключенной фиче."""
        rhythm_config = self.features.get("rhythm", {})
        if isinstance(rhythm_config, bool):
            rhythm_config = {"enabled": rhythm_config}
        if not rhythm_config.get("enabled", False):
            return
        from app.features.rhythm_manager import RhythmConfig, RhythmManager
        if self._activity_tracker is None:
            from app.features.proactive_messaging import ChatActivityTracker
            self._activity_tracker = ChatActivityTracker(context=self.context)
        # Досье общее с proactive (один экземпляр на бота); включённому без
        # proactive нужен свой — отметки событий rhythm в досье чата
        if self._chat_dossier is None:
            from app.features.chat_dossier import ChatDossier
            self._chat_dossier = ChatDossier(context=self.context, router=self.router)
        self.rhythm = RhythmManager(
            context=self.context,
            config=RhythmConfig.from_dict(rhythm_config),
            router=self.router,
            persona=self.persona,
            memory=self.memory,
            activity_tracker=self._activity_tracker,
            sender=sender,
            muted_check=self.is_muted,
            dossier=self._chat_dossier,
            turn_gate=self._get_turn_gate(),
        )
        logger.info(f"  [{self.persona_name}] Rhythm инициализирован с sender")

    # ── слэш-команды: создание сущности + ответ через LLM в образе персоны ──

    def describe_image(self, image_bytes: bytes, question: str = "",
                       lang: Optional[str] = None) -> Optional[str]:
        """OCR + описание изображения через vision-провайдер основного роутера.
        lang — язык пользователя (None — по подписи к картинке).
        Возвращает None, если ни один vision-провайдер не настроен/не ответил."""
        if not self.router.supports_vision():
            return None
        prompt = (
            "The user sent an image. Extract all visible text from it (OCR) "
            "and briefly describe what is shown (1-2 sentences).\n"
            "Response format:\nTEXT: <text from the image or \"no text\">\nDESCRIPTION: <...>"
        )
        if question:
            prompt += f"\nAdditionally answer the user's question about the image: {question}"
        # Текст с картинки (TEXT) — как есть, описание — на языке пользователя
        prompt += "\n" + user_language_line(lang or detect_language(question))
        return self.router.get_response_with_image(prompt, image_bytes)

    def _enrich_inventory_item(self, name: str, desc: str = "", expires: Optional[str] = None,
                               lang: Optional[str] = None) -> tuple:
        """Дополняет описание и срок годности предмета через ЛОКАЛЬНУЮ модель
        (основную не трогаем). Срок придумывается только для портящихся предметов.
        Возвращает (desc, expires) — незаполненные поля остаются как были."""
        if not name:
            return desc, expires
        if not self._local_router or not self._local_router.is_available(task="inventory_enrich"):
            logger.info(f"[Inventory] Локальная модель недоступна — «{name}» без описания/срока")
            return desc, expires
        try:
            today = timeutil.today().isoformat()
            messages = [
                {"role": "system", "content": (
                    f"Today is {today}. For the item, come up with:\n"
                    "1) DESCRIPTION — brief (5-15 words), without the name and without quotes.\n"
                    "2) EXPIRES — an expiration date YYYY-MM-DD, ONLY if the item can spoil "
                    "(food, drinks, flowers, etc.); for non-perishable items write \"-\".\n"
                    "The answer is strictly two lines:\nDESCRIPTION: ...\nEXPIRES: ...\n"
                    + user_language_line(lang or detect_language(name))
                )},
                {"role": "user", "content": name.strip()},
            ]
            resp = self._local_router.get_response(messages, temperature=0.3, max_tokens=80, top_p=0.9, task="inventory_enrich")
            if resp:
                for line in resp.strip().splitlines():
                    line = line.strip()
                    low = line.lower()
                    if not desc and low.startswith(("description", "описание")):
                        candidate = line.partition(":")[2].strip().strip('"\'""«»').strip()
                        if 3 <= len(candidate) <= 120:
                            desc = candidate
                    elif not expires and low.startswith(("expires", "срок")):
                        m = re.search(r"\d{4}-\d{2}-\d{2}", line)
                        if m and m.group(0) >= today:  # прошедшую дату не принимаем
                            expires = m.group(0)
                logger.info(f"[Inventory] «{name}» → описание={desc!r}, срок={expires!r}")
            else:
                logger.info(f"[Inventory] Локальная модель не ответила для «{name}»")
        except Exception as e:
            logger.debug(f"[Inventory] Обогащение предмета не удалось: {e}")
        return desc, expires

    def command_reply(
        self, context_note: str, note_kind: str,
        chat_id: str, user_id: str, user_name: str, user_input: str,
    ) -> str:
        """Ответ на слэш-команду — тоже ход пользователя (пишет в STM пару
        «команда → ответ»): фоновые сообщения между ними не встают."""
        with self.user_turn(self.stm_key(chat_id, user_id)):
            return self._command_reply_impl(
                context_note, note_kind, chat_id, user_id, user_name, user_input)

    def _command_reply_impl(
        self, context_note: str, note_kind: str,
        chat_id: str, user_id: str, user_name: str, user_input: str,
    ) -> str:
        """
        Генерирует ответ на слэш-команду в характере персоны.
        Сущность (напоминание/задача/предмет/сессия обучения) уже создана в _dispatch_command —
        здесь только формируется контекстная инструкция и вызывается LLM.
        БЕЗ detection-блоков и обработки маркеров (чтобы не создать сущность повторно).
        """
        # Сохраняем сообщение пользователя (текст команды) в STM
        self.memory.add_message("user", user_input, user_id, chat_id, user_name)

        # Собираем контекст
        stm_messages, ltm_facts, stm_relevant = self.memory.get_context(
            user_id, chat_id, ltm_query=user_input
        )
        context_parts = []
        if ltm_facts:
            context_parts.append("\n".join(ltm_facts))
        # В группе — факты других участников, сказанные публично в этом чате
        if chat_id and str(chat_id) != str(user_id):
            chat_facts_block = self.memory.get_chat_facts_block(chat_id, exclude_user_id=user_id)
            if chat_facts_block:
                context_parts.append(chat_facts_block)
        memory_text = "\n\n".join(context_parts)
        self_memory_block = None
        if self.self_memory:
            self_memory_block = self.self_memory.get_context_block()
        stm_relevant_text = None
        if stm_relevant:
            parts = []
            for msg in stm_relevant:
                role_ru = msg.get("user_name", "User") if msg["role"] == "user" else "Assistant"
                parts.append(f"  {role_ru}: {msg['content'][:200]}")
            stm_relevant_text = "\n".join(parts)

        # Маршрутизируем note в нужный *_context параметр prepare_messages
        kwargs = dict(
            user_message=user_input, memory_context=memory_text, history=stm_messages,
            user_id=user_id, user_name=user_name,
            has_files=False, self_memory_block=self_memory_block,
            stm_relevant=stm_relevant_text,
        )
        if note_kind == "reminder":
            kwargs["reminder_context"] = context_note
        elif note_kind == "todo":
            kwargs["todo_context"] = context_note
        elif note_kind == "learning":
            kwargs["learning_context"] = context_note
        elif note_kind == "inventory":
            kwargs["inventory_context"] = context_note

        messages = self.persona.prepare_messages(**kwargs)
        settings = self.persona.get_settings()
        answer = self.router.get_response(
            messages, **settings)
        if not answer:
            logger.error("Все LLM-провайдеры недоступны, ответ не сгенерирован")
            return "Сейчас все LLM-провайдеры недоступны. Попробуй позже."

        # Защита от обрыва по max_tokens — та же логика, что в основном процессе сообщений.
        # Для webchat догенерация отключена (см. process_message): «continue» в
        # непрерывном чате даёт дубли реплики и мусор в ленте.
        _webchat_answered = str(getattr(self.router, "_last_provider", "") or "").startswith("webchat")
        _continuations = 0
        while not _webchat_answered and _looks_truncated(answer) and _continuations < 2:
            follow_up_messages = messages + [
                {"role": "assistant", "content": answer},
                {"role": "user", "content": "You stopped mid-sentence. Continue strictly from where you left off — do not repeat what was already written and do not start over. Continue in the same language as the reply."},
            ]
            cont = self.router.get_response(
                follow_up_messages, **settings)
            if not cont:
                break
            answer = answer + cont
            _continuations += 1

        answer = self._clean_response(answer)

        answer = self._save_assistant_reply(answer, user_id, chat_id)
        return answer

    def _dispatch_command(
        self, kind: str, args: str, chat_id: str, user_id: str, user_name: str,
    ) -> str:
        # Область диалога — как у process_message: свой тред веб-чата
        with dialog_scope(self.stm_key(chat_id, user_id)):
            return self._dispatch_command_impl(kind, args, chat_id, user_id,
                                               user_name)

    def _dispatch_command_impl(
        self, kind: str, args: str, chat_id: str, user_id: str, user_name: str,
    ) -> str:
        """
        Оркестратор слэш-команд. Создаёт сущность через manager API и формирует
        контекстную инструкцию для ответа в образе персоны.
        kind: 'remind' | 'todo' | 'inventory' | 'learn' | 'stop_learning'
        """
        args = (args or "").strip()
        user_input_cmd = f"/{kind} {args}".strip()  # что сохранится в STM
        # Якорь личности автора команды — иначе в групповом чате LLM может
        # приписать команду другому участнику из истории (по имени/теме)
        who = f"{user_name} (ID:{user_id})"

        # Как и в process_message: сбрасываем флаг «последний ответ — вопрос бота»
        # ЭТОГО чата. Команда тоже может закончиться вопросом (пока только /learn —
        # «как часто присылать уроки?»), и telegram-слой по флагу регистрирует
        # message_id ответа.
        self._pending_question_kind[str(chat_id)] = None
        # Команда — тоже следующий ход: висящий переспрос «Записать «X»…?» снят
        self._take_list_offer(chat_id, user_id)

        if kind == "remind":
            if not self.reminder_manager:
                return "Напоминания не активны для этой персоны."
            if not args:
                return "Использование: /remind <что напомнить> [через N ...]"
            # Повторяющееся расписание («каждый день в 9», «по пятницам в 18»)
            rec = parse_recurring("напомни " + args)
            if rec:
                rem_task, rec_schedule = rec
                if rem_task:
                    rem_task = self._reformulate_task(rem_task)
                topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                self.reminder_manager.add_reminder(
                    chat_id, user_name or "User", rem_task, 0, topic_id,
                    schedule=rec_schedule,
                    user_id=user_id, username=get_username(user_id),
                )
                task_display = f" «{rem_task}»" if rem_task else ""
                return f"Хорошо, буду напоминать{task_display} — {format_schedule(rec_schedule, 'ru')}."
            parsed = parse_reminder("напомни " + args)
            if parsed:
                rem_task, rem_delay = parsed
                if rem_task:
                    rem_task = self._reformulate_task(rem_task)
                topic_id = self.get_chat_topic(chat_id) if hasattr(self, "get_chat_topic") else None
                delay_text = self.reminder_manager.format_delay(rem_delay)
                self.reminder_manager.add_reminder(chat_id, user_name or "User", rem_task, rem_delay, topic_id,
                                                   user_id=user_id, username=get_username(user_id))
                task_display = f" «{rem_task}»" if rem_task else ""
                note = (
                    f"User {who} used a command to ask to be reminded{task_display} in {delay_text}. "
                    "The reminder is already scheduled — confirm this in your own style, briefly. "
                    f"Address {user_name} specifically, not other chat participants."
                )
            else:
                # Время не указано — переспрашиваем, запоминаем задачу
                rem_task = self._reformulate_task(args)
                self.reminder_manager.begin_pending_remind(chat_id, rem_task, user_id=user_id)
                note = (
                    f"User {who} used a command to ask to be reminded \"{rem_task}\", but did not specify how soon. "
                    "Ask when to remind them (for example, \"in 2 hours\", \"tomorrow at 12\") — in your own style, briefly. "
                    f"Address {user_name} specifically, not other chat participants."
                )
            return self.command_reply(note, "reminder", chat_id, user_id, user_name, user_input_cmd)

        if kind == "todo":
            if not self.todo_manager:
                return "Список дел не активен для этой персоны."
            if not args:
                return "Использование: /todo <задача>"
            task = self._reformulate_task(args)
            self.todo_manager.add_item(chat_id, user_name or "User", task)
            note = (
                f"User {who} used a command to add the task \"{task}\" to the todo list. "
                "Confirm this in your own style, briefly."
            )
            return self.command_reply(note, "todo", chat_id, user_id, user_name, user_input_cmd)

        if kind == "inventory":
            if not self.inventory_manager:
                return "Инвентарь не активен для этой персоны."
            if not args:
                return "Использование: /inventory <название предмета>[: описание]"
            # Разбираем название и опциональное описание (разделитель : или —)
            parts = re.split(r"\s*[:—–-]\s*", args, maxsplit=1)
            name = parts[0].strip()
            desc = parts[1].strip() if len(parts) > 1 else ""
            # Описание и срок годности (для портящегося) придумает локальная модель
            desc, expires = self._enrich_inventory_item(name, desc, None)
            result = self.inventory_manager.add_item(name, description=desc, source=user_name or "user", expires=expires)
            note = (
                f"User {who} used a command to put the item \"{name}\" into your inventory"
                + (f" (description: {desc})" if desc else "")
                + (f" (expires: {expires})" if expires else "")
                + f". Result: {result} "
                "Confirm this in your own style, briefly."
            )
            return self.command_reply(note, "inventory", chat_id, user_id, user_name, user_input_cmd)

        if kind == "stop_learning":
            if not self.learning_manager:
                return "Режим обучения не активен для этой персоны."
            result = self.learning_manager.handle_stop_request(chat_id, user_id, args, explicit=True)
            if result["kind"] == "which":
                # «Какой курс остановить?» — вопрос, reply на него распознается
                self._pending_question_kind[str(chat_id)] = "stop_choice"
            reply = self.learning_manager.render_stop_reply(
                result, user_language=self.chat_user_language(chat_id))
            # Пара «команда → ответ» в STM, как у command_reply: иначе персона
            # не знает, что курс остановлен, и продолжает о нём говорить
            with self.user_turn(self.stm_key(chat_id, user_id)):
                self.memory.add_message("user", user_input_cmd, user_id, chat_id, user_name)
                return self._save_assistant_reply(self._clean_response(reply), user_id, chat_id)

        if kind == "learn":
            if not self.learning_manager:
                return "Режим обучения не активен для этой персоны."
            if not args:
                return "Использование: /learn <тема>"
            subject = args
            self.learning_manager.begin_setup(chat_id, subject, user_id or "default", user_name or "User")
            note = (
                f"User {who} used a command to ask you to teach them \"{subject}\". "
                "Ask briefly and in your own style how often to send lessons "
                "(for example: once a day, every 2 hours). The course starts after their reply."
            )
            # Ответ ниже — вопрос «как часто?»: отмечаем, чтобы telegram-слой
            # зарегистрировал его message_id и reply пользователя распознался
            # как ответ о частоте (иначе reply-gate для /learn не сработает).
            self._pending_question_kind[str(chat_id)] = "frequency"
            return self.command_reply(note, "learning", chat_id, user_id, user_name, user_input_cmd)

        return "Неизвестная команда."

    def record_activity(self, chat_id: str):
        # Записывает активность в чате. Вызывается при каждом сообщении.
        if self._activity_tracker:
            self._activity_tracker.record_activity(chat_id)

    def note_presence(self, chat_id: str):
        """Сигнал «пользователь появился» (сообщение в TG / поллинг веб-инбокса) —
        триггер утреннего приветствия фичи rhythm. Дёшев, не блокирует."""
        if self.rhythm is not None:
            try:
                self.rhythm.note_presence(chat_id)
            except Exception as e:
                logger.debug(f"[{self.persona_name}] note_presence: {e}")

    def on_user_message(self, chat_id: str):
        """Единая точка для «пришло сообщение пользователя»: note_presence
        ДО record_activity — иначе rhythm.note_presence через _last_seen()
        берёт max(presence_ts, activity_tracker.last_activity), а
        record_activity уже проставит last_activity=now до того, как rhythm
        посмотрит на разрыв — пауза окажется ≈0, и утреннее приветствие не
        сработает, сколько бы пользователь ни молчал. Порядок здесь
        гарантирован и не зависит от вызывающего — используй этот метод
        вместо раздельных record_activity/note_presence."""
        self.note_presence(chat_id)
        self.record_activity(chat_id)

    def record_topic(self, chat_id: str, topic_id: int):
        # Записывает ID топика для чата.
        if self._activity_tracker:
            self._activity_tracker.record_topic(chat_id, topic_id)

    def get_chat_topic(self, chat_id: str) -> Optional[int]:
        # Возвращает ID топика для чата.
        if self._activity_tracker:
            return self._activity_tracker.get_topic(chat_id)
        return None
