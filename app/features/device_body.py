"""Explicit activate/sleep requests for the current persona's assigned body."""
from dataclasses import dataclass
import re
import threading
import time

from app.core import device_bodies, devices
from app.features import device_head

COMMAND_LIFETIME_SEC = 10.0
RESULT_TIMEOUT_SEC = 12.0
_transition_locks = {}
_locks_guard = threading.Lock()


@dataclass(frozen=True)
class Request:
    state: str
    device: dict | None = None
    problem: str | None = None


def parse_request(text, address_names=(), device_names=()):
    if not isinstance(text, str) or len(text) > 250 or '\n' in text:
        return None
    text, wake = re.subn(r'\b(?:включи(?:те)?\s+(?:сво[её]\s+)?тело|activate\s+(?:your\s+)?body)\b',
                         'поверни голову прямо', text, flags=re.IGNORECASE)
    text, sleep = re.subn(r'\b(?:усни|отключи(?:те)?\s+тело|go\s+to\s+sleep)\b',
                          'поверни голову прямо', text, flags=re.IGNORECASE)
    if wake + sleep != 1 or not device_head.parse_request(text, address_names, device_names):
        return None
    return 'awake' if wake else 'asleep'


def find_request(text, persona_name, address_names=()):
    state = parse_request(text, address_names)
    if state is None:
        return None
    try:
        bound = device_bodies.bindings().get(persona_name)
    except device_bodies.BindingError:
        return Request(state, problem='body-binding-invalid')
    if not bound:
        # A normal persona may be asked to sleep as part of a conversation.
        return None if state == 'asleep' else Request(state, problem='body-unbound')
    dev = next((dev for dev in devices.list_devices() if dev['id'] == bound), None)
    return Request(state, dev, None if dev else 'body-offline')


def denied(lang=None):
    return ('Only the owner or an allowed user can activate or put the body to sleep.'
            if str(lang).startswith('en') else
            'Включать и усыплять тело может владелец или разрешённый пользователь.')


def execute(request, lang=None):
    if request.device is None:
        return _execute(request, lang)
    # A sleep requested during wake must use the robot's new confirmed session,
    # rather than being queued with the session from before that wake.
    with _locks_guard:
        lock = _transition_locks.setdefault(request.device['id'], threading.Lock())
    with lock:
        return _execute(request, lang)


def _execute(request, lang=None):
    english = str(lang).startswith('en')
    problem = device_bodies.problem_text(request.problem, lang)
    if problem:
        return problem
    dev = request.device
    if dev is None or not dev.get('online'):
        return device_bodies.problem_text('body-offline', lang)
    action = 'body.wake' if request.state == 'awake' else 'body.sleep'
    try:
        result = devices.command(dev['id'], action,
                                 {'expires_at': time.time() + COMMAND_LIFETIME_SEC},
                                 timeout=RESULT_TIMEOUT_SEC)
    except devices.DeviceError as error:
        if error.code in {'offline', 'unknown'}:
            return device_bodies.problem_text('body-offline', lang)
        if error.code == 'unsupported':
            return ('Body activation and sleep are not enabled on the robot yet.' if english else
                    'На роботе ещё не установлен или не включён режим «включи тело / усни».')
        if error.code == 'body-unknown':
            return ('The robot has not reported its body state. Wait for it to reconnect.' if english else
                    'Робот ещё не сообщил состояние тела. Подожди его переподключения к ядру.')
        return ('The body did not confirm the change. I will not retry automatically.' if english else
                'Тело не подтвердило смену состояния. Автоматически команду не повторяю.')
    data = result.get('data')
    if (not devices.valid_body_status(data) or data['state'] != request.state
            or data.get('camera_enabled') is not (request.state == 'awake')
            or (request.state == 'asleep' and (data.get('pwm_off') is not True or data.get('head_enabled') is not False))):
        return ('The robot did not confirm its body state.' if english else
                'Робот не подтвердил состояние тела.')
    if request.state == 'asleep':
        return ('The body is asleep: camera and head commands are disabled. The Pi stays on.' if english else
                'Тело уснуло: камера и повороты отключены. Pi остаётся включённой.')
    return ('The body is active. You can ask for a photo or an available head action.' if english else
            'Тело включено. Можно попросить снимок.' + (' Повороты головы доступны.' if data.get('head_enabled') is True else ' Повороты головы пока недоступны.'))
