"""Повторяемые блоки промпта для постоянного треда веб-чата.

Зачем: агент задач шлёт каждый шаг целиком — ~8 тыс. символов правил и
список элементов страницы — в ОДИН постоянный тред сайта (web_llm, канал
cc). Модель в этом треде уже видела те же правила шагом раньше; повторять их
на каждом шаге — это 15–25 тыс. символов на сообщение и лишний риск бана
аккаунта. Вызывающий код помечает такие блоки (StickyBlock), а веб-чат
(app/features/web_llm.py) заменяет блок короткой ссылкой, если ЭТОТ тред
получил его полностью недавно (не дальше window сообщений назад, в той же
вкладке, без навигации). Иначе блок уходит целиком.

API-провайдеры и локальная модель области не видят: им каждый вызов уходит
полным промптом — у них нет памяти треда.

ContextVar — по той же причине, что и dialog_scope: область доходит до
синхронного вызова в потоке через asyncio.to_thread.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class StickyBlock:
    # Точный текст блока, как он стоит в промпте
    text: str
    # Чем заменить блок, если тред его уже видел
    ref: str
    # Блок «свежий», пока с его полной отправки в тред ушло не больше window
    # сообщений (включая чужие вызовы того же канала)
    window: int
    # Длинный блок можно отправить заранее отдельным сообщением, когда
    # полный промпт не влезает в жёсткий лимит поля сайта (duck.ai — 16 тыс.)
    primable: bool = False


_STICKY: ContextVar[Tuple[StickyBlock, ...]] = ContextVar(
    "sticky_blocks", default=())


def current_sticky() -> Tuple[StickyBlock, ...]:
    return _STICKY.get()


@contextmanager
def sticky_blocks(*blocks: StickyBlock):
    """Вызовы внутри — с этими повторяемыми блоками (пустые пропускаются)."""
    blocks = tuple(b for b in blocks if b is not None and b.text)
    token = _STICKY.set(blocks)
    try:
        yield
    finally:
        _STICKY.reset(token)
