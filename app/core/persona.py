import time
from datetime import datetime

import yaml
from pathlib import Path
from typing import Optional, List, Dict

from app.core.language import detect_dialogue_language, response_language_note


def _format_msg_ts(ts) -> str:
    """Метка времени сообщения для LLM: «15.08 14:32» (+год, если не текущий).
    Пустая строка — если метки нет/битая. Локальное время сервера (как и
    везде: напоминания, env_context)."""
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
    year = f".{dt.year}" if dt.year != datetime.now().year else ""
    return dt.strftime(f"%d.%m{year} %H:%M")


def _ts_prefix(ts) -> str:
    """Готовый префикс «[15.08 14:32] » (или пустой)."""
    formatted = _format_msg_ts(ts)
    return f"[{formatted}] " if formatted else ""


class PersonaLayer:
    def __init__(self, persona_name: str = "connor"):
        self.persona_name = persona_name
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

        persona_dir = Path(__file__).parent.parent / "personas"
        persona_path = persona_dir / f"{name}.yaml"

        if not persona_path.exists():
            print(f"Файл не найден: {persona_path}.")
            return {
                "system_prompt": "",
                "settings": {}
            }
        
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
        # релевантные записи собирает app.features.glossary_context и
        # bot_instance добавляет их в book_context. Здесь только проверяем,
        # что файл существует.
        glossary_file = data.get("glossary")
        if glossary_file and not (persona_dir / glossary_file).exists():
            print(f"[PersonaLayer] Glossary не найден: {persona_dir / glossary_file}")

        return data

    def available_personas(self) -> List[str]:
        personas_dir = Path(__file__).parent.parent / "personas"
        if not personas_dir.exists():
            return []
        
        files = list(personas_dir.glob("*.yaml"))
        return [f.stem for f in files]
    
    
    def _get_special_user_note(self, user_id: str) -> Optional[str]:
        # Если user_id совпадает со special_user из YAML — вернуть инструкцию.
        import os
        special_users = self.persona_data.get("special_users", [])
        for su in special_users:
            su_id = str(su.get("id", ""))
            # Поддержка ${ENV_VAR} в поле id
            if su_id.startswith("${") and su_id.endswith("}"):
                su_id = os.getenv(su_id[2:-1], "")
            # Однопользовательский веб-режим: собеседник один — особый пользователь он
            if not ((self.web_single_user and su_id) or (su_id and str(su_id) == str(user_id))):
                continue
            aliases = ", ".join(su.get("aliases", []))
            greeting = su.get("greeting", "")
            behavior = su.get("behavior", "")
            return (
                f"\n\nIMPORTANT: the current user has ID {user_id} — this is {aliases}.\n"
                f"Greeting: {greeting}\n"
                f"Behavior: {behavior}"
            )
        return None

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
                         book_context: Optional[str] = None,
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

        # Стилевое ограничение помощи по intellect tier (§4 плана уровней
        # интеллекта): подставляется ТОЛЬКО на help-запросах — в остальное
        # время тон персоны не трогается
        if help_style_context:
            context_block += f"\n\n{help_style_context}"

        # Живой контекст персоны (state/world/offline-факты, план «живой» персоны):
        # что персонаж делал и как себя чувствовал между сообщениями
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

        # Контекст из книги (RAG по Lord of the Mysteries)
        if book_context:
            context_block += (
                f"\n\n{book_context}\n\n"
                "ПРАВИЛА РАБОТЫ С ФРАГМЕНТАМИ:\n"
                "1. Фрагменты — твой ЕДИНСТВЕННЫЙ источник фактов о мире книги.\n"
                "2. Язык ответа — язык сообщения пользователя.\n"
                "3. НЕ цитируй дословно — пересказывай суть своими словами.\n"
                "4. НЕ упоминай «база данных», «фрагменты», «поиск» — говори как знаток.\n"
                "5. Собирай ответ из нескольких фрагментов — не жди что всё в одном."
            )

        # Веб-контекст (результаты поиска DuckDuckGo)
        if web_context:
            context_block += f"""

WEB SEARCH RESULTS:
{web_context}

SOURCE PRIORITY:
1. If the answer is in memory (LTM facts) or uploaded files — use those, ignore the web search.
2. If memory and files do not contain the answer — use the web search data.
3. Do not mention internet sources if the answer came from memory/files.
4. If you use web search data — answer in the user's language, cite sources when appropriate.
5. If the question concerns personal feelings, use data from self memory.
"""

        # Добавляем идентификацию специального пользователя
        special_note = ""
        if user_id:
            special_note = self._get_special_user_note(user_id) or ""

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
            {"role": "system", "content": self.system_prompt + context_block + special_note + env_note + timestamps_note + todo_note + reminder_note + learning_note + inventory_note + inventory_events_note + computer_control_note + conv_style_note + language_note},
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
                    uid_tag = f" (ID:{uid})" if uid else ""
                    content = f"{prefix}[{name}{uid_tag}]: {msg['content']}"
                    messages.append({"role": "user", "content": content})
                else:
                    messages.append({"role": msg["role"], "content": f"{prefix}{msg['content']}"})

        # Последнее (текущее) сообщение — всегда с именем, ID отправителя и меткой времени
        # Перед основным ответом вставляем текст из сообщения, на которое пользователь ответил
        if reply_context:
            user_message = f"[Reply to message: {reply_context}]\n{user_message}"

        now_prefix = _ts_prefix(time.time())
        if current_sender_name and current_sender_id:
            uid_tag = f" (ID:{current_sender_id})"
            formatted = f"{now_prefix}[{current_sender_name}{uid_tag}]: {user_message}"
        else:
            formatted = f"{now_prefix}{user_message}"
        messages.append({"role": "user", "content": formatted})
        return messages

    def change_persona(self, persona_name: str) -> bool:
        # Сменить персону. Возвращает True если персона успешно загружена.
        
        persona_path = Path(__file__).parent.parent / "personas" / f"{persona_name}.yaml"
        if not persona_path.exists():
            return False
        
        self.persona_name = persona_name
        self.persona_data = self._load_persona(persona_name)
        self.system_prompt = self.persona_data.get("system_prompt", "")
        self.settings = self.persona_data.get("settings", {})
        return True