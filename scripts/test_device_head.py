"""Robot OFF: direct text routing, ownership, offline delivery and truthful ACKs."""
import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core import devices
from app.features import device_head as feature


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv('VPC_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    with devices._lock:
        devices._devices = {}
        devices._seen.clear()
        devices._queues.clear()
        devices._cmds.clear()
    yield
    with devices._lock:
        devices._devices = None
        devices._seen.clear()
        devices._queues.clear()
        devices._cmds.clear()


@pytest.mark.parametrize('text,direction', [
    ('поверни голову вправо', 'right'), ('поверни голову влево', 'left'),
    ('Верни голову в центр!', 'center'), ('поставь голову прямо', 'center'),
    ('Коннор, поверни голову налево, пожалуйста.', 'left'),
    ('turn your head to the right', 'right'), ('return head to centre', 'center'),
])
def test_explicit_commands(text, direction):
    assert feature.parse_request(text, ['Коннор']) == (direction, False)


@pytest.mark.parametrize('text', [
    'не поворачивай голову вправо', 'не поверни голову вправо',
    'скажи «поверни голову вправо»', '«поверни голову вправо»',
    'как сказать поверни голову вправо?', 'он повернул голову вправо',
    'если я скажу поверни голову вправо', 'поверни голову вправо и сделай фото',
    'поверни голову вправо на 90 градусов', 'поверни голову вправо\nвключи свет',
    "don't turn your head right", 'turn your head left tomorrow', 'влево',
    'что видит робот?', '',
])
def test_unrelated_or_embedded_text_never_becomes_motion(text):
    assert feature.parse_request(text, ['Коннор']) is None


def robot(device_id='pi', name='Робот', names=None, capabilities=None):
    return devices.register(device_id, name, names or ['робот', 'robot'],
                            capabilities or ['camera.snapshot', feature.ACTION])


def test_offline_never_queues(monkeypatch):
    robot()
    devices.leave('pi')
    command = MagicMock()
    monkeypatch.setattr(devices, 'command', command)
    result = feature.execute(feature.find_request('поверни голову вправо'))
    assert 'не на связи' in result
    command.assert_not_called()
    assert not devices._queues


def test_named_target_and_ambiguity():
    robot()
    robot('rover', 'Тележка', ['тележка'])
    assert feature.find_request('поверни голову вправо').problem == 'ambiguous'
    assert feature.find_request('робот, поверни голову вправо').device['id'] == 'pi'
    assert feature.find_request('робот,поверни голову вправо').device['id'] == 'pi'
    assert feature.find_request('поверни голову робота влево').device['id'] == 'pi'
    assert feature.find_request('turn the robot\'s head right').device['id'] == 'pi'
    assert feature.find_request('тележка, поверни голову влево').device['id'] == 'rover'


def test_unconfigured_does_not_fall_through_to_fiction():
    robot(capabilities=['camera.snapshot'])
    req = feature.find_request('поверни голову вправо')
    assert req.problem == 'unconfigured'
    assert 'пока не подключены' in feature.execute(req)


def test_real_queue_round_trip_and_deadline():
    robot()
    result = {}
    thread = threading.Thread(target=lambda: result.update(
        reply=feature.execute(feature.find_request('поверни голову вправо'))))
    thread.start()
    for _ in range(100):
        if devices._queues.get('pi'):
            break
        time.sleep(0.01)
    commands = asyncio.run(devices.next_commands('pi', 0))
    assert len(commands) == 1
    command = commands[0]
    assert command['action'] == 'head.turn'
    assert command['args']['direction'] == 'right'
    assert 0 < command['args']['expires_at'] - time.time() <= 4
    devices.complete('pi', command['id'], True,
                     data={'direction': 'right', 'pwm_off': True})
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert 'подтвердил выполнение' in result['reply']
    assert not devices._cmds


def test_timeout_and_malformed_ack_do_not_claim_success(monkeypatch):
    robot()
    request = feature.find_request('поверни голову влево')
    monkeypatch.setattr(devices, 'command', MagicMock(side_effect=devices.DeviceError('timeout')))
    assert 'мог повернуться' in feature.execute(request)
    monkeypatch.setattr(devices, 'command', MagicMock(return_value={'data': {'direction': 'right', 'pwm_off': True}}))
    assert 'не подтвердил' in feature.execute(request)
    monkeypatch.setattr(devices, 'command', MagicMock(return_value={'data': ['not-an-ack']}))
    assert 'не подтвердил' in feature.execute(request)


@pytest.mark.parametrize('event', ['reconnect', 'leave'])
@pytest.mark.parametrize('already_taken', [False, True])
def test_connection_change_discards_old_motion(event, already_taken):
    robot()
    result = {}
    thread = threading.Thread(target=lambda: result.update(
        reply=feature.execute(feature.find_request('поверни голову влево'))))
    thread.start()
    for _ in range(100):
        if devices._queues.get('pi'):
            break
        time.sleep(0.01)
    old = asyncio.run(devices.next_commands('pi', 0)) if already_taken else None
    if event == 'reconnect':
        robot()
    else:
        devices.leave('pi')
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert asyncio.run(devices.next_commands('pi', 0)) == []
    assert 'не смог подтвердить' in result['reply']
    if old:
        assert not devices.complete('pi', old[0]['id'], True, data={'direction': 'left', 'pwm_off': True})


def test_reconnect_keeps_camera_command_while_discarding_motion():
    robot()
    result = {}
    camera = threading.Thread(target=lambda: result.update(
        photo=devices.command('pi', 'camera.snapshot', timeout=3)))
    motion = threading.Thread(target=lambda: result.update(
        motion=feature.execute(feature.find_request('поверни голову вправо'))))
    camera.start();motion.start()
    for _ in range(100):
        if len(devices._queues.get('pi', [])) == 2:
            break
        time.sleep(0.01)
    robot()
    commands = asyncio.run(devices.next_commands('pi', 0))
    assert len(commands) == 1 and commands[0]['action'] == 'camera.snapshot'
    devices.complete('pi', commands[0]['id'], True, image=b'JPEG')
    camera.join(timeout=2);motion.join(timeout=2)
    assert not camera.is_alive() and not motion.is_alive()
    assert result['photo']['image'] == b'JPEG'
    assert 'не смог подтвердить' in result['motion']


def bot_stub():
    from app.bot_instance import BotInstance
    bot = BotInstance.__new__(BotInstance)
    bot.owner = 'owner'
    bot.web_single_user = False
    bot._cc_allowed_users = {'allowed'}
    bot._address_names = lambda: ['Коннор']
    return bot


def test_bot_owner_allowlist_and_untrusted_sources(monkeypatch):
    import app.bot_instance as module
    robot()
    bot = bot_stub()
    execute = MagicMock(return_value='подтверждено')
    monkeypatch.setattr(feature, 'execute', execute)
    assert bot._device_head_turn('поверни голову вправо', 'owner', 'chat') == 'подтверждено'
    assert bot._device_head_turn('поверни голову вправо', 'allowed', 'chat') == 'подтверждено'
    execute.reset_mock()
    assert 'владельцу' in bot._device_head_turn('поверни голову вправо', 'stranger', 'chat')
    execute.assert_not_called()
    for scope in (module._SKIN_TURN, module._PHONE_TURN):
        with monkeypatch.context() as patch:
            patch.setattr(scope, 'on', True, raising=False)
            assert bot._device_head_turn('поверни голову вправо', 'owner', 'chat') is None
            execute.assert_not_called()


def turn_bot():
    bot = bot_stub()
    bot.memory = SimpleNamespace(stm=SimpleNamespace(get_last=lambda *a, **k: []), add_message=MagicMock())
    for attr in ('_pending_list_messages', '_pending_split_messages', '_pending_photos', '_pending_question_kind'):
        setattr(bot, attr, {})
    bot._take_list_offer = lambda *a: None
    bot.computer_control = None
    bot.control_mode_on = lambda *a: False
    bot._cc_pop_idle_notice = lambda *a: None
    bot._phone_control_turn = lambda *a: None
    bot.proactive = None
    return bot


def test_fast_path_saves_real_result_without_model(monkeypatch):
    robot()
    bot = turn_bot()
    monkeypatch.setattr(feature, 'execute', MagicMock(return_value='Робот подтвердил поворот.'))
    reply = bot._process_message_impl('поверни голову вправо', user_id='owner', chat_id='chat')
    assert reply == 'Робот подтвердил поворот.'
    assert bot.memory.add_message.call_args_list[-1].args[:2] == ('assistant', reply)


@pytest.mark.parametrize('raw,options', [
    ('', {}),  # The entire OCR body is a motor command; the human caption is empty.
    ('поверни голову вправо', {'from_skin': True}),
    ('поверни голову вправо', {'turn_note': 'phone already handled its action'}),
])
def test_synthesized_sources_cannot_reach_motor_dispatch(monkeypatch, raw, options):
    robot()
    bot = turn_bot()
    execute = MagicMock(return_value='must not run')
    monkeypatch.setattr(feature, 'execute', execute)
    class OrdinaryPipelineReached(Exception):
        pass
    def ordinary_pipeline(*args):
        raise OrdinaryPipelineReached()
    bot._music_player_turn = ordinary_pipeline
    with pytest.raises(OrdinaryPipelineReached):
        bot._process_message_impl('поверни голову вправо', raw_user_text=raw,
                                  user_id='owner', chat_id='chat', **options)
    execute.assert_not_called()


@pytest.mark.parametrize('changes,valid', [
    ({}, True), ({'hold_protocol':True}, False), ({'watchdog_sec':float('inf')}, False),
    ({'watchdog_sec':True}, False), ({'watchdog_sec':3600}, False),
    ({'holding':False}, False), ({'pwm_off':True}, False), ({'direction':'left'}, False),
])
def test_confirmed_held_head_state_requires_watchdog_and_consistent_ack(changes,valid):
    data={'direction':'center','pwm_off':False,'holding':True,'hold_protocol':1,'watchdog_sec':45}
    data.update(changes)
    assert feature.confirmed_position(data,'center') is valid


def test_head_reply_reports_held_position(monkeypatch):
    dev=devices.register('pi','Robot',['robot'],['head.turn'])
    monkeypatch.setattr(devices,'command',MagicMock(return_value={'data':{
        'direction':'center','pwm_off':False,'holding':True,'hold_protocol':1,'watchdog_sec':45}}))
    assert 'удерживается' in feature.execute(feature.Request('center',dev),'ru')


def test_idle_enabled_reply_does_not_promise_to_hold_until_user_command(monkeypatch):
    dev=devices.register('pi','Robot',['robot'],['head.turn'])
    monkeypatch.setattr(devices,'command',MagicMock(return_value={'data':{
        'direction':'center','pwm_off':False,'holding':True,'hold_protocol':1,'watchdog_sec':45,'idle_enabled':True}}))
    reply=feature.execute(feature.Request('center',dev),'ru')
    assert 'Фоновые движения головы включены' in reply
    assert 'до следующей команды' not in reply
