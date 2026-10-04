"""
Rich Message Formatter — конвертация Markdown в Rich HTML/Markdown для Telegram Bot API 10.1+

Использование после обновления python-telegram-bot:
    from app.core.rich_message_formatter import RichMessageFormatter
    
    formatter = RichMessageFormatter()
    rich_html = formatter.markdown_to_rich_html(text)
    # Отправка через sendRichMessage (когда будет доступно в PTB)
    await bot.send_rich_message(chat_id, rich_message=InputRichMessage(html=rich_html))

Пока PTB не поддерживает Rich Messages — используем новые HTML-теги через parse_mode="HTML":
    - <tg-spoiler> — спойлеры
    - <u>, <ins> — подчеркивание
    - <sub>, <sup> — индексы
    - <mark> — выделение
    - <blockquote expandable> — раскрываемые цитаты
"""

import re
from typing import List, Optional, Tuple

_CODE_BLOCK_RE = re.compile(r'```(\w*)\n?(.*?)```', re.DOTALL)
_INLINE_CODE_RE = re.compile(r'`([^`]+)`')


def _escape_html(text: str) -> str:
    # Экранирование HTML-сущностей — единственное определение на модуль.
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _stash_code(text: str) -> Tuple[str, List[str], List[str]]:
    """Вырезать код-блоки и инлайн-код в плейсхолдеры, экранируя содержимое.

    Код экранируется тем же ``_escape_html``, что и остальной текст — один
    путь на модуль. Без этого содержимое ``<pre>`` ушло бы в Telegram как
    есть: любой ``<`` в примере кода сломал бы разбор HTML (сообщение не
    отправится), а ``<b>`` или ``<a href=...>`` из текста модели
    исполнились бы как разметка.
    """
    code_blocks: List[str] = []
    inline_codes: List[str] = []

    def _save_block(m):
        lang = m.group(1)
        code = _escape_html(m.group(2))
        placeholder = f'\x00BLOCK{len(code_blocks)}\x00'
        if lang:
            code_blocks.append(f'<pre><code class="language-{lang}">{code}</code></pre>')
        else:
            code_blocks.append(f'<pre>{code}</pre>')
        return placeholder

    def _save_inline(m):
        placeholder = f'\x00INLINE{len(inline_codes)}\x00'
        inline_codes.append(f'<code>{_escape_html(m.group(1))}</code>')
        return placeholder

    text = _CODE_BLOCK_RE.sub(_save_block, text)
    text = _INLINE_CODE_RE.sub(_save_inline, text)
    return text, code_blocks, inline_codes


def _restore_code(text: str, code_blocks: List[str], inline_codes: List[str]) -> str:
    # Вернуть на место плейсхолдеры кода (после всей остальной разметки).
    for i, code in enumerate(inline_codes):
        text = text.replace(f'\x00INLINE{i}\x00', code)
    for i, block in enumerate(code_blocks):
        text = text.replace(f'\x00BLOCK{i}\x00', block)
    return text


class RichMessageFormatter:
    # Конвертирует Markdown в Rich HTML/Markdown по спецификации Telegram Bot API 10.1.

    # ─── Markdown → Rich HTML ──────────────────────────────────────

    @staticmethod
    def markdown_to_rich_html(text: str) -> str:
        # Конвертирует Markdown в Rich HTML для Telegram, включая новые теги Bot API 10.1.
        if not text:
            return text

        # Прячем код в плейсхолдеры (с экранированием внутри — общий путь)
        text, code_blocks, inline_codes = _stash_code(text)

        # Экранируем HTML-сущности в оставшемся тексте
        text = _escape_html(text)

        # Жирный: **text** → <b>text</b>
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)

        # Подчеркнутый: __text__ → <u>text</u> (Rich HTML). Раньше одиночного
        # «_»: иначе __x__ становился <i>_x_</i>. Порядок как в вебе
        # (MessageText.tsx): ** раньше *, __ раньше _
        text = re.sub(r'__(.+?)__', r'<u>\1</u>', text)

        # Курсив: *text* или _text_ → <i>text</i>; «_» внутри слова
        # (snake_case) — не разметка
        text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
        text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', text)

        # Зачеркнутый: ~~text~~ → <s>text</s>
        text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

        # Спойлер: ||text|| → <tg-spoiler>text</tg-spoiler>
        text = re.sub(r'\|\|(.+?)\|\|', r'<tg-spoiler>\1</tg-spoiler>', text)

        # Выделенный: ==text== → <mark>text</mark>
        text = re.sub(r'==(.+?)==', r'<mark>\1</mark>', text)

        # Заголовки: # H1 → <h1>H1</h1>, ## H2 → <h2>H2</h2>, etc.
        for i in range(6, 0, -1):
            text = re.sub(rf'^#{{{i}}}\s+(.+)$', rf'<h{i}>\1</h{i}>', text, flags=re.MULTILINE)

        # Горизонтальная линия: --- → <hr/>
        text = re.sub(r'^---+$', '<hr/>', text, flags=re.MULTILINE)

        # Цитаты: > text → <blockquote>text</blockquote>
        # Многострочные цитаты
        def _process_blockquotes(t):
            lines = t.split('\n')
            result = []
            in_quote = False
            quote_lines = []

            for line in lines:
                if line.startswith('&gt;') or line.startswith('>'):
                    # Извлекаем содержимое после >
                    content = line[1:].strip() if line.startswith('>') else line[4:].strip()
                    if not in_quote:
                        in_quote = True
                        quote_lines = [content]
                    else:
                        quote_lines.append(content)
                else:
                    if in_quote:
                        quote_text = '<br>'.join(quote_lines)
                        result.append(f'<blockquote>{quote_text}</blockquote>')
                        in_quote = False
                        quote_lines = []
                    result.append(line)

            if in_quote:
                quote_text = '<br>'.join(quote_lines)
                result.append(f'<blockquote>{quote_text}</blockquote>')

            return '\n'.join(result)

        text = _process_blockquotes(text)

        # Списки
        # Неупорядоченные: - item, * item, + item → <ul><li>item</li></ul>
        # Упорядоченные: 1. item → <ol><li>item</li></ol>
        # Task: - [ ] item, - [x] item → <ul><li><input type="checkbox">item</li></ul>
        text = _process_lists(text)

        # Ссылки: [text](url) → <a href="url">text</a>
        text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', text)

        # Сноски: [^id] — пока не поддерживаем в HTML, убираем
        text = re.sub(r'\[\^[^\]]+\]', '', text)

        # Возвращаем код на место
        text = _restore_code(text, code_blocks, inline_codes)

        return text.strip()

    @staticmethod
    def markdown_to_rich_markdown(text: str) -> str:
        # Возвращает текст как есть — Telegram сам парсит Markdown, нормализация не нужна.
        return text.strip()

    # ─── Поддержка новых тегов через parse_mode="HTML" (текущий API) ─

    @staticmethod
    def to_current_html(text: str) -> str:
        """
        Конвертирует Markdown в HTML, используя только теги,
        поддерживаемые текущим parse_mode="HTML" (до Bot API 10.1).
        
        Новые теги которые уже работают:
        - <tg-spoiler> — спойлер
        - <u>, <ins> — подчеркивание
        - <sub>, <sup> — индексы
        - <mark> — выделение
        - <blockquote expandable> — раскрываемая цитата
        """
        if not text:
            return text

        # Тот же общий путь, что и в markdown_to_rich_html
        text, code_blocks, inline_codes = _stash_code(text)

        text = _escape_html(text)

        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        # __ раньше одиночного _ (см. markdown_to_rich_html)
        text = re.sub(r'__(.+?)__', r'<u>\1</u>', text)
        text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
        text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', text)
        text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
        text = re.sub(r'\|\|(.+?)\|\|', r'<tg-spoiler>\1</tg-spoiler>', text)
        text = re.sub(r'==(.+?)==', r'<mark>\1</mark>', text)

        # Заголовки → просто жирный текст (текущий HTML не поддерживает h1-h6)
        text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

        # Горизонтальная линия
        text = re.sub(r'^---+$', '───────────', text, flags=re.MULTILINE)

        # Цитаты
        text = re.sub(r'^&gt;\s?(.*)$', r'<blockquote>\1</blockquote>', text, flags=re.MULTILINE)
        text = re.sub(r'</blockquote>\n<blockquote>', '\n', text)

        # Ссылки
        text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', text)

        # Возвращаем код на место
        text = _restore_code(text, code_blocks, inline_codes)

        return text.strip()


def _process_lists(text: str) -> str:
    # Обрабатывает markdown-списки и конвертирует в HTML.
    lines = text.split('\n')
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Неупорядоченный список
        if re.match(r'^[-*+]\s', line):
            items = []
            while i < len(lines) and re.match(r'^[-*+]\s', lines[i]):
                content = re.sub(r'^[-*+]\s', '', lines[i])
                # Task list
                task_match = re.match(r'^\[([ x])\]\s*(.*)', content)
                if task_match:
                    checked = 'checked' if task_match.group(1) == 'x' else ''
                    content = f'<input type="checkbox" {checked}>{task_match.group(2)}'
                items.append(f'<li>{content}</li>')
                i += 1
            result.append(f'<ul>{"".join(items)}</ul>')
            continue

        # Упорядоченный список
        ordered_match = re.match(r'^(\d+)\.\s', line)
        if ordered_match:
            start = ordered_match.group(1)
            items = []
            while i < len(lines) and re.match(r'^\d+\.\s', lines[i]):
                content = re.sub(r'^\d+\.\s', '', lines[i])
                items.append(f'<li>{content}</li>')
                i += 1
            result.append(f'<ol start="{start}">{"".join(items)}</ol>')
            continue

        result.append(line)
        i += 1

    return '\n'.join(result)
