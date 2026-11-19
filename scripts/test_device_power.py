"""Shutdown routing tests. No machine power APIs or real device connections."""
import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

from app.core import devices
from app.features import device_power as feature
from scripts.test_device_head import isolated_registry, turn_bot


def robot(device_id='pi', name='Робот', names=('робот', 'robot'), capabilities=None):
    return devices.register(device_id, name, list(names),
                            [feature.ACTION] if capabilities is None else capabilities)


@pytest.mark.parametrize('text', ['выключи себя', 'Робот, выключи себя!',
    'выключи робота', 'робот выключи себя', 'Коннор, выключи себя, пожалуйста.',
    'robot, shut yourself down'])
def test_literal_shutdown_resolves_only_robot(text):
    robot()
    assert feature.find_request(text, ['Коннор']).device['id'] == 'pi'


@pytest.mark.parametrize('text', ['не выключи себя', 'не выключай себя',
    'скажи «выключи себя»', '«выключи себя»', 'можно ли сказать выключи себя?',
    'выключи себя и компьютер', 'выключи Mac', 'выключи ядро',
    'выключи себя\nа потом включись', 'выключи себя через час',
    'выключи себя если будет жарко', 'shut yourself down tomorrow', 'поверни голову прямо'])
def test_narrative_negated_and_compound_text_is_not_shutdown(text):
    robot()
    assert feature.find_request(text, ['Коннор']) is None


def test_offline_unconfigured_and_ambiguous_never_send(monkeypatch):
    send = MagicMock()
    monkeypatch.setattr(devices, 'command', send)
    assert 'не настроена' in feature.execute(feature.find_request('выключи себя'))
    robot()
    robot('other', 'Тележка', ('тележка',))
    assert 'какого робота' in feature.execute(feature.find_request('выключи себя'))
    devices.leave('pi')
    assert 'не на связи' in feature.execute(feature.find_request('робот, выключи себя'))
    send.assert_not_called()
    assert not devices._queues


def ack():
    return {'data': {'status': 'scheduled', 'scheduled_for': time.time() + 10,
                     'platform': 'raspberry_pi_4', 'hardware_power_cut': False}}


def test_accepted_request_is_not_claimed_already_powered_off(monkeypatch):
    robot()
    send = MagicMock(return_value=ack())
    monkeypatch.setattr(devices, 'command', send)
    reply = feature.execute(feature.find_request('выключи себя'))
    assert 'принял запрос' in reply and 'останется включённым' in reply
    assert send.call_args.args[:2] == ('pi', 'robot.poweroff')
    assert set(send.call_args.args[2]) == {'expires_at'}
    assert 0 < send.call_args.args[2]['expires_at'] - time.time() <= 5


@pytest.mark.parametrize('data', [None, [], {'status': 'powered_off'},
    {'status': 'scheduled', 'platform': 'macos', 'hardware_power_cut': False, 'scheduled_for': float('inf')}])
def test_malformed_ack_is_not_success(monkeypatch, data):
    robot()
    monkeypatch.setattr(devices, 'command', MagicMock(return_value={'data': data}))
    assert 'не подтвердила' in feature.execute(feature.find_request('выключи себя'))


def test_missing_confirmation_does_not_promise_no_shutdown_or_retry(monkeypatch):
    robot()
    send = MagicMock(side_effect=devices.DeviceError('timeout'))
    monkeypatch.setattr(devices, 'command', send)
    assert 'могла начать' in feature.execute(feature.find_request('выключи себя'))
    assert send.call_count == 1


def test_owner_allowlist_and_source_guards(monkeypatch):
    import app.bot_instance as module
    robot()
    bot = turn_bot()
    send = MagicMock(return_value='Pi приняла запрос.')
    monkeypatch.setattr(feature, 'execute', send)
    for user in ['owner', 'allowed']:
        assert bot._device_power_turn('выключи себя', user, 'chat') == 'Pi приняла запрос.'
    send.reset_mock()
    assert 'владельцу' in bot._device_power_turn('выключи себя', 'stranger', 'chat')
    for scope in (module._SKIN_TURN, module._PHONE_TURN):
        with monkeypatch.context() as patch:
            patch.setattr(scope, 'on', True, raising=False)
            assert bot._device_power_turn('выключи себя', 'owner', 'chat') is None
    send.assert_not_called()


def test_raw_human_fast_path_saves_real_ack_without_llm(monkeypatch):
    robot()
    bot = turn_bot()
    monkeypatch.setattr(feature, 'execute', MagicMock(return_value='Pi приняла запрос.'))
    assert bot._process_message_impl('выключи себя', user_id='owner', chat_id='chat') == 'Pi приняла запрос.'
    assert bot.memory.add_message.call_args_list[-1].args[:2] == ('assistant', 'Pi приняла запрос.')


@pytest.mark.parametrize('raw,options', [('', {}), ('выключи себя', {'from_skin': True}),
    ('выключи себя', {'turn_note': 'phone result'})])
def test_ocr_and_service_results_do_not_shut_down(monkeypatch, raw, options):
    robot()
    bot = turn_bot()
    send = MagicMock()
    monkeypatch.setattr(feature, 'execute', send)
    class OrdinaryPipelineReached(Exception):
        pass
    def ordinary(*args):
        raise OrdinaryPipelineReached()
    bot._music_player_turn = ordinary
    with pytest.raises(OrdinaryPipelineReached):
        bot._process_message_impl('выключи себя', raw_user_text=raw,
                                  user_id='owner', chat_id='chat', **options)
    send.assert_not_called()


@pytest.mark.parametrize('event', ['register', 'leave'])
@pytest.mark.parametrize('taken', [True, False])
def test_reconnect_and_disconnect_cancel_old_shutdown(event, taken):
    robot()
    result = {}
    thread = threading.Thread(target=lambda: result.update(reply=feature.execute(feature.find_request('выключи себя'))))
    thread.start()
    for _ in range(100):
        if devices._queues.get('pi'):
            break
        time.sleep(.01)
    if taken:
        assert len(asyncio.run(devices.next_commands('pi', 0))) == 1
    robot() if event == 'register' else devices.leave('pi')
    thread.join(2)
    assert not thread.is_alive()
    assert 'не получено' in result['reply']
    assert not devices._queues.get('pi') and not devices._cmds
