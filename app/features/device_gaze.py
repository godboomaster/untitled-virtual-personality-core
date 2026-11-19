"""A single turn-and-capture command, followed by local image description."""
import logging
import re
import time

from app.core import devices
from app.features import device_head, device_look, screen_look

logger = logging.getLogger(__name__)
ACTION = 'head.look'
COMMAND_LIFETIME_SEC = 8.0
RESULT_TIMEOUT_SEC = 12.0


def mentions_directional_look(text):
    return bool(re.search(r'\b(?:посмотри(?:те)?\s+(?:вправо|направо|влево|налево|прямо|в\s+центр)|look\s+(?:to\s+(?:the\s+)?)?(?:left|right|center|centre|straight(?:\s+ahead)?))\b',
                          text or '', flags=re.IGNORECASE))


def parse_request(text, address_names=(), device_names=()):
    if not isinstance(text, str):
        return None
    # Only substitute the literal imperative. The existing full-match parser
    # still rejects negation, quotations, reported speech and extra actions.
    text, ru = re.subn(r'\bпосмотри(?:те)?\s+(вправо|направо|влево|налево|прямо|в\s+центр)\b',
                       r'поверни голову \1', text, flags=re.IGNORECASE)
    text, en = re.subn(r'\blook\s+(?:to\s+(?:the\s+)?)?(left|right|center|centre|straight(?:\s+ahead)?)\b',
                       lambda m: 'turn head ' + ('center' if m[1].lower().startswith('straight') else m[1]),
                       text, flags=re.IGNORECASE)
    if ru + en != 1:
        return None
    parsed = device_head.parse_request(text, address_names, device_names)
    return parsed if parsed and parsed[0] in ('left', 'center', 'right') else None


def find_request(text, address_names=(), persona_name=None):
    return device_head.resolve_request(text, address_names, ACTION, parse_request, persona_name)


def caption(request):
    return f"{request.device['name']} · " + {'right': 'вправо', 'left': 'влево', 'center': 'прямо'}[request.direction]


def capture_input(local_router, request, question, lang=None, on_shot=None):
    from app.core import device_bodies
    problem = device_bodies.problem_text(request.problem, lang)
    if problem:
        raise screen_look.ScreenLookError(problem)
    english = str(lang).startswith('en')
    if request.problem == 'ambiguous':
        raise screen_look.ScreenLookError('Specify which robot should look.' if english else
                                         'Уточни, какому роботу посмотреть в сторону.')
    if request.device is None:
        raise screen_look.ScreenLookError('Turn-and-capture is not configured yet.' if english else
                                         'Поворот головы со снимком пока не подключён.')
    device = request.device
    name = device['name']
    if not device.get('online'):
        raise screen_look.ScreenLookError(f'«{name}» is offline; no command was sent.' if english else
                                         f'«{name}» сейчас не на связи. Команду не отправлял.')
    try:
        result = devices.command(device['id'], ACTION,
                                 {'direction': request.direction,
                                  'expires_at': time.time() + COMMAND_LIFETIME_SEC},
                                 timeout=RESULT_TIMEOUT_SEC)
    except devices.DeviceError as error:
        if error.code == 'asleep':
            text = 'The body is asleep. Say “activate body” first.' if english else 'Тело спит. Сначала скажи «включи тело».'
        elif error.code in ('unknown', 'offline'):
            text = f'«{name}» is offline; no command was sent.' if english else f'«{name}» сейчас не на связи. Команду не отправлял.'
        elif error.code == 'unsupported':
            text = f'Turn-and-capture is not enabled on «{name}».' if english else f'На «{name}» поворот со снимком ещё не включён.'
        elif error.code == 'timeout':
            text = ('No confirmed photo arrived in time; the command will not be retried automatically.'
                    if english else 'Подтверждённый снимок не пришёл вовремя. Автоматически команду не повторяю.')
        else:
            text = ('Could not obtain a confirmed photo from the requested view.' if english else
                    'Не удалось получить подтверждённый снимок из заданного положения.')
        logger.warning('Directed camera request failed: %s', error.code)
        raise screen_look.ScreenLookError(text) from error
    data = result.get('data')
    shot = result.get('image')
    if (not device_head.confirmed_position(data, request.direction)
            or data.get('captured_while_held') is not True
            or data.get('direction_reference') != 'observer_facing_robot'
            or not isinstance(shot, bytes) or not shot.startswith(b'\xff\xd8') or not shot.endswith(b'\xff\xd9')):
        raise screen_look.ScreenLookError('The device did not confirm a photo taken while holding the head.' if english else
                                         'Устройство не подтвердило снимок во время удержания головы.')
    note = (device_look.DEVICE_NOTE.format(name=name)
            + f'Requested view: {request.direction}, relative to a person facing the robot. '
              'The photo was captured while that head command was active. '
            + ('The head is currently held at the target. Automatic idle movements may change its position later. '
               if data.get('idle_enabled') is True else
               'The head position remains held until another command or sleep. ' if data.get('holding') is True
               else 'The motor signal is now off. ')
            + 'No angle was measured. Describe only this image, not other views.\n')
    return device_look.describe_snapshot(local_router, device, shot, question, lang, on_shot, note)
