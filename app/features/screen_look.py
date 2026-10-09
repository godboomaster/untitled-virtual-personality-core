"""«Посмотри на экран»: снимок экрана компьютера → локальная vision-модель.

Снимок смотрит только локальная модель (Ollama) и никуда не отправляется —
решение пользователя: экран и кадры камеры только локально. Дальше обычная
модель персоны отвечает по текстовому описанию снимка (его отдавать можно —
тоже решение пользователя), как с картинками из чата.

Экран — компьютера, на котором работает ядро: просьба с телефона или из
Telegram показывает экран ноутбука. Смотреть просит только владелец: веб и
приложение — владелец всегда, в Telegram вызывающий проверяет сам.

macOS пускает к экрану только приложение с разрешением «Запись экрана» —
то, из которого запущено ядро (Терминал, iTerm, панель). Без разрешения
снимок молча выходит одними обоями, поэтому сначала спрашиваем систему.
"""

import ctypes
import io
import logging
import re
import sys
from typing import Optional

logger = logging.getLogger(__name__)

# Длинная сторона снимка: Retina-экран (3000+ px) модель всё равно ужмёт,
# а мелкий текст при 1600 ещё читается
MAX_SIDE = 1600

_CG_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"

NO_PERMISSION = (
    "Нет доступа к экрану: macOS не разрешила запись экрана приложению, из "
    "которого запущено ядро. Откройте «Системные настройки → Конфиденциальность "
    "и безопасность → Запись экрана и системного звука», включите это "
    "приложение (Терминал, iTerm или панель Virtual Persona) и перезапустите его."
)
NO_LOCAL_MODEL = (
    "Смотреть на экран и в камеру может только локальная модель, а Ollama сейчас "
    "недоступна. Запустите Ollama и повторите."
)
LOCAL_FAILED = "Локальная модель не смогла разобрать снимок — попробуйте ещё раз."

# Для модели персоны: что за описание у неё в сообщении
SCREEN_NOTE = (
    "The user asked you to look at their computer screen. A screenshot was taken; "
    "only a local vision model saw it. Its description:\n"
)
CAMERA_NOTE = (
    "The user took a photo with their camera. Only a local vision model saw it. "
    "Its description:\n"
)


class ScreenLookError(Exception):
    """Посмотреть не вышло; текст — для пользователя."""


# Просьба посмотреть на экран — повелительное или «можешь посмотреть», и
# вопрос «что у меня на экране». Экран телефона ядро не видит — не наше
_VERBS = (r"посмотри(?:те)?|глянь(?:те)?|взгляни(?:те)?|погляди(?:те)?|загляни(?:те)?|"
          r"зацени(?:те)?|оцени(?:те)?|прочитай(?:те)?|прочти(?:те)?|проверь(?:те)?|"
          r"посмотреть|глянуть|взглянуть|заглянуть")
_SCREEN_WORD = r"(?:экран\w*|монитор\w*|рабоч\w+\s+стол\w*)"
_RU = re.compile(
    rf"(?:\b(?:{_VERBS})\b[\s,]+(?:\S+[\s,]+){{0,3}}?(?:на|в)\s+(?:мо[йёе]\w*\s+|это\w*\s+)?{_SCREEN_WORD}"
    rf"|\bчто\s+(?:\S+\s+){{0,4}}?на\s+(?:мо[ёе]м\s+|это\w*\s+)?(?:экране|мониторе|рабочем\s+столе)"
    rf"|\b(?:сделай|сними)(?:те)?\s+(?:скрин\w*|снимок\s+экрана))",
    re.IGNORECASE,
)
_EN = re.compile(
    r"\b(?:(?:look|check|glance|peek)\s+(?:at\s+)?|see\s+|read\s+)(?:my|the|this)\s+(?:screen|monitor|desktop)\b"
    r"|\bwhat(?:['’]s|\s+is)\s+(?:\S+\s+){0,2}?on\s+(?:my|the|this)\s+(?:screen|monitor|desktop)\b"
    r"|\btake\s+a\s+screenshot\b",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(?:экран\w*|screen)\s+(?:\S+\s+)?(?:телефон\w*|смартфон\w*|мобильн\w+|phone)",
                    re.IGNORECASE)


def is_screen_request(text: str) -> bool:
    """Пользователь просит посмотреть на экран компьютера."""
    if not text:
        return False
    t = text.strip()
    if _PHONE.search(t):
        return False
    return bool(_RU.search(t) or _EN.search(t))


def _core_graphics():
    if sys.platform != "darwin":
        return None
    try:
        return ctypes.cdll.LoadLibrary(_CG_PATH)
    except OSError:
        return None


def screen_permission() -> Optional[bool]:
    """Разрешение на запись экрана (macOS); None — не macOS или не узнать."""
    cg = _core_graphics()
    if cg is None:
        return None
    try:
        fn = cg.CGPreflightScreenCaptureAccess
        fn.restype = ctypes.c_bool
        return bool(fn())
    except AttributeError:
        return None


def _request_permission() -> None:
    # Системный запрос (только первый раз) — дальше выключатель в настройках
    cg = _core_graphics()
    if cg is None:
        return
    try:
        fn = cg.CGRequestScreenCaptureAccess
        fn.restype = ctypes.c_bool
        fn()
    except AttributeError:
        pass


def capture_screen() -> bytes:
    """Снимок основного экрана → JPEG, длинная сторона не больше MAX_SIDE."""
    if screen_permission() is False:
        _request_permission()
        raise ScreenLookError(NO_PERMISSION)
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
    except Exception as e:
        logger.warning(f"[Screen] Снимок экрана не удался: {e}")
        raise ScreenLookError(f"Не получилось сделать снимок экрана: {e}") from e
    return to_jpeg(img)


def to_jpeg(img, max_side: int = MAX_SIDE) -> bytes:
    """PIL-картинка → JPEG не больше max_side по длинной стороне."""
    img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def describe_locally(local_router, image_bytes: bytes, question: str,
                     lang: Optional[str], subject: str) -> str:
    """Описание снимка только локальной моделью; нет её — ScreenLookError."""
    if local_router is None:
        raise ScreenLookError(NO_LOCAL_MODEL)
    text = local_router.ocr_image(image_bytes, question, lang, subject=subject)
    if text:
        return text
    raise ScreenLookError(LOCAL_FAILED if getattr(local_router, "_available", False) else NO_LOCAL_MODEL)


def screen_input(local_router, question: str, lang: Optional[str]) -> str:
    """Снимок экрана → текст хода для модели персоны: просьба + описание."""
    shot = capture_screen()
    desc = describe_locally(local_router, shot, question, lang, subject="screen")
    logger.info(f"[Screen] Снимок {len(shot) // 1024} КБ описан локальной моделью ({len(desc)} симв.)")
    return (f"{question}\n\n" if question else "") + SCREEN_NOTE + desc
