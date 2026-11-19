"""Persona binding and body protocol tests; all devices and models are simulated."""
import asyncio
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from app.core import device_bodies as binding, devices
from app.features import device_body as feature, device_head, device_gaze, device_power
from scripts.test_device_head import isolated_registry, turn_bot


@pytest.fixture(autouse=True)
def persona_files(tmp_path, monkeypatch):
    root = tmp_path / 'personas'
    root.mkdir()
    (root / 'iron.yaml').write_text('id: iron\nbody:\n  device_id: pi\n')
    (root / 'other.yaml').write_text('id: other\n')
    monkeypatch.setattr(binding, 'persona_dirs', lambda: [root])
    return root


def register(state='asleep', device_id='pi', session='a' * 32):
    return devices.register(device_id, 'Робот', ['робот', 'robot'],
                            ['body.wake', 'body.sleep', 'camera.snapshot', 'head.turn', 'head.look', 'robot.poweroff'],
                            body={'state': state, 'session': session})


@pytest.mark.parametrize('text,state', [('включи тело', 'awake'), ('включи своё тело', 'awake'),
    ('Железяка, включи тело, пожалуйста!', 'awake'), ('усни', 'asleep'),
    ('Железяка, усни.', 'asleep'), ('activate your body', 'awake'), ('go to sleep', 'asleep')])
def test_literal_commands(text, state):
    assert feature.parse_request(text, ['Железяка']) == state


@pytest.mark.parametrize('text', ['не усни', 'не включи тело', 'скажи «усни»', '«включи тело»',
    'если я скажу усни', 'усни и выключи Pi', 'включи тело через час', 'усни\nвключи тело',
    'она сказала усни', 'поверни голову прямо', 'можем ли мы включить тело?'])
def test_no_embedded_negated_or_compound_commands(text):
    assert feature.parse_request(text) is None


def test_binding_is_persistent_and_exclusive(persona_files):
    assert binding.bindings() == {'iron': 'pi'}
    (persona_files / 'other.yaml').write_text('body: {device_id: pi}\n')
    with pytest.raises(binding.BindingError, match='multiple-personas'):
        binding.bindings()
    assert feature.find_request('включи тело', 'iron').problem == 'body-binding-invalid'


def test_binding_reloads_after_edit_and_honors_directory_precedence(persona_files, tmp_path, monkeypatch):
    fallback = tmp_path / 'fallback';fallback.mkdir()
    (fallback / 'iron.yaml').write_text('body: {device_id: different}\n')
    monkeypatch.setattr(binding, 'persona_dirs', lambda: [persona_files, fallback])
    assert binding.bindings()['iron'] == 'pi'
    (persona_files / 'iron.yaml').write_text('body: {device_id: newpi}\n')
    assert binding.bindings()['iron'] == 'newpi'


def test_auxiliary_addon_yaml_is_not_a_persona_but_broken_binding_fails_closed(persona_files):
    (persona_files / 'glossary.yaml').write_text('glossary:\n  entry: [broken\n')
    assert binding.bindings() == {'iron': 'pi'}
    (persona_files / 'iron.yaml').write_text('id: iron\nbody: [broken\n')
    with pytest.raises(binding.BindingError, match='unreadable'):
        binding.bindings()


def test_other_persona_never_controls_owned_head_gaze_or_power(monkeypatch):
    register('awake')
    send = MagicMock();monkeypatch.setattr(devices, 'command', send)
    for module, text in [(device_head, 'поверни голову вправо'),
                         (device_gaze, 'посмотри влево'), (device_power, 'выключи себя')]:
        request = module.find_request(text, persona_name='other')
        assert request.problem == 'body-other-persona'
        if module is not device_gaze:
            assert 'другой персоне' in module.execute(request)
    send.assert_not_called()


def test_current_body_wins_over_global_ambiguity_and_wrong_named_body_is_denied(persona_files):
    register('awake')
    register('awake', 'rover')
    (persona_files / 'other.yaml').write_text('body: {device_id: rover}\n')
    assert device_head.find_request('поверни голову вправо', persona_name='iron').device['id'] == 'pi'
    assert device_gaze.find_request('робот, посмотри вправо', persona_name='iron').device['id'] == 'pi'
    assert device_power.find_request('выключи себя', persona_name='other').device['id'] == 'rover'


def test_offline_and_unbound_never_queue(monkeypatch):
    send = MagicMock();monkeypatch.setattr(devices, 'command', send)
    assert 'Pi должна' in feature.execute(feature.find_request('включи тело', 'iron'))
    assert 'не привязано' in feature.execute(feature.find_request('включи тело', 'other'))
    assert feature.find_request('усни', 'other') is None
    register();devices.leave('pi')
    assert 'не на связи' in feature.execute(feature.find_request('усни', 'iron'))
    send.assert_not_called()


def take():
    for _ in range(100):
        if devices._queues.get('pi'):
            return asyncio.run(devices.next_commands('pi', 0))
        time.sleep(.01)
    raise AssertionError('command not queued')


def data(state, session='b' * 32):
    return {'state': state, 'session': session, 'camera_enabled': state == 'awake',
            'head_enabled': state == 'awake', 'pwm_off': True}


def test_wake_and_sleep_roundtrip_updates_observed_state():
    register()
    for text, state, session in [('включи тело', 'awake', 'b' * 32), ('усни', 'asleep', 'c' * 32)]:
        result = {}
        previous = devices.list_devices()[0]['body']['session']
        thread = threading.Thread(target=lambda: result.update(reply=feature.execute(feature.find_request(text, 'iron'))))
        thread.start();cmd = take()[0]
        assert cmd['args']['_body_session'] == previous
        assert 0 < cmd['args']['expires_at'] - time.time() <= 10
        devices.complete('pi', cmd['id'], True, data=data(state, session))
        thread.join(2);assert not thread.is_alive()
        assert ('включено' if state == 'awake' else 'уснуло') in result['reply']
        assert devices.list_devices()[0]['body'] == {'state': state, 'session': session}


def test_sleep_requested_during_wake_waits_for_new_confirmed_session():
    register()
    replies = {}
    wake = threading.Thread(target=lambda: replies.update(wake=feature.execute(feature.find_request('включи тело', 'iron'))))
    wake.start();wake_cmd = take()[0]
    sleep = threading.Thread(target=lambda: replies.update(sleep=feature.execute(feature.find_request('усни', 'iron'))))
    sleep.start()
    time.sleep(.02)
    assert not devices._queues.get('pi')
    devices.complete('pi', wake_cmd['id'], True, data=data('awake'))
    sleep_cmd = take()[0]
    assert sleep_cmd['action'] == 'body.sleep' and sleep_cmd['args']['_body_session'] == 'b' * 32
    devices.complete('pi', sleep_cmd['id'], True, data=data('asleep', 'c' * 32))
    wake.join(2);sleep.join(2)
    assert not wake.is_alive() and not sleep.is_alive()
    assert 'уснуло' in replies['sleep']


def test_sleeping_camera_and_head_block_before_queue_but_poweroff_remains_available():
    register()
    for action in devices.BODY_PROTECTED:
        with pytest.raises(devices.DeviceError) as raised:
            devices.command('pi', action, timeout=0)
        assert raised.value.code == 'asleep'
    assert not devices._queues
    # Poweroff is allowed while asleep; a zero wait gives a timeout, not asleep.
    with pytest.raises(devices.DeviceError) as raised:
        devices.command('pi', 'robot.poweroff', timeout=0)
    assert raised.value.code == 'timeout'


def test_camera_gets_current_session_and_start_deadline():
    register('awake')
    result = {}
    thread = threading.Thread(target=lambda: result.update(res=devices.command('pi', 'camera.snapshot', timeout=2)))
    thread.start();cmd = take()[0]
    assert cmd['args']['_body_session'] == 'a' * 32
    assert 0 < cmd['args']['_body_expires_at'] - time.time() <= 4
    devices.complete('pi', cmd['id'], True, image=b'jpeg')
    thread.join(2);assert not thread.is_alive()


def test_sleep_cancels_unsent_camera_without_delaying_sleep():
    register('awake')
    outcome = {}
    def camera():
        try:
            devices.command('pi', 'camera.snapshot', timeout=2)
        except devices.DeviceError as error:
            outcome['camera'] = error.code
    shot = threading.Thread(target=camera);shot.start()
    for _ in range(100):
        if devices._queues.get('pi'):break
        time.sleep(.01)
    sleeper = threading.Thread(target=lambda: outcome.update(reply=feature.execute(feature.find_request('усни', 'iron'))))
    sleeper.start();shot.join(2)
    commands = take()
    assert len(commands) == 1 and commands[0]['action'] == 'body.sleep'
    devices.complete('pi', commands[0]['id'], True, data=data('asleep'))
    sleeper.join(2)
    assert outcome['camera'] == 'failed' and 'уснуло' in outcome['reply']


def test_failed_cleanup_still_reports_asleep_metadata_to_core():
    register('awake')
    thread = threading.Thread(target=lambda: feature.execute(feature.find_request('усни', 'iron')))
    thread.start();cmd = take()[0]
    devices.complete('pi', cmd['id'], False, error='cleanup failed', body={'state': 'asleep', 'session': 'b' * 32})
    thread.join(2)
    assert devices.list_devices()[0]['body']['state'] == 'asleep'


@pytest.mark.parametrize('action', ['body.wake', 'body.sleep', 'camera.snapshot'])
def test_reconnect_cancels_stale_body_work(action):
    register('awake')
    done = {}
    def send():
        try:devices.command('pi', action, {'expires_at': time.time() + 10}, timeout=2)
        except devices.DeviceError as error:done['error'] = error.code
    thread = threading.Thread(target=send);thread.start()
    take();register('asleep', session='b' * 32)
    thread.join(2)
    assert done['error'] == 'failed'
    assert not devices._queues.get('pi')


def test_malformed_or_missing_confirmation_never_claims_success(monkeypatch):
    register()
    request = feature.find_request('включи тело', 'iron')
    for response in [{'data': data('asleep')}, {'data': {'state': 'awake'}}, {'data': []}]:
        monkeypatch.setattr(devices, 'command', MagicMock(return_value=response))
        assert 'не подтвердил' in feature.execute(request)
    send = MagicMock(side_effect=devices.DeviceError('timeout'))
    monkeypatch.setattr(devices, 'command', send)
    assert 'не повторяю' in feature.execute(request)
    assert send.call_count == 1


def test_owner_gate_and_no_llm_fast_path(monkeypatch):
    register();bot = turn_bot();bot.persona_name = 'iron'
    send = MagicMock(return_value='Тело включено.')
    monkeypatch.setattr(feature, 'execute', send)
    assert 'владелец' in bot._device_body_turn('включи тело', 'stranger', 'chat')
    send.assert_not_called()
    reply = bot._process_message_impl('включи тело', user_id='owner', chat_id='chat')
    assert reply == 'Тело включено.'
    assert bot.memory.add_message.call_args_list[-1].args[:2] == ('assistant', reply)


@pytest.mark.parametrize('raw,options', [('', {}), ('усни', {'from_skin': True}),
    ('усни', {'turn_note': 'phone handled'})])
def test_synthetic_text_is_not_a_body_request(monkeypatch, raw, options):
    register();bot = turn_bot();bot.persona_name = 'iron'
    send = MagicMock();monkeypatch.setattr(feature, 'execute', send)
    class OrdinaryPipelineReached(Exception):pass
    def ordinary(*args):raise OrdinaryPipelineReached()
    bot._music_player_turn = ordinary
    with pytest.raises(OrdinaryPipelineReached):
        bot._process_message_impl('усни', raw_user_text=raw, user_id='owner', chat_id='chat', **options)
    send.assert_not_called()


def test_api_camera_and_gaze_enforce_binding_and_sleep():
    from app.api import server
    from app.api.schemas import ChatRequest
    from fastapi import HTTPException
    register()
    bot = SimpleNamespace(persona_name='other', _address_names=lambda: [],
                          _cc_allowed=lambda *args: True, control_mode_on=lambda *args: False)
    for text in ['что видит робот?', 'посмотри вправо']:
        with pytest.raises(HTTPException) as raised:
            server._prepare_turn_input(bot, ChatRequest(persona='other', message=text))
        assert 'другой персоне' in str(raised.value.detail)
    bot.persona_name = 'iron'
    with pytest.raises(HTTPException) as raised:
        server._prepare_turn_input(bot, ChatRequest(persona='iron', message='что видит робот?'))
    assert 'Тело спит' in str(raised.value.detail)
    assert not devices._queues


def test_device_body_schema_rejects_bad_state_or_session():
    from app.api.schemas import DeviceHello
    from pydantic import ValidationError
    for status in [{'state': 'awake', 'session': 'wrong'}, {'state': 'booting', 'session': 'a' * 32}]:
        with pytest.raises(ValidationError):
            DeviceHello(id='pi', name='Робот', body=status)


def test_zhelezyaka_persona_has_memory_and_no_autonomous_features():
    path = Path(__file__).resolve().parents[1] / 'data/personas/zhelezyaka.yaml'
    cfg = yaml.safe_load(path.read_text())
    from app.core.living_persona import LivingPersonaConfig
    assert cfg['body']['device_id'] == 'vpc-pi'
    assert cfg['stm_size'] == 50 and cfg['features']['ltm'] is True
    assert cfg['fresh_stm'] is False
    for name in ['proactive', 'rhythm', 'learning']:
        assert cfg['features'][name]['enabled'] is False
    assert cfg['features']['computer_control'] is False
    assert LivingPersonaConfig(cfg['features']).enabled is False


def test_body_client_must_reintroduce_itself_after_core_loses_session():
    register(state='awake')
    with devices._lock:devices._seen.clear()
    assert asyncio.run(devices.next_commands('pi',0)) is None
    register(state='awake',session='b'*32)
    assert asyncio.run(devices.next_commands('pi',0))==[]
