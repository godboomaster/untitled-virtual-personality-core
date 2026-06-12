import yaml
from pathlib import Path
from typing import Optional, List, Dict

class PersonaLayer:
    def __init__(self, persona_name: str = "connor"):
        self.persona_name = persona_name
        self.persona_data = self._load_persona(persona_name)
        self.system_prompt = self.persona_data.get("system_prompt", "")
        self.settings = self.persona_data.get("settings", {}) 

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
            return yaml.safe_load(f)

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
            if su_id and str(su_id) == str(user_id):
                aliases = ", ".join(su.get("aliases", []))
                greeting = su.get("greeting", "")
                behavior = su.get("behavior", "")
                return (
                    f"\n\nВАЖНО: текущий пользователь имеет ID {user_id} — это {aliases}.\n"
                    f"Приветствие: {greeting}\n"
                    f"Поведение: {behavior}"
                )
        return None

    def prepare_messages(self, user_message: str, memory_context: Optional[str] = None,
                         history: Optional[List[Dict]] = None, user_id: str = None,
                         user_name: str = None, web_context: Optional[str] = None,
                         has_files: bool = False, self_memory_block: Optional[str] = None,
                         reply_context: Optional[str] = None,
                         stm_relevant: Optional[str] = None,
                         todo_context: Optional[str] = None,
                         inventory_context: Optional[str] = None) -> List[Dict]:
        context_block = ""
        if memory_context:
            if has_files:
                context_block = f"""

КОНТЕКСТ ИЗ ФАЙЛОВ:
{memory_context}

Используй информацию из загруженных файлов для ответа если пользователь упоминает файлы. Если пользователь спрашивает о содержании файлов — отвечай на основе предоставленных чанков.
"""
            else:
                context_block = f"\nПамять:\n{memory_context}"

        # Личная память бота (эпизоды и наблюдения)
        if self_memory_block:
            context_block += (
                f"\n\n{self_memory_block}\n\n"
                "СТРОГОЕ ПРАВИЛО: блок выше — твои внутренние воспоминания. "
                "Используй их как контекст, но КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО явно упоминать их в ответе. "
                "НЕ пиши: «Внутренний монолог:», «Мои мысли:», «Я думаю про себя:», «Про себя:», "
                "«Вспоминаю:» или любые подобные пометки. "
                "Просто отвечай естественно, как если бы эти воспоминания были твоими естественными знаниями."
            )

        # Релевантный контекст из STM (векторный поиск)
        if stm_relevant:
            context_block += (
                f"\n\nРанее в разговоре обсуждалось связанное:\n"
                f"{stm_relevant}\n\n"
                "Используй этот контекст если он относится к текущему вопросу. "
                "НЕ упоминай что это «извлечённые воспоминания» — просто используй как естественный контекст."
            )

        # Веб-контекст (результаты поиска DuckDuckGo)
        if web_context:
            context_block += f"""

РЕЗУЛЬТАТЫ ВЕБ-ПОИСКА:
{web_context}

ПРИОРИТЕТ ИСТОЧНИКОВ:
1. Если ответ есть в памяти (факты LTM) или загруженных файлах — используй их, веб-поиск игнорируй.
2. Если память и файлы не содержат ответа — используй данные из веб-поиска.
3. Не упоминай источники из интернета если ответ взят из памяти/файлов.
4. Если используешь данные из веб-поиска — отвечай на языке пользователя, источники указывай если уместно.
5. Если вопрос качается личных чувств, используй данные из self memory.
"""

        # Добавляем идентификацию специального пользователя
        special_note = ""
        if user_id:
            special_note = self._get_special_user_note(user_id) or ""

        # Todo-контекст: инструкция для LLM + текущий список
        todo_note = ""
        if todo_context:
            todo_note = (
                "\n\nУ пользователя есть общий список дел для этого чата. "
                "Если он просит что-то записать, добавить, напомнить или отметить как дело — "
                "извлеки чистый текст задачи из его сообщения и в конце своего ответа добавь маркер: "
                "[TODO_ADD:текст задачи]. "
                "После маркера выведи актуальный список дел. "
                "Если он просто спрашивает список — покажи его без маркера.\n\n"
                f"Текущий список дел:\n{todo_context}\n"
            )

        # Inventory-контекст: вещи бота
        inventory_note = ""
        if inventory_context:
            inventory_note = (
                "\n\nЭто твой личный инвентарь — вещи, которые у тебя есть. "
                "Ты можешь упоминать их в ответах естественно, как часть своего образа. "
                "Если пользователь просит взять, получить или надеть что-то — "
                "придумай краткое описание предмета и добавь маркер [INVENTORY_ADD:Название в именительном падеже:описание]. "
                "ВАЖНО: название в маркере должно быть в именительном падеже (кто? что?). "
                "Например, если пользователь говорит 'возьми ключ' — маркер: [INVENTORY_ADD:Ключ:маленький металлический ключ]. "
                "Если 'держи красный шар' — маркер: [INVENTORY_ADD:Красный шар:яркий резиновый шар]. "
                "Если просит выбросить или убрать — добавь маркер [INVENTORY_REMOVE:Название в именительном падеже]. "
                "ВАЖНО: без маркера предмет НЕ сохранится. Маркер обязателен. "
                "Если просто спрашивает что у тебя есть — перечисли без маркеров.\n\n"
                f"{inventory_context}\n"
            )

        messages = [
            {"role": "system", "content": self.system_prompt + context_block + special_note + todo_note + inventory_note},
        ]

        # Определяем, является ли текущее сообщение от именованного пользователя (групповой чат)
        current_sender_name = user_name
        current_sender_id = user_id

        # История диалога из STM (исключаем последнее сообщение — текущее user_message)
        if history and len(history) > 1:
            for msg in history[:-1]:
                if msg["role"] == "user":
                    name = msg.get("user_name", "Пользователь")
                    uid = msg.get("sender_id", "")
                    uid_tag = f" (ID:{uid})" if uid else ""
                    content = f"[{name}{uid_tag}]: {msg['content']}"
                    messages.append({"role": "user", "content": content})
                else:
                    messages.append({"role": msg["role"], "content": msg["content"]})

        # Последнее (текущее) сообщение — всегда с именем и ID отправителя
        # Перед основным ответом вставляем текст из сообщения, на которое пользователь ответил
        if reply_context:
            user_message = f"[Ответ на сообщение: {reply_context}]\n{user_message}"

        if current_sender_name and current_sender_id:
            uid_tag = f" (ID:{current_sender_id})"
            formatted = f"[{current_sender_name}{uid_tag}]: {user_message}"
        else:
            formatted = user_message
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