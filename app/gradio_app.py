"""
Gradio-интерфейс для Virtual Personality.
Запускается отдельно от Telegram-ботов:

    python -m app.gradio_app

Использует свой контекст "gradio" — отдельная база данных от Telegram.
"""

import gradio as gr
import yaml
import shutil
import os
from pathlib import Path
from typing import Optional, Tuple, List
from concurrent.futures import ThreadPoolExecutor

from app.core.persona import PersonaLayer
from app.core.memory import MemoryManager
from app.core.router import ModelRouter
from app.core.config import Config
from app.core.file_vector_db import FileVectorDB
from app.core.file_reader import extract_text
from app.features.file_sender import prepare_response, cleanup_files


# Директория для файлов, отправляемых из чата
GRADIO_FILES_DIR = Path(__file__).parent / "gradio_output_files"
GRADIO_FILES_DIR.mkdir(exist_ok=True)

# User ID владельца для Gradio (всегда owner)
GRADIO_USER_ID = os.getenv("OWNER_USER_ID", "gradio_owner")

# Специальный ID для Арродес (если есть в env)
ARRODES_SPECIAL_ID = os.getenv("ARRODES_SPECIAL_USER_ID", "")


class GradioBot:
    """Обёртка для Gradio — один экземпляр с переключаемой персоной.
    Features читаются из YAML персоны, как в BotInstance."""

    def __init__(self):
        self.persona_name = "connor"
        self.context = "gradio"
        self.persona = PersonaLayer(persona_name=self.persona_name)
        self._init_from_yaml()

    def _init_from_yaml(self):
        """Инициализация компонентов на основе features из YAML персоны.
        Контекст = gradio_{persona_name} — отдельная база для каждой персоны."""
        persona_data = self.persona.persona_data
        self.features: dict = persona_data.get("features", {})

        # Контекст — отдельная база для каждой персоны в Gradio
        self.context = f"gradio_{self.persona_name}"

        # STM size из YAML (fallback на Config)
        self.stm_size: int = persona_data.get("stm_size", Config.STM_SIZE)

        # Max docs из YAML (fallback на дефолт 3)
        self.max_docs: int = persona_data.get("max_docs", 3)

        # Router
        self.router = ModelRouter()

        # Memory
        self.memory = MemoryManager(
            stm_size=self.stm_size,
            enable_ltm_extraction=Config.LTM_EXTRACTION_ENABLED,
            ltm_model_provider=Config.LTM_MODEL_PROVIDER,
            load_stm_from_db=True,
            context=self.context,
            main_router=self.router
        )

        # File DB (только если file_upload в features)
        self.file_db: Optional[FileVectorDB] = None
        if self.features.get("file_upload", False):
            self.file_db = FileVectorDB(context=self.context, max_docs=self.max_docs)

        # Web search
        self._web_search_enabled = self.features.get("web_search", False)
        self._web_search_disabled = False
        self._web_pool = None
        if self._web_search_enabled:
            from app.features.web_search import search_web, format_web_results
            self._search_web = search_web
            self._format_web_results = format_web_results
            self._web_pool = ThreadPoolExecutor(max_workers=1)

        # Self memory (эпизодическая память бота)
        self.self_memory = None
        if self.features.get("self_memory", False):
            from app.core.self_memory import BotSelfMemory
            self.self_memory = BotSelfMemory(
                context=self.context,
                persona_name=self.persona_name,
                router=self.router
            )

    def _get_user_id(self) -> str:
        """Возвращает user_id для текущей персоны.
        Для Арродес — ARRODES_SPECIAL_USER_ID (если задан), иначе GRADIO_USER_ID."""
        if self.persona_name == "arrodes" and ARRODES_SPECIAL_ID:
            return ARRODES_SPECIAL_ID
        return GRADIO_USER_ID

    def chat(self, message: str, history: list) -> Tuple[list, str, Optional[List[str]]]:
        """
        Обрабатывает сообщение и возвращает:
        - обновленную историю чата (в формате messages для gr.Chatbot)
        - текст для поля ввода (очищаем)
        - список путей к файлам (если есть код) или None
        """
        if not message or not message.strip():
            return history, "", None

        user_id = self._get_user_id()

        # Добавляем сообщение пользователя
        history = history + [{"role": "user", "content": message}]

        # Генерируем ответ
        answer = self.process_message(message, user_id=user_id)

        # Анализируем ответ — нужны ли файлы (как в Telegram)
        try:
            msg_parts, files = prepare_response(answer)
        except Exception:
            msg_parts = [answer]
            files = None

        # Формируем ответ бота
        bot_text = "\n\n".join(msg_parts) if msg_parts else answer

        # Добавляем ответ ассистента
        history = history + [{"role": "assistant", "content": bot_text}]

        # Если есть файлы — копируем в gradio_output_files для отображения
        file_paths = []
        if files:
            for filepath, filename in files:
                dest = GRADIO_FILES_DIR / filename
                # Если файл уже есть — добавляем номер
                counter = 1
                original_dest = dest
                while dest.exists():
                    stem = original_dest.stem
                    suffix = original_dest.suffix
                    dest = GRADIO_FILES_DIR / f"{stem}_{counter}{suffix}"
                    counter += 1
                shutil.copy2(filepath, dest)
                file_paths.append(str(dest))
            # Удаляем временные файлы
            cleanup_files(files)

        return history, "", file_paths if file_paths else None

    def process_message(self, user_input: str, user_id: str = None) -> str:
        # В Gradio используем _get_user_id() если не передан
        if user_id is None:
            user_id = self._get_user_id()
        # 1. Веб-поиск в фоне (если включен)
        web_future = None
        if self._web_search_enabled and not self._web_search_disabled:
            web_future = self._web_pool.submit(self._search_web, user_input, 5)

        # 2. Добавляем в память
        self.memory.add_message("user", user_input, user_id)

        # 3. Контекст из памяти
        stm_messages, ltm_facts, _stm_relevant = self.memory.get_context(user_id, ltm_query=user_input)

        # 4. Файлы (если включены)
        file_context = None
        if self.file_db:
            file_chunks = self.file_db.search(user_id=user_id, query=user_input, limit=5)
            if file_chunks:
                file_context = "Контекст из загруженных файлов:\n" + "\n---\n".join(file_chunks)

        # 5. Веб-контекст
        web_context = None
        if web_future is not None:
            try:
                results = web_future.result(timeout=10)
                if results:
                    web_context = self._format_web_results(results)
            except Exception:
                pass

        # 6. Self-memory контекст
        self_memory_block = None
        if self.self_memory:
            self_memory_block = self.self_memory.get_context_block()

        # 7. Общий контекст
        context_parts = []
        if ltm_facts:
            context_parts.append("\n".join(ltm_facts))
        if file_context:
            context_parts.append(file_context)
        if self_memory_block:
            context_parts.append(self_memory_block)
        memory_text = "\n\n".join(context_parts) if context_parts else None

        # 8. Persona + LLM
        has_files = file_context is not None
        messages = self.persona.prepare_messages(
            user_input, memory_text, history=stm_messages,
            user_id=user_id, has_files=has_files, web_context=web_context
        )
        settings = self.persona.get_settings()
        print(f"[Gradio] [Response] -> {self.router.get_provider_model_info()}")
        answer = self.router.get_response(messages, **settings)

        # 9. Сохраняем ответ
        self.memory.add_message("assistant", answer, user_id)

        # 10. Self-memory tick (фоновая обработка)
        if self.self_memory:
            self.self_memory.tick(stm_messages, user_id, user_input)

        return answer

    def get_memory_stats(self) -> str:
        stats = self.memory.get_stats(user_id=self._get_user_id())
        return (
            f"Краткосрочная: {stats['stm_count']}/{stats['stm_max']} сообщений\n"
            f"Долгосрочная: {stats['ltm_count']} фактов"
        )

    def clear_memory(self) -> tuple:
        self.memory.clear_stm()
        self.memory.clear_ltm(user_id=self._get_user_id())
        return "Память очищена", self.get_memory_stats()

    def change_persona(self, persona_name: str) -> str:
        success = self.persona.change_persona(persona_name)
        if success:
            self.persona_name = persona_name
            # Пересоздаем компоненты с новыми features
            self._init_from_yaml()
            return f"Персона изменена на: {persona_name}"
        return f"Ошибка: персона '{persona_name}' не найдена"

    def get_persona_info(self, persona_name: str) -> str:
        persona_path = Path(__file__).parent / "personas" / f"{persona_name}.yaml"
        if not persona_path.exists():
            return "Персона не найдена"
        with open(persona_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        description = data.get("description", "Описание отсутствует")
        features = data.get("features", {})
        enabled = [k for k, v in features.items() if v is True]
        features_text = f"\n\nВключены: {', '.join(enabled)}" if enabled else ""
        return f"**{persona_name}**\n\n{description}{features_text}"

    def available_personas(self) -> list:
        return self.persona.available_personas()

    # --- Web Search ---

    def toggle_web_search(self) -> str:
        if not self._web_search_enabled:
            return "Веб-поиск недоступен для этой персоны"
        self._web_search_disabled = not self._web_search_disabled
        status = "выключен" if self._web_search_disabled else "включён"
        return f"Веб-поиск {status}."

    def get_web_search_status(self) -> str:
        if not self._web_search_enabled:
            return "Недоступен (не включён в features персоны)"
        return "Включён" if not self._web_search_disabled else "Выключен"

    def is_web_search_available(self) -> bool:
        return self._web_search_enabled

    # --- File Upload ---

    def upload_file(self, file_path: str) -> str:
        if not file_path:
            return "Файл не выбран"
        if not self.file_db:
            return "Загрузка файлов недоступна для этой персоны (file_upload: false в YAML)"

        filename = Path(file_path).name
        user_id = self._get_user_id()
        try:
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            text = extract_text(file_bytes, filename)

            if text.startswith(("Ошибка", "Формат", "Не удалось", "Библиотека")):
                return text

            self.file_db.add_file(user_id=user_id, filename=filename, content=text)
            loaded = self.file_db.get_loaded_files(user_id)
            return f"Файл '{filename}' загружен. Всего файлов: {len(loaded)}/{self.max_docs}"
        except Exception as e:
            return f"Ошибка загрузки файла: {e}"

    def get_loaded_files(self) -> str:
        if not self.file_db:
            return "Загрузка файлов недоступна для этой персоны"
        files = self.file_db.get_loaded_files(self._get_user_id())
        if not files:
            return "Нет загруженных файлов"
        return "Загруженные файлы:\n" + "\n".join(f"- {f}" for f in files)

    def reset_files(self) -> str:
        if not self.file_db:
            return "Загрузка файлов недоступна для этой персоны"
        self.file_db.reset(user_id=self._get_user_id())
        return "Файловая база очищена"

    def is_file_upload_available(self) -> bool:
        return self.file_db is not None


# ─── Создаём бота ────────────────────────────────────
bot = GradioBot()

# ─── Gradio UI ───────────────────────────────────────
with gr.Blocks(title="Виртуальная Личность") as demo:
    gr.Markdown("# Виртуальная Личность")
    gr.Markdown("Чат с двухуровневой памятью (краткосрочная + долгосрочная)")

    with gr.Row():
        # Чат
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(
                height=500,
            )
            msg_input = gr.Textbox(
                label="Сообщение",
                placeholder="Введите сообщение...",
                lines=2
            )
            with gr.Row():
                send_btn = gr.Button("Отправить", variant="primary")
                clear_chat_btn = gr.Button("Очистить чат")

            # Файлы с кодом (появляются когда LLM генерирует код)
            output_files = gr.Files(
                label="Файлы с кодом",
                visible=False,
                interactive=False
            )

            def on_send(message, history):
                history, cleared_msg, file_paths = bot.chat(message, history)
                # Показываем файлы если есть
                has_files = file_paths is not None and len(file_paths) > 0
                return history, cleared_msg, gr.update(value=file_paths or [], visible=has_files)

            def on_clear():
                return [], "", gr.update(value=[], visible=False)

            send_btn.click(
                fn=on_send,
                inputs=[msg_input, chatbot],
                outputs=[chatbot, msg_input, output_files]
            )
            msg_input.submit(
                fn=on_send,
                inputs=[msg_input, chatbot],
                outputs=[chatbot, msg_input, output_files]
            )
            clear_chat_btn.click(
                fn=on_clear,
                outputs=[chatbot, msg_input, output_files]
            )

        # Боковая панель
        with gr.Column(scale=1):
            gr.Markdown("## Настройки")

            # Выбор персоны
            gr.Markdown("### Персона")
            personas_list = bot.available_personas()
            current_persona = bot.persona_name

            persona_dropdown = gr.Dropdown(
                choices=personas_list,
                value=current_persona,
                label="Выберите персону"
            )
            persona_status = gr.Textbox(label="Статус", interactive=False)
            change_persona_btn = gr.Button("Сменить персону")
            change_persona_btn.click(
                fn=bot.change_persona,
                inputs=persona_dropdown,
                outputs=persona_status
            )

            # Информация о персоне
            persona_info = gr.Markdown()
            persona_dropdown.change(
                fn=bot.get_persona_info,
                inputs=persona_dropdown,
                outputs=persona_info
            )

            gr.Markdown("---")

            # Веб-поиск
            gr.Markdown("### Веб-поиск")
            web_status = gr.Textbox(
                label="Статус",
                value=bot.get_web_search_status(),
                interactive=False
            )
            web_toggle_btn = gr.Button("Вкл / Выкл веб-поиск")
            web_toggle_btn.click(
                fn=bot.toggle_web_search,
                outputs=web_status
            )

            gr.Markdown("---")

            # Файлы
            gr.Markdown("### Файлы")
            file_upload = gr.File(
                label="Загрузить файл",
                file_types=[".txt", ".md", ".py", ".js", ".json", ".csv", ".html",
                           ".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg"]
            )
            file_status = gr.Textbox(label="Результат загрузки", interactive=False)
            file_upload.change(
                fn=bot.upload_file,
                inputs=file_upload,
                outputs=file_status
            )

            files_list = gr.Textbox(
                label="Загруженные файлы",
                value=bot.get_loaded_files(),
                interactive=False
            )
            refresh_files_btn = gr.Button("Обновить список")
            refresh_files_btn.click(fn=bot.get_loaded_files, outputs=files_list)

            reset_files_btn = gr.Button("Очистить файлы", variant="stop")
            reset_files_btn.click(
                fn=bot.reset_files,
                outputs=file_status
            )

            gr.Markdown("---")

            # Память
            gr.Markdown("### Память")
            stats_text = gr.Textbox(
                label="Статистика",
                value=bot.get_memory_stats(),
                interactive=False
            )
            refresh_btn = gr.Button("Обновить статистику")
            refresh_btn.click(fn=bot.get_memory_stats, outputs=stats_text)

            clear_btn = gr.Button("Очистить память", variant="stop")
            clear_output = gr.Textbox(label="Результат")
            clear_btn.click(fn=bot.clear_memory, outputs=[clear_output, stats_text])


def main():
    demo.launch(server_name="0.0.0.0")


if __name__ == "__main__":
    main()
