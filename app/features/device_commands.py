"""Read-only command help for the robot assigned to the current persona."""
from app.core import device_bodies, devices


_COMMANDS = (
    ('body.wake', 'включи тело', 'разрешить камеру и повороты',
     'activate your body', 'enable the camera and head commands'),
    ('body.sleep', 'усни', 'отключить камеру и удержание головы; Pi остаётся включённой',
     'go to sleep', 'disable the camera and release the head; the Pi stays on'),
    ('camera.snapshot', 'что видит {name}?', 'снимок и описание без поворота головы',
     'what does {name} see?', 'take and describe a photo without turning the head'),
    ('head.turn', 'поверни голову влево', 'повернуть влево',
     'turn your head left', 'turn left'),
    ('head.turn', 'поверни голову вправо', 'повернуть вправо',
     'turn your head right', 'turn right'),
    ('head.turn', 'поверни голову прямо', 'вернуть в центр',
     'return your head to center', 'return to center'),
    ('head.look', 'посмотри влево', 'поворот, снимок и описание',
     'look left', 'turn, take a photo and describe it'),
    ('head.look', 'посмотри вправо', 'поворот, снимок и описание',
     'look right', 'turn, take a photo and describe it'),
    ('head.look', 'посмотри прямо', 'голова в центр, снимок и описание',
     'look straight ahead', 'center the head, take a photo and describe it'),
    ('robot.poweroff', 'выключи себя', 'штатно выключить Raspberry Pi',
     'shut yourself down', 'shut down the Raspberry Pi'),
)


def render(persona_name, args='', lang=None):
    english = str(lang).startswith('en')
    if args.strip():
        return 'Usage: /commands' if english else 'Использование: /commands'
    try:
        device_id = device_bodies.bindings().get(persona_name)
    except device_bodies.BindingError:
        return device_bodies.problem_text('body-binding-invalid', lang)
    if not device_id:
        return (device_bodies.problem_text('body-unbound', lang)
                + ('\nGeneral chat commands: /help' if english else '\nОбщие команды чата: /help'))
    device = next((d for d in devices.list_devices() if d['id'] == device_id), None)
    if device is None:
        return ('The assigned robot has not registered yet. Start its service, then try /commands again.'
                if english else 'Привязанный робот ещё не подключался. Запусти его службу, затем повтори /commands.')

    caps = set(device.get('capabilities') or [])
    names = device.get('names') or [device['name']]
    preferred = 'robot' if english else 'робот'
    camera_name = preferred if preferred in names else names[0]
    lines = [f'Commands for «{device["name"]}»:' if english else f'Команды «{device["name"]}»:']
    if not device.get('online'):
        lines.append('Offline. Commands listed from its last connection.' if english else
                     'Сейчас не на связи. Список по последнему подключению.')
    else:
        state = (device.get('body') or {}).get('state')
        states = {'awake': ('body active', 'тело включено'), 'asleep': ('body asleep', 'тело спит')}
        state_text = states.get(state, ('body state unknown', 'состояние тела неизвестно'))
        lines.append(('Online; ' + state_text[0] + '.') if english else ('На связи; ' + state_text[1] + '.'))
    lines.append('')
    entries = []
    for capability, ru_command, ru_description, en_command, en_description in _COMMANDS:
        if capability in caps:
            command, description = (en_command, en_description) if english else (ru_command, ru_description)
            entries.append(f'• {command.format(name=camera_name)} — {description}.')
    lines.extend(entries or ['No robot commands are enabled.' if english else 'Команды тела пока не включены.'])
    if 'body.wake' in caps:
        lines.extend(['', 'The Pi must already be powered on and its service running to activate the body.'
                      if english else 'Для «включи тело» сама Pi должна быть включена, а служба робота запущена.'])
    lines.extend(['', '/commands — show this list' if english else '/commands — показать этот список'])
    return '\n'.join(lines)
