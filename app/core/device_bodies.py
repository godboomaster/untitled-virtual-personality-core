"""Persona → robot bindings from persona YAML, shared by every chat channel."""
import re

import yaml

from app.core.addons import persona_dirs


class BindingError(ValueError):
    pass


def bindings():
    result, owners, seen = {}, {}, set()
    for directory in persona_dirs():
        for path in sorted(directory.glob('*.yaml')):
            persona = path.stem
            if persona in seen:
                continue  # Same precedence as PersonaLayer: user YAML first.
            seen.add(persona)
            try:
                raw = path.read_text(encoding='utf-8')
            except (OSError, UnicodeError) as error:
                raise BindingError('body-binding-unreadable') from error
            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError as error:
                # Addon directories also contain glossaries/lore YAML, which
                # cannot be selected as personas. Broken persona/binding files
                # fail closed; unrelated auxiliary files do not block robots.
                if re.search(r'^(?:id|system_prompt|body)\s*:', raw, flags=re.MULTILINE):
                    raise BindingError('body-binding-unreadable') from error
                continue
            body = data.get('body') if isinstance(data, dict) else None
            if body is None:
                continue
            device = body.get('device_id') if isinstance(body, dict) else None
            if not isinstance(device, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', device):
                raise BindingError('body-binding-invalid')
            if device in owners:
                raise BindingError('body-bound-to-multiple-personas')
            result[persona] = device
            owners[device] = persona
    return result


def access_problem(persona_name, device_id, mapping=None):
    mapping = bindings() if mapping is None else mapping
    owner = next((name for name, device in mapping.items() if device == device_id), None)
    return 'body-other-persona' if owner is not None and owner != persona_name else None


def problem_text(problem, lang=None):
    english = str(lang).startswith('en')
    pairs = {
        'body-other-persona': ('This robot belongs to another persona. Open its chat to control it.',
                              'Этот робот привязан к другой персоне. Управлять им можно в её чате.'),
        'body-binding-invalid': ('Could not verify the robot binding. Check the persona body.device_id configuration.',
                                 'Не удалось проверить привязку робота. Проверь body.device_id в настройках персон.'),
        'body-unbound': ('No robot body is assigned to this persona.', 'К этой персоне пока не привязано тело робота.'),
        'body-offline': ('The assigned robot is offline. The Pi must be powered on to activate its body.',
                         'Привязанное тело не на связи. Для «включи тело» сама Pi должна быть включена.'),
    }
    pair = pairs.get(problem)
    return pair[0 if english else 1] if pair else None


def ensure_access(persona_name, device_id):
    try:
        problem = access_problem(persona_name, device_id)
    except BindingError:
        problem = 'body-binding-invalid'
    if problem:
        raise BindingError(problem_text(problem))
