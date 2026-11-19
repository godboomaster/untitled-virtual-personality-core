"""Literal human requests to halt a registered robot's Pi, never the core host."""
from dataclasses import dataclass
import math
import re
import time

from app.core import devices
from app.features import device_head

ACTION = 'robot.poweroff'
COMMAND_LIFETIME_SEC = 5.0
RESULT_TIMEOUT_SEC = 7.0


@dataclass(frozen=True)
class Request:
    device: dict | None = None
    problem: str | None = None


def parse_request(text, address_names=(), device_names=()):
    if not isinstance(text, str) or len(text) > 250 or '\n' in text:
        return None
    text = ' '.join(text.casefold().replace('ё', 'е').split())
    named = False
    names = device_head._names(device_names)
    if names:
        text, count = re.subn(rf'\bвыключи(?:те)?\s+{names}(?=\s|[.!?,]|$)',
                              'выключи себя', text, count=1)
        named = bool(count)
    # Reuse the strict full-command grammar, including known robot/persona names.
    text, count = re.subn(r'\b(?:выключи(?:те)?\s+себя|shut\s+yourself\s+down)\b',
                          'поверни голову прямо', text, count=1)
    if not count:
        return None
    parsed = device_head.parse_request(text, address_names, device_names)
    return ('poweroff', named or parsed[1]) if parsed else None


def find_request(text, address_names=(), persona_name=None):
    result = device_head.resolve_request(text, address_names, ACTION, parse_request, persona_name)
    return Request(result.device, result.problem) if result else None


def denied(lang=None):
    return ('Only the owner or an allowed user can shut down the robot.'
            if str(lang).startswith('en') else
            'Выключение робота доступно владельцу или разрешённому пользователю.')


def execute(request, lang=None):
    from app.core import device_bodies
    problem = device_bodies.problem_text(request.problem, lang)
    if problem:
        return problem
    english = str(lang).startswith('en')
    if request.problem == 'ambiguous':
        return 'Specify which robot to shut down.' if english else 'Уточни, какого робота выключить.'
    if request.device is None:
        return ('Robot shutdown is not configured yet.' if english else
                'Команда выключения робота ещё не настроена.')
    dev = request.device
    name = dev['name']
    if not dev.get('online'):
        return (f'«{name}» is offline. No shutdown command was queued.' if english else
                f'«{name}» сейчас не на связи. Команду выключения в очередь не ставил.')
    try:
        result = devices.command(dev['id'], ACTION,
                                 {'expires_at': time.time() + COMMAND_LIFETIME_SEC},
                                 timeout=RESULT_TIMEOUT_SEC)
    except devices.DeviceError as error:
        if error.code in {'offline', 'unknown'}:
            return ('The robot is offline. No shutdown command was sent.' if english else
                    'Робот не на связи. Команду выключения не отправлял.')
        if error.code == 'unsupported':
            return ('Shutdown is not enabled on this robot.' if english else
                    'На этом роботе команда выключения ещё не включена.')
        return ('Shutdown was not confirmed. The Pi may be shutting down; I will not retry automatically.'
                if english else 'Подтверждение выключения не получено. Pi могла начать выключение; автоматически команду не повторяю.')
    data = result.get('data')
    scheduled = data.get('scheduled_for') if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get('status') != 'scheduled'
            or data.get('platform') != 'raspberry_pi_4'
            or data.get('hardware_power_cut') is not False
            or isinstance(scheduled, bool) or not isinstance(scheduled, (int, float))
            or not math.isfinite(scheduled) or not 0 < scheduled - time.time() <= 15):
        return ('The Pi did not confirm scheduling its shutdown.' if english else
                'Pi не подтвердила приём запроса на штатное выключение.')
    return (f'«{name}» accepted the Pi shutdown request. The power adapter will remain connected.'
            if english else f'«{name}» принял запрос штатного выключения Pi. Питание от адаптера останется включённым.')
