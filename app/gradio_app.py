"""
Gradio-интерфейс для Virtual Personality.
Запускается отдельно от Telegram-ботов:

    python -m app.gradio_app

Использует свой контекст "gradio" — отдельная база данных от Telegram.
"""

import gradio as gr
import yaml
from pathlib import Path

from app.core.persona import PersonaLayer
from app.core.memory import MemoryManager
from app.core.router import ModelRouter
from app.core.config import Config
from app.core.file_vector_db import FileVectorDB


class GradioBot:
    """Обёртка для Gradio — один экземпляр с переключаемой персоной."""

    def __init__(self):
        self.persona_name = "arrodes"
        self.persona = PersonaLayer(persona_name=self.persona_name)
        self.router = ModelRouter()
        self.memory = MemoryManager(
            stm_size=Config.STM_SIZE,
            enable_ltm_extraction=Config.LTM_EXTRACTION_ENABLED,
            ltm_model_provider=Config.LTM_MODEL_PROVIDER,
            load_stm_from_db=True,
            context="gradio",
            main_router=self.router
        )
        self.file_db = FileVectorDB(context="gradio")

    def chat(self, message: str, history: list) -> str:
        return self.process_message(message, user_id="gradio")

    def process_message(self, user_input: str, user_id: str = "gradio") -> str:
        # 1. Добавляем в память
        self.memory.add_message("user", user_input, user_id)

        # 2. Контекст из памяти
        stm_messages, ltm_facts = self.memory.get_context(user_id, ltm_query=user_input)

        # 3. Файлы
        file_context = None
        if self.file_db:
            file_chunks = self.file_db.search(user_id=user_id, query=user_input, limit=5)
            if file_chunks:
                file_context = "Контекст из загруженных файлов:\n" + "\n---\n".join(file_chunks)

        # 4. Общий контекст
        context_parts = []
        if ltm_facts:
            context_parts.append("\n".join(ltm_facts))
        if file_context:
            context_parts.append(file_context)
        memory_text = "\n\n".join(context_parts) if context_parts else None

        # 5. Persona + LLM
        has_files = file_context is not None
        messages = self.persona.prepare_messages(user_input, memory_text, history=stm_messages,
                                                  has_files=has_files)
        settings = self.persona.get_settings()
        print(f"[Gradio] [Response] -> {self.router.get_provider_model_info()}")
        answer = self.router.get_response(messages, **settings)

        # 6. Сохраняем ответ
        self.memory.add_message("assistant", answer, user_id)
        return answer

    def get_memory_stats(self) -> str:
        stats = self.memory.get_stats(user_id="gradio")
        return (
            f"Краткосрочная: {stats['stm_count']}/{stats['stm_max']} сообщений\n"
            f"Долгосрочная: {stats['ltm_count']} фактов"
        )

    def clear_memory(self) -> tuple:
        self.memory.clear_stm()
        self.memory.clear_ltm(user_id="gradio")
        return "Память очищена", self.get_memory_stats()

    def change_persona(self, persona_name: str) -> str:
        success = self.persona.change_persona(persona_name)
        if success:
            self.persona_name = persona_name
            return f"Персона изменена на: {persona_name}"
        return f"Ошибка: персона '{persona_name}' не найдена"

    def get_persona_info(self, persona_name: str) -> str:
        persona_path = Path(__file__).parent / "personas" / f"{persona_name}.yaml"
        if not persona_path.exists():
            return "Персона не найдена"
        with open(persona_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        description = data.get("description", "Описание отсутствует")
        return f"**{persona_name}**\n\n{description}"

    def available_personas(self) -> list:
        return self.persona.available_personas()


# ─── Создаём бота ────────────────────────────────────
bot = GradioBot()

# ─── Gradio UI ───────────────────────────────────────
with gr.Blocks(title="Виртуальная Личность") as demo:
    gr.Markdown("# Виртуальная Личность")
    gr.Markdown("Чат с двухуровневой памятью (краткосрочная + долгосрочная)")

    with gr.Row():
        # Чат
        with gr.Column(scale=3):
            chatbot = gr.ChatInterface(
                fn=bot.chat,
                title="",
                description="",
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
    demo.launch()


if __name__ == "__main__":
    main()
