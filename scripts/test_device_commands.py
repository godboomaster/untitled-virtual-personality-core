"""Robot slash help is persona-bound and read-only, including offline use."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import device_bodies, devices
from app.features import device_commands
from scripts.test_device_head import isolated_registry


@pytest.fixture(autouse=True)
def assigned_robot(monkeypatch):
    monkeypatch.setattr(device_bodies, 'bindings', lambda: {'iron': 'pi'})


def register(caps=None, state='awake'):
    return devices.register('pi', 'Железяка', ['робот', 'robot'],
                            caps if caps is not None else ['body.wake', 'body.sleep', 'camera.snapshot',
                                'head.turn', 'head.look', 'robot.poweroff'],
                            body={'state': state, 'session': 'a' * 32})


@pytest.mark.parametrize('offline,state', [(False, 'awake'), (False, 'asleep'), (True, 'awake')])
def test_help_remains_available_without_waking_moving_or_photographing(monkeypatch, offline, state):
    register(state=state)
    if offline:
        devices.leave('pi')
    before = devices.list_devices()
    command = MagicMock(side_effect=AssertionError('Help cannot control devices'))
    monkeypatch.setattr(devices, 'command', command)
    reply = device_commands.render('iron')
    for phrase in ['включи тело', 'усни', 'что видит робот?', 'поверни голову влево',
                   'поверни голову вправо', 'поверни голову прямо', 'посмотри влево',
                   'посмотри вправо', 'посмотри прямо', 'выключи себя', '/commands']:
        assert phrase in reply
    assert ('не на связи' if offline else ('тело спит' if state == 'asleep' else 'тело включено')) in reply
    assert [(d['id'], d['body']) for d in devices.list_devices()] == [(d['id'], d['body']) for d in before]
    assert not devices._queues
    command.assert_not_called()


def test_help_uses_assigned_capabilities_and_never_falls_back_to_another_robot():
    register(caps=['camera.snapshot'])
    devices.register('other', 'Other', ['other'], ['head.turn', 'robot.poweroff'])
    reply = device_commands.render('iron')
    assert 'что видит робот?' in reply
    assert 'поверни голову' not in reply and 'выключи себя' not in reply and 'Other' not in reply
    assert 'не привязано' in device_commands.render('unbound')


def test_unregistered_body_does_not_invent_capabilities():
    reply = device_commands.render('iron')
    assert 'ещё не подключался' in reply and 'поверни голову' not in reply


def test_invalid_binding_and_arguments_are_reported_without_actions(monkeypatch):
    def invalid():
        raise device_bodies.BindingError('invalid')
    monkeypatch.setattr(device_bodies, 'bindings', invalid)
    assert 'Не удалось проверить привязку' in device_commands.render('iron')
    assert device_commands.render('iron', 'усни') == 'Использование: /commands'
    assert not devices._queues


def test_english_list_uses_supported_english_commands():
    register()
    reply = device_commands.render('iron', lang='en')
    assert 'activate your body' in reply and 'what does robot see?' in reply
    assert 'shut yourself down' in reply


def web_bot():
    return SimpleNamespace(persona_name='iron', chat_user_language=lambda chat: 'ru')


def test_web_slash_returns_static_robot_help_without_llm():
    from app.api.server import _try_slash_command
    from app.api.schemas import ChatRequest
    register()
    reply, llm_used = _try_slash_command(web_bot(), ChatRequest(persona='iron', message='/commands'))
    assert 'Команды «Железяка»' in reply and llm_used is False
    assert not devices._queues


@pytest.mark.parametrize('text', ['/сommands', '/commаnds', '/команды'])
def test_cyrillic_spellings_are_not_registered_aliases(text):
    from app.api.server import _try_slash_command
    from app.api.schemas import ChatRequest
    register()
    reply, llm_used = _try_slash_command(web_bot(), ChatRequest(persona='iron', message=text))
    assert reply.startswith('Неизвестная команда') and llm_used is False


def test_telegram_commands_handler_shares_read_only_list():
    pytest.importorskip('telegram')
    from app import telegram_bot
    register()
    bot = SimpleNamespace(persona_name='iron', features={}, self_memory=None, file_db=None,
                          _web_search_enabled=False, todo_manager=None, notes=None,
                          reminder_manager=None, inventory_manager=None, learning_manager=None,
                          chat_user_language=lambda chat: 'ru')
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=123),
                             message=SimpleNamespace(reply_text=AsyncMock()))
    handler = telegram_bot.create_handlers(bot)['commands']
    asyncio.run(handler(update, SimpleNamespace(args=[])))
    reply = update.message.reply_text.call_args.args[0]
    assert reply == device_commands.render('iron') and not devices._queues
