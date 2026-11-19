"""Explicit head commands from human text; no LLM-generated motor actions."""
from dataclasses import dataclass
import re
import math
import time

from app.core import devices

ACTION = "head.turn"
COMMAND_LIFETIME_SEC = 4.0
RESULT_TIMEOUT_SEC = 5.0


@dataclass(frozen=True)
class Request:
    direction: str
    device: dict | None = None
    problem: str | None = None


def _names(names):
    alternatives = []
    for name in names:
        name = " ".join(str(name).casefold().replace("ё", "е").split())
        if not name:
            continue
        escaped = re.escape(name).replace(r"\ ", r"\s+")
        if name in {"робот", "robot"}:
            escaped += r"(?:а|у|'s)?"
        alternatives.append(escaped)
    return "(?:" + "|".join(sorted(alternatives, key=len, reverse=True)) + ")" if alternatives else None


def parse_request(text: str, address_names=(), device_names=()) -> tuple[str, bool] | None:
    """A complete imperative only. Quotes, negations and compound requests fail closed."""
    if not isinstance(text, str) or len(text) > 250 or "\n" in text:
        return None
    text = " ".join(text.casefold().replace("ё", "е").split()).strip()
    addresses = _names(address_names)
    if addresses:
        text = re.sub(rf"^{addresses}\s*[,!:]\s*", "", text, count=1)
    text = re.sub(r"^(?:пожалуйста|please)(?:\s*[,!]\s*|\s+)", "", text)
    text = re.sub(r"\s*,?\s+(?:пожалуйста|please)[.!?]*$", "", text)
    text = text.rstrip(".!?").strip()
    addressed = False
    names = _names(device_names)
    if names:
        text, count = re.subn(rf"^{names}(?:\s*[,!:]\s*|\s+)", "", text, count=1)
        addressed |= bool(count)
        text, count = re.subn(rf"\bголову\s+{names}(?=\s)", "голову", text, count=1)
        addressed |= bool(count)
        text, count = re.subn(rf"\b{names}\s+голову\b", "голову", text, count=1)
        addressed |= bool(count)
        text, count = re.subn(rf"\b{names}\s+head\b", "head", text, count=1)
        addressed |= bool(count)
    match = re.fullmatch(
        r"(?:поверни(?:те)?|верни(?:те)?|поставь(?:те)?)\s+(?:свою\s+)?голову\s+"
        r"(вправо|направо|влево|налево|в\s+центр|прямо)", text)
    if match:
        direction = {"вправо": "right", "направо": "right", "влево": "left",
                     "налево": "left", "в центр": "center", "прямо": "center"}[match[1]]
        return direction, addressed
    match = re.fullmatch(
        r"(?:turn|return|move)\s+(?:(?:the|your)\s+)?head\s+(?:to\s+(?:the\s+)?)?"
        r"(left|right|center|centre)", text)
    return (("center" if match[1] == "centre" else match[1]), addressed) if match else None


def resolve_request(text: str, address_names, action: str, parser, persona_name=None) -> Request | None:
    registered = devices.list_devices()
    matches = []
    for dev in registered:
        parsed = parser(text, address_names, dev.get("names", []))
        if parsed and parsed[1]:
            matches.append((dev, parsed[0]))
    parsed = parser(text, address_names)
    if not matches and not parsed:
        return None
    from app.core import device_bodies
    try:
        mapping = device_bodies.bindings() if persona_name is not None else {}
    except device_bodies.BindingError:
        return Request(matches[0][1] if matches else parsed[0], problem='body-binding-invalid')
    bound = mapping.get(persona_name)
    def checked(value, dev):
        return Request(value, dev, device_bodies.access_problem(persona_name, dev['id'], mapping))
    if matches:
        own = [match for match in matches if match[0]['id'] == bound]
        if own:
            matches = own
        if len(matches) != 1:
            return Request(matches[0][1], problem="ambiguous")
        return checked(matches[0][1], matches[0][0])
    if bound:
        dev = next((dev for dev in registered if dev['id'] == bound), None)
        return checked(parsed[0], dev) if dev else Request(parsed[0], problem='body-offline')
    capable = [dev for dev in registered if action in dev.get("capabilities", [])]
    if len(capable) != 1:
        return Request(parsed[0], problem="ambiguous" if capable else "unconfigured")
    return checked(parsed[0], capable[0])


def find_request(text: str, address_names=(), persona_name=None) -> Request | None:
    return resolve_request(text, address_names, ACTION, parse_request, persona_name)


def denied(lang=None):
    return ("Only the owner or an allowed user can control the robot's head."
            if str(lang).startswith("en") else
            "Управление головой робота доступно владельцу или разрешённому пользователю.")


def confirmed_position(data, direction):
    if not isinstance(data, dict) or data.get('direction') != direction:
        return False
    if data.get('pwm_off') is True:
        return data.get('holding', False) is False
    lease = data.get('watchdog_sec')
    return (data.get('pwm_off') is False and data.get('holding') is True
            and type(data.get('hold_protocol')) is int and data['hold_protocol'] == 1
            and type(lease) in (int, float) and math.isfinite(lease) and 5 <= lease <= 60)


def execute(request: Request, lang=None) -> str:
    from app.core import device_bodies
    problem = device_bodies.problem_text(request.problem, lang)
    if problem:
        return problem
    english = str(lang).startswith("en")
    if request.problem == "ambiguous":
        return "Specify which robot should turn its head." if english else "Уточни, какому роботу повернуть голову."
    if request.device is None:
        return ("No device with head control is configured yet." if english else
                "Устройства с управлением головой пока не подключены.")
    dev = request.device
    name = dev["name"]
    if not dev.get("online"):
        return (f'«{name}» is offline. The turn command was not sent.' if english else
                f'«{name}» сейчас не на связи. Команду поворота не отправлял.')
    try:
        result = devices.command(dev["id"], ACTION,
                                 {"direction": request.direction,
                                  "expires_at": time.time() + COMMAND_LIFETIME_SEC},
                                 timeout=RESULT_TIMEOUT_SEC)
    except devices.DeviceError as error:
        if error.code == 'asleep':
            return 'The body is asleep. Say “activate body” first.' if english else 'Тело спит. Сначала скажи «включи тело».'
        if error.code in {"offline", "unknown"}:
            return (f'«{name}» is offline. The turn command was not sent.' if english else
                    f'«{name}» сейчас не на связи. Команду поворота не отправлял.')
        if error.code == "unsupported":
            return (f'Head control is not enabled on «{name}» yet.' if english else
                    f'На «{name}» управление головой ещё не включено.')
        if error.code == "timeout":
            return ("The robot did not confirm the turn in time. It may have moved; the command will not be retried automatically."
                    if english else "Робот не подтвердил поворот вовремя. Он мог повернуться; автоматически команду не повторяю.")
        return ("The robot could not confirm the head command." if english else
                "Робот не смог подтвердить выполнение команды голове.")
    data = result.get("data")
    if not confirmed_position(data, request.direction):
        return ("The robot did not confirm completion of the head command." if english else
                "Робот не подтвердил завершение команды голове.")
    direction = {"left": "влево", "right": "вправо", "center": "в центр"}[request.direction]
    if data.get('holding') is True:
        if data.get('idle_enabled') is True:
            return (f'«{name}» confirmed the head command: {request.direction}. Automatic idle head movements are enabled.'
                    if english else f'«{name}» подтвердил команду голове: {direction}. Фоновые движения головы включены.')
        return (f'«{name}» confirmed the head command: {request.direction}. The position is held until another command or sleep.'
                if english else f'«{name}» подтвердил команду голове: {direction}. Положение удерживается до следующей команды или «усни».')
    return (f'«{name}» confirmed the head command: {request.direction}.' if english else
            f'«{name}» подтвердил выполнение команды голове: {direction}.')
