"""«Что видит робот?»: кадр с камеры устройства ядра (app/core/devices) →
локальная vision-модель → ход персоны.

Как «посмотри на экран» (screen_look): кадр видит только локальная модель,
модели персоны достаётся текстовое описание, а сам кадр уходит в ответ
пользователю (веб и приложение показывают его в пузыре ответа). Просьбу
узнаём по имени устройства из его hello («робот», «robot») рядом с просьбой
посмотреть: «что видит робот», «посмотри через робота», «робот, что вокруг?».
"""

import logging
import re
from typing import Callable, Optional

from app.core import devices
from app.features import screen_look

logger = logging.getLogger(__name__)

SNAPSHOT = "camera.snapshot"
# Устройство снимает ~2 с (камере нужно подстроить яркость) и шлёт ~250 КБ
SNAPSHOT_TIMEOUT_SEC = 20.0

OFFLINE = ("Устройство «{name}» сейчас не на связи — камера недоступна. Проверьте, "
           "что оно включено и его программа запущена.")
TIMEOUT = "Устройство «{name}» не прислало снимок вовремя — попробуйте ещё раз."
FAILED = "Устройство «{name}» не смогло сделать снимок: {detail}"

# Для модели персоны: что за описание у неё в сообщении
DEVICE_NOTE = (
    "The user asked you to look through the camera of their device \"{name}\". "
    "It took a photo just now; only a local vision model saw it. Its description:\n"
)

_LOOK_RU = (r"посмотри(?:те)?|смотри(?:те)?|глянь(?:те)?|взгляни(?:те)?|погляди(?:те)?|осмотри(?:сь|тесь)|"
            r"огляди(?:сь|тесь)|оглянись|покажи(?:те)?|посмотреть|глянуть|взглянуть|показать")
_PHOTO_RU = r"сфоткай(?:те)?|сфотографируй(?:те)?|сделай(?:те)?\s+(?:фото\w*|снимок|кадр)"
_SHOOT_RU = rf"сними(?:те)?|{_PHOTO_RU}"
_NEG = re.compile(rf"\bне\s+(?:{_LOOK_RU}|{_SHOOT_RU})\b|\b(?:don['’]t|do\s+not|never)\s+(?:look|take|snap)\b",
                  re.IGNORECASE)


def _name_group(names: list[str]) -> str:
    # Имя с окончаниями: «робот» → «робота», «роботом»; «robot» → «robot's»
    alts = []
    for n in names:
        words = [re.escape(w) for w in n.lower().split() if w]
        if words:
            alts.append(r"\s+".join(w + r"\w{0,3}" for w in words))
    alts.sort(key=len, reverse=True)
    return rf"(?<!\w)(?:{'|'.join(alts)})(?!\w)"


def _patterns(n: str) -> list[str]:
    art = r"(?:(?:the|my|your)\s+)?"
    return [
        # что видит робот / что сейчас видно у робота / что показывает камера робота
        rf"\bчто\s+(?:\S+\s+){{0,2}}?(?:видит|видно|снимает|показывает)\s+(?:\S+\s+)?{n}",
        # что робот видит
        rf"\bчто\s+{n}\s+(?:\S+\s+)?(?:видит|видно|снимает)",
        # что вокруг робота / кто перед роботом
        rf"\b(?:что|кто)\s+(?:\S+\s+)?(?:вокруг|перед|рядом\s+с)\s+{n}",
        # что на камере робота / покажи камеру робота
        rf"\b(?:что|кто)\s+(?:\S+\s+)?(?:на|в)\s+камер\w*\s+{n}",
        rf"\b(?:{_LOOK_RU})\b[\s,]+(?:\S+\s+)?камер\w*\s+{n}",
        # посмотри через робота / глянь камерой робота / посмотри, что в комнате, через робота
        rf"\b(?:{_LOOK_RU})\b[\s,]+(?:\S+[\s,]+){{0,3}}?(?:через|камерой|глазами|с\s+помощью)\s+{n}",
        # сфоткай роботом / сделай фото роботом / сними на робота («сними робота
        # с полки» — не снимок)
        rf"\b(?:{_PHOTO_RU})\s+(?:\S+\s+){{0,2}}?(?:(?:на|через)\s+)?{n}",
        rf"\bсними(?:те)?\s+(?:\S+\s+){{0,2}}?(?:на|через)\s+{n}",
        # робот, посмотри вокруг / робот, что ты видишь?
        rf"^\W*{n}[\s,!:.]+(?:\S+\s+)?(?:{_LOOK_RU}|{_SHOOT_RU}|что\s+(?:ты\s+)?видишь|что\s+вокруг|что\s+перед\s+тобой)",
        # what does the robot see / what's around the robot
        rf"\bwhat\s+(?:does|do|can|did)\s+{art}{n}\s+see\b",
        rf"\bwhat(?:['’]s|\s+is)\s+(?:\S+\s+)?(?:around|in\s+front\s+of|near)\s+{art}{n}",
        # look through the robot / take a photo with the robot
        rf"\b(?:look|peek|check|glance)\s+(?:\S+\s+){{0,3}}?(?:through|with|via|using)\s+{art}{n}",
        rf"\b(?:take|snap|grab)\s+a\s+(?:photo|picture|pic|snapshot|shot)\s+(?:\S+\s+){{0,2}}?"
        rf"(?:with|from|using|through)\s+{art}{n}",
        # robot, look around / robot, what do you see?
        rf"^\W*{n}[\s,!:.]+(?:look|what\s+do\s+you\s+see|what(?:['’]s|\s+is)\s+around|take\s+a\s+(?:photo|picture))",
        rf"\b(?:what(?:['’]s|\s+is)\s+on|show\s+me|look\s+at)\s+{art}{n}['’]s\s+camera",
    ]


def is_device_request(text: str, names: list[str]) -> bool:
    """Просьба посмотреть камерой устройства с одним из имён names."""
    if not text or not names or _NEG.search(text):
        return False
    n = _name_group(names)
    return any(re.search(p, text, re.IGNORECASE) for p in _patterns(n))


def find_request(text: str) -> Optional[dict]:
    """Устройство с камерой, к которому обращена реплика, — или None."""
    for dev in devices.with_capability(SNAPSHOT):
        if is_device_request(text, dev["names"]):
            return dev
    return None


def snapshot_input(local_router, device: dict, question: str, lang: Optional[str],
                   on_shot: Optional[Callable[[bytes], None]] = None) -> tuple[str, bytes]:
    """Кадр устройства → (текст хода для модели персоны, JPEG для ответа).
    on_shot — кадр пришёл, описания ещё нет: потоковый чат сразу показывает
    его пользователю (снимок сделан), пока смотрит модель.
    Не вышло — ScreenLookError с текстом для пользователя."""
    name = device["name"]
    try:
        res = devices.command(device["id"], SNAPSHOT, timeout=SNAPSHOT_TIMEOUT_SEC)
    except devices.DeviceError as e:
        if e.code == "timeout":
            raise screen_look.ScreenLookError(TIMEOUT.format(name=name))
        if e.code == "failed":
            raise screen_look.ScreenLookError(FAILED.format(name=name, detail=e.detail or "без подробностей"))
        raise screen_look.ScreenLookError(OFFLINE.format(name=name))
    shot = res.get("image")
    if not shot:
        raise screen_look.ScreenLookError(FAILED.format(name=name, detail="пустой снимок"))
    if on_shot is not None:
        try:
            on_shot(shot)
        except Exception as e:
            logger.warning(f"[DeviceLook] Кадр «{name}» не отдан заранее: {e}")
    desc = screen_look.describe_locally(local_router, shot, question, lang, subject="image")
    logger.info(f"[DeviceLook] Кадр «{name}» {len(shot) // 1024} КБ описан локальной моделью ({len(desc)} симв.)")
    return (f"{question}\n\n" if question else "") + DEVICE_NOTE.format(name=name) + desc, shot
