import os
import time

import yaml
from typing import Optional, List, Dict

from app.core import timeutil
from app.core.addons import CORE_PERSONAS_DIR, find_persona_file, persona_dirs
from app.core.language import detect_dialogue_language, response_language_note


def _format_msg_ts(ts) -> str:
    """Метка времени сообщения для LLM: «15.08 14:32» (+год, если не текущий).
    Пустая строка — если метки нет/битая. Время пользователя (TIMEZONE) —
    как и везде: напоминания, env_context."""
    if not ts:
        return ""
    try:
        dt = timeutil.from_ts(float(ts))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    year = f".{dt.year}" if dt.year != timeutil.today().year else ""
    return dt.strftime(f"%d.%m{year} %H:%M")


def _ts_prefix(ts) -> str:
    # Готовый префикс «[15.08 14:32] » (или пустой).
    formatted = _format_msg_ts(ts)
    return f"[{formatted}] " if formatted else ""


def addressee_note(persona, chat_id=None, user_id=None) -> str:
    """Нота для сообщений, которые бот пишет сам (инициатива, напоминание,
    утро/ночь): адресат — особый пользователь (special_users)? Без неё
    модель писала бы ему как обычному: промпт фона — голый system_prompt
    без заметки «кто пишет». Адресат — user_id, иначе chat_id (в личке это
    одно и то же; группа без user_id — пусто). Обычный адресат и персоны
    без special_users (и заглушки без метода) — пустая строка."""
    get = getattr(persona, "special_user", None)
    if not callable(get):
        return ""
    uid = user_id if user_id not in (None, "") else chat_id
    try:
        su = get(uid)
        who = persona._who_special(su) if isinstance(su, dict) else ""
    except Exception:
        return ""
    if not who or not isinstance(who, str):
        return ""
    return (f"\n\nWHO RECEIVES THIS MESSAGE: {who} — your special user. "
            "Write to them exactly as your persona instructions say for the "
            "special user (tone, forms of address, rules), not as to a regular user.")


# Как часто заглядывать в YAML за свежей заморозкой (features.muted)
_MUTED_RECHECK_SEC = 5.0


class PersonaLayer:
    def __init__(self, persona_name: str = "connor"):
        self.persona_name = persona_name
        # mtime YAML на момент чтения и время последней сверки — для is_muted()
        self._yaml_mtime: Optional[float] = None
        self._muted_checked_at = float("-inf")
        self.persona_data = self._load_persona(persona_name)
        self.system_prompt = self.persona_data.get("system_prompt", "")
        self.settings = self.persona_data.get("settings", {})
        # Однопользовательский режим (веб/API): единственный собеседник —
        # это и есть особый пользователь/владелец (выставляет API-реестр)
        self.web_single_user = False

    def get_settings(self) -> dict:
        # Значения по умолчанию, если не указаны в yaml
        return {
            "temperature": self.settings.get("temperature", 0.7),
            "max_tokens": self.settings.get("max_tokens", 2000),
            "top_p": self.settings.get("top_p", 0.9)
        }
    
    def _load_persona(self, name: str) -> Dict:
        # YAML ищется в папке пользователя, app/personas и папках персон аддонов
        persona_path = find_persona_file(name) or CORE_PERSONAS_DIR / f"{name}.yaml"
        persona_dir = persona_path.parent

        if not persona_path.exists():
            print(f"Файл не найден: {persona_path}.")
            return {
                "system_prompt": "",
                "settings": {}
            }
        
        try:
            self._yaml_mtime = persona_path.stat().st_mtime
        except OSError:
            self._yaml_mtime = None
        with open(persona_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # Пустой файл → None, файл со списком верхнего уровня → list:
        # оба случая ломали бы .get() ниже по коду
        if not isinstance(data, dict):
            print(f"Персона '{name}' пуста или имеет неверный формат ({persona_path}).")
            return {
                "system_prompt": "",
                "settings": {}
            }

        # Загружаем glossary если указан — НЕ в системный промпт целиком
        # (~17k токенов на каждое сообщение), а динамически по вопросу:
        # релевантные записи собирает книжный аддон и добавляет их в свой
        # блок промпта. Здесь только проверяем, что файл существует.
        glossary_file = data.get("glossary")
        if glossary_file and not (persona_dir / glossary_file).exists():
            print(f"[PersonaLayer] Glossary не найден: {persona_dir / glossary_file}")

        return data

    def is_muted(self) -> bool:
        """Заморожена ли персона (features.muted) — с подхватом правки YAML на
        живую. Заморозку из веба пишет API-процесс, а Telegram-боты работают
        в другом процессе и читали YAML только при старте: без этой сверки
        замороженная персона отвечала и слала напоминания до рестарта.
        YAML перечитывается только при смене mtime и не чаще раза в
        _MUTED_RECHECK_SEC; из него берётся лишь флаг muted."""
        now = time.monotonic()
        if now - getattr(self, "_muted_checked_at", float("-inf")) >= _MUTED_RECHECK_SEC:
            self._muted_checked_at = now
            self._sync_muted_from_yaml()
        return bool((self.persona_data.get("features") or {}).get("muted"))

    def _sync_muted_from_yaml(self) -> None:
        path = find_persona_file(self.persona_name)
        if path is None:
            return
        try:
            mtime = path.stat().st_mtime
            if mtime == getattr(self, "_yaml_mtime", None):
                return
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception:
            return  # файл переписывают прямо сейчас — сверимся в следующий раз
        self._yaml_mtime = mtime
        if not isinstance(data, dict):
            return
        muted = bool((data.get("features") or {}).get("muted"))
        features = self.persona_data.get("features")
        if not isinstance(features, dict):
            if not muted:
                return
            features = {}
            self.persona_data["features"] = features
        if bool(features.get("muted")) != muted:
            features["muted"] = muted

    def available_personas(self) -> List[str]:
        # Персона — YAML с непустым system_prompt: рядом лежат служебные
        # файлы (глоссарий, таймлайн), они не персоны
        names: List[str] = []
        for personas_dir in persona_dirs():
            if not personas_dir.exists():
                continue
            for f in sorted(personas_dir.glob("*.yaml")):
                if f.stem in names:
                    continue
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        data = yaml.safe_load(fh)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("system_prompt"):
                    names.append(f.stem)
        return names
    
    
    # ── особые пользователи (special_users в YAML) ──
    # Узнаются только по ID аккаунта: имя в мессенджере любое, а назваться
    # кем угодно может каждый. Модели это говорится явно в каждом ходе —
    # кто пишет сейчас и чьи реплики в истории, — иначе она узнавала
    # особого пользователя на вопрос «кто я?», а в остальных ответах
    # говорила с ним как с обычным

    def _special_entries(self) -> List[Dict]:
        # special_users с раскрытым id (${ENV_VAR}); записи без id не действуют
        out = []
        for su in self.persona_data.get("special_users") or []:
            if not isinstance(su, dict):
                continue
            su_id = str(su.get("id", "") or "").strip()
            if su_id.startswith("${") and su_id.endswith("}"):
                su_id = os.getenv(su_id[2:-1], "").strip()
            if su_id:
                out.append(dict(su, id=su_id))
        return out

    def special_user(self, user_id) -> Optional[Dict]:
        """Запись special_users для user_id; None — обычный собеседник.
        Однопользовательский веб-режим: единственный собеседник — он и есть
        особый пользователь (первый из списка)."""
        entries = self._special_entries()
        if self.web_single_user:
            return entries[0] if entries else None
        uid = str(user_id or "")
        return next((e for e in entries if uid and e["id"] == uid), None)

    @staticmethod
    def _special_names(su: Dict) -> List[str]:
        return [str(a).strip() for a in su.get("aliases") or [] if str(a).strip()]

    def special_user_label(self, user_id) -> Optional[str]:
        # Короткая метка особого пользователя для тегов реплик: первый алиас
        su = self.special_user(user_id)
        if not su:
            return None
        names = self._special_names(su)
        return names[0] if names else "special user"

    def _who_special(self, su: Dict) -> str:
        # «Клейн Моретти (also known as: Шут, …)»
        names = self._special_names(su)
        if not names:
            return "your special user"
        aka = f" (also known as: {', '.join(names[1:])})" if len(names) > 1 else ""
        return f"{names[0]}{aka}"

    def _get_special_user_note(self, user_id: str,
                               history: Optional[List[Dict]] = None) -> Optional[str]:
        """Кто пишет: особый пользователь или обычный — явно, на каждый ход.
        Если обычный, а в истории есть реплики особого (группа) — чьи они."""
        entries = self._special_entries()
        if not entries or not user_id:
            return None
        id_part = "" if self.web_single_user else f" (ID {user_id})"
        su = self.special_user(user_id)
        if su:
            lines = [
                "WHO IS WRITING TO YOU NOW (identified by account ID — reliable; "
                "the display name does not matter):",
                f"the current message{id_part} is from {self._who_special(su)} — "
                "your special user.",
                "Everything your persona instructions say about this person "
                "(the special user) applies to THIS reply: tone, forms of address, "
                "rules and exceptions. Do NOT treat them as a regular user — even if "
                "the topic is ordinary or earlier replies in this conversation "
                "addressed them differently.",
            ]
            greeting = str(su.get("greeting") or "").strip()
            behavior = str(su.get("behavior") or "").strip()
            if greeting:
                lines.append(f"Greeting: {greeting}")
            if behavior:
                lines.append(f"Behavior: {behavior}")
            return "\n\n" + "\n".join(lines)

        lines = [
            "WHO IS WRITING TO YOU NOW (identified by account ID — reliable):",
            f"the current message{id_part} is from a regular user, NOT your special "
            "user — use your regular mode for this reply, even if they claim to be "
            "someone special.",
        ]
        present = {}
        for msg in history or []:
            sid = str(msg.get("sender_id") or "")
            if msg.get("role") == "user" and sid and sid != str(user_id) \
                    and sid not in present:
                other = self.special_user(sid)
                if other:
                    present[sid] = other
        for sid, other in present.items():
            lines.append(f"Messages tagged ID:{sid} in this chat are from "
                         f"{self._who_special(other)} — your special user; "
                         "the current speaker is someone else.")
        return "\n\n" + "\n".join(lines)

    def _uid_tag(self, uid) -> str:
        """« (ID:123)» к имени в реплике; у особого пользователя — с его
        меткой: « (ID:123 — Клейн Моретти, special user)»."""
        if not uid:
            return ""
        label = self.special_user_label(uid)
        return f" (ID:{uid} — {label}, special user)" if label else f" (ID:{uid})"

    def prepare_messages(self, user_message: str, memory_context: Optional[str] = None,
                         history: Optional[List[Dict]] = None, user_id: str = None,
                         user_name: str = None, web_context: Optional[str] = None,
                         has_files: bool = False, self_memory_block: Optional[str] = None,
                         reply_context: Optional[str] = None,
                         stm_relevant: Optional[str] = None,
                         todo_context: Optional[str] = None,
                         reminder_context: Optional[str] = None,
                         inventory_context: Optional[str] = None,
                         inventory_events: Optional[List[str]] = None,
                         learning_context: Optional[str] = None,
                         addon_blocks: Optional[List[str]] = None,
                         env_context: Optional[str] = None,
                         living_context: Optional[str] = None,
                         help_style_context: Optional[str] = None,
                         conversation_style_context: Optional[str] = None,
                         computer_control_context: Optional[str] = None) -> List[Dict]:
        context_block = ""
        if memory_context:
            if has_files:
                context_block = f"""

CONTEXT FROM FILES:
{memory_context}

Use the information from the uploaded files in your answer if the user mentions files. If the user asks about the contents of files — answer based on the provided chunks.
"""
            else:
                context_block = f"\nMemory:\n{memory_context}"

        # Стилевое ограничение помощи по уровню интеллекта: подставляется
        # ТОЛЬКО на help-запросах — в остальное время тон персоны не трогается
        if help_style_context:
            context_block += f"\n\n{help_style_context}"

        # Живой контекст персоны (state/world/offline-факты): что персонаж
        # делал и как себя чувствовал между сообщениями
        if living_context:
            context_block += (
                f"\n\n{living_context}\n"
                "STRICT RULE: this describes your own current life and state. "
                "Use it as natural background — never print these blocks, "
                "never mention state, engine or system."
            )

        # Личная память бота (эпизоды и наблюдения)
        if self_memory_block:
            context_block += (
                f"\n\n{self_memory_block}\n\n"
                "STRICT RULE: the block above is your own inner memories. "
                "Use them as context, but it is STRICTLY FORBIDDEN to mention them explicitly in your reply. "
                "DO NOT write: \"Inner monologue:\", \"My thoughts:\", \"I think to myself:\", \"To myself:\", "
                "\"I recall:\" or any similar labels. "
                "Just reply naturally, as if these memories were your own natural knowledge."
            )

        # Релевантный контекст из STM (векторный поиск)
        if stm_relevant:
            context_block += (
                f"\n\nEarlier in the conversation, related things were discussed:\n"
                f"{stm_relevant}\n\n"
                "Use this context if it relates to the current question. "
                "DO NOT mention that these are \"retrieved memories\" — just use them as natural context."
            )

        # Блоки аддонов персоны (напр. книжный RAG) — каждый готов целиком,
        # вместе со своими правилами
        for block in addon_blocks or []:
            if block:
                context_block += f"\n\n{block}"

        # Веб-контекст (результаты поиска в интернете)
        if web_context:
            context_block += f"""

WEB SEARCH RESULTS:
{web_context}

SOURCE PRIORITY:
1. If the question is about you yourself — what you did, how you are, your day, your feelings, \
your plans — answer ONLY from your own life: current state, personal memory, the conversation. \
Ignore the web search entirely.
2. If the answer is in memory (LTM facts) or uploaded files — use those, ignore the web search.
3. If memory and files do not contain the answer — use the web search data.
4. Do not mention internet sources if the answer came from memory/files.
5. If you use web search data — answer in the user's language, cite sources when appropriate.
"""

        # Кто пишет (особый пользователь или обычный) — по ID; в системном
        # блоке ближе к концу (после книжного/веб-контекста), чтобы не
        # тонуть в длинных блоках выше
        special_note = ""
        if user_id:
            special_note = self._get_special_user_note(user_id, history) or ""

        # Окружение пользователя: город, его локальное время и погода (одна строка)
        env_note = ""
        if env_context:
            env_note = (
                f"\n\nUser's current environment: {env_context}\n"
                "STRICT RULE: this is background information — use it only to understand "
                "the time of day and give time-appropriate replies. It is STRICTLY FORBIDDEN "
                "to be the first to mention the city, location, time or weather — talk about them "
                "ONLY if the user themselves asked about the weather/time or brought up "
                "their own location."
            )

        # Todo-контекст: инструкция для LLM + текущий список
        todo_note = ""
        if todo_context:
            todo_note = (
                "\n\nThe user has a shared todo list for this chat. "
                "If they ask to write something down, add it, or mark it as a task — "
                "extract the clean task text from their message and append a marker at the end of your reply: "
                "[TODO_ADD:task text]. "
                "If they ask to remove, cross out, or mark something as done — "
                "append the marker [TODO_DONE:N] where N is the item number from the list. "
                "The current todo list will be shown to the user automatically — DO NOT print it yourself. "
                "If they simply ask for the list — show it without a marker.\n\n"
                f"Current todo list:\n{todo_context}\n"
            )

        # Reminder-контекст: напоминание уже запланировано — просто подтверди
        reminder_note = ""
        if reminder_context:
            reminder_note = (
                f"\n\n{reminder_context}\n"
                "You CAN set reminders and write first: the system delivers your "
                "message automatically at the scheduled time. NEVER claim that you "
                "cannot remind the user or cannot write first.\n"
            )

        # Learning-контекст: режим обучения (уточнение частоты, оценка теста, подтверждение)
        learning_note = ""
        if learning_context:
            learning_note = f"\n\n{learning_context}\n"

        # Inventory-контекст: вещи бота
        inventory_note = ""
        if inventory_context:
            inventory_note = (
                "\n\nThis is your personal inventory — things that you have. "
                "You may mention them in your replies naturally, as part of your persona. "
                "If the user asks you to take, receive or put on something — "
                "come up with a short description of the item and append the marker [INVENTORY_ADD:Name in base form:description]. "
                "IMPORTANT: the name in the marker must be in the base (nominative) form. "
                "IMPORTANT: the description must be meaningful, do not leave it empty. "
                "For example, if the user says 'take the key' — marker: [INVENTORY_ADD:Key:a small metal door key]. "
                "If 'here, a red ball' — marker: [INVENTORY_ADD:Red ball:a bright rubber ball for playing]. "
                "If 'here's some whiskey' — marker: [INVENTORY_ADD:Whiskey:a bottle of Scotch whiskey, strong alcohol]. "
                "If they ask to throw away or remove something — append the marker [INVENTORY_REMOVE:Name in base form]. "
                "If the item can spoil — add an expiration date: [INVENTORY_ADD:Name:description:YYYY-MM-DD]. "
                "For example: [INVENTORY_ADD:Apple:a fresh red apple:2026-06-25]. "
                "IMPORTANT: without the marker the item will NOT be saved. The marker is mandatory. "
                "The current inventory will be shown to the user automatically after your reply — DO NOT print it yourself. "
                "If they simply ask what you have — list it without markers.\n\n"
                f"{inventory_context}\n"
            )

        # События инвентаря (предмет использован, просрочился) — LLM должен отреагировать
        inventory_events_note = ""
        if inventory_events:
            events_text = "\n".join(f"- {e}" for e in inventory_events)
            inventory_events_note = (
                "\n\nIMPORTANT EVENTS (react naturally, in your own style):\n"
                f"{events_text}\n"
                "This just happened. React to it in your reply — "
                "as a character, not as a robot. DO NOT write technical details."
            )

        # Пояснение меток времени: каждая реплика ниже начинается с [DD.MM HH:MM] —
        # дата и время отправки (24ч, локальное время). Год добавляется, если не текущий.
        timestamps_note = (
            "\n\nEvery message in the dialogue below starts with a [DD.MM HH:MM] tag — "
            "the date and time when that message was sent (24-hour local time). "
            "Use these timestamps to understand how recent or old each message is "
            "(for example: the user replied only the next day, or asked about this an hour ago). "
            "Do NOT copy the tags into your own replies."
        )

        # Computer control: инструкция о маркерах управления компьютером
        # (+ результат подтверждения, если это ответ на «выполнить?»)
        computer_control_note = ""
        if computer_control_context:
            computer_control_note = f"\n\n{computer_control_context}"

        # Платформенное правило финальных вопросов (conversation_style): идёт
        # ПОСЛЕДНИМ в системном блоке — инструкции ближе к месту генерации
        # модель выполняет стабильнее
        conv_style_note = ""
        if conversation_style_context:
            conv_style_note = f"\n\n{conversation_style_context}"

        # Правило языка ответа: бот всегда отвечает на языке, на котором
        # пишет пользователь, независимо от языка любых инструкций в промпте
        # (персона, стилевые ноты, книжный RAG). Идёт САМЫМ ПОСЛЕДНИМ —
        # перекрывает язык всех блоков выше. Язык берём из текущего
        # сообщения (до обёртки reply_context), фолбэк — последние
        # сообщения этого же пользователя из истории.
        language_note = response_language_note(
            detect_dialogue_language(user_message, history, user_id)
        ) or ""

        messages = [
            {"role": "system", "content": self.system_prompt + context_block + env_note + timestamps_note + todo_note + reminder_note + learning_note + inventory_note + inventory_events_note + computer_control_note + special_note + conv_style_note + language_note},
        ]

        # Определяем, является ли текущее сообщение от именованного пользователя (групповой чат)
        current_sender_name = user_name
        current_sender_id = user_id

        # История диалога из STM (исключаем последнее сообщение — текущее user_message).
        # Каждое сообщение (и пользователя, и бота) помечается временем отправки —
        # модель видит, когда была каждая реплика: «[15.08 14:32] [Имя (ID:1)]: …».
        if history and len(history) > 1:
            for msg in history[:-1]:
                prefix = _ts_prefix(msg.get("timestamp"))
                if msg["role"] == "user":
                    name = msg.get("user_name", "User")
                    uid = msg.get("sender_id", "")
                    uid_tag = self._uid_tag(uid)
                    content = f"{prefix}[{name}{uid_tag}]: {msg['content']}"
                    messages.append({"role": "user", "content": content})
                else:
                    messages.append({"role": msg["role"], "content": f"{prefix}{msg['content']}"})

        # Перед основным ответом вставляем текст из сообщения, на которое пользователь ответил
        if reply_context:
            user_message = f"[Reply to message: {reply_context}]\n{user_message}"

        # Последнее (текущее) сообщение — всегда с именем, ID отправителя и меткой времени
        now_prefix = _ts_prefix(time.time())
        if current_sender_name and current_sender_id:
            uid_tag = self._uid_tag(current_sender_id)
            formatted = f"{now_prefix}[{current_sender_name}{uid_tag}]: {user_message}"
        else:
            formatted = f"{now_prefix}{user_message}"
        messages.append({"role": "user", "content": formatted})
        return messages

    def change_persona(self, persona_name: str) -> bool:
        # Сменить персону. Возвращает True если персона успешно загружена.
        
        if find_persona_file(persona_name) is None:
            return False
        
        self.persona_name = persona_name
        self.persona_data = self._load_persona(persona_name)
        self.system_prompt = self.persona_data.get("system_prompt", "")
        self.settings = self.persona_data.get("settings", {})
        return True