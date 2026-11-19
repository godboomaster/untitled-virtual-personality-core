"""Offline directional vision: one motor/photo command, one real returned frame."""
import asyncio
import base64
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.test_device_head import isolated_registry, turn_bot
from app.core import devices
from app.features import device_gaze as gaze, screen_look

JPEG = b'\xff\xd8DIRECTED-PHOTO\xff\xd9'


class Local:
    _available = True
    def __init__(self): self.images = []
    def ocr_image(self, image_bytes, question='', lang=None, **kwargs):
        self.images.append(image_bytes)
        return 'TEXT: нет\nDESCRIPTION: справа стоит шкаф'


def register(device_id='pi', name='Робот', names=None, capabilities=None):
    return devices.register(device_id, name, names or ['робот', 'robot'], capabilities or ['camera.snapshot', 'head.turn', gaze.ACTION])


def result(direction='right', **changes):
    value = {'ok': True, 'image': JPEG, 'data': {
        'direction': direction, 'pwm_off': True, 'captured_while_held': True,
        'direction_reference': 'observer_facing_robot'}}
    value.update(changes)
    return value


def api_bot(allowed=True, control=False):
    return SimpleNamespace(_local_router=Local(), _address_names=lambda: ['Коннор'],
                           _cc_allowed=lambda *a: allowed, control_mode_on=lambda *a: control,
                           chat_user_language=lambda *a: 'ru')


@pytest.mark.parametrize('text,direction', [
    ('посмотри вправо', 'right'), ('посмотри влево', 'left'),
    ('Коннор, посмотри направо, пожалуйста!', 'right'),
    ('пожалуйста,посмотри налево', 'left'), ('look right', 'right'),
    ('look to the left', 'left'),
    ('посмотри прямо', 'center'), ('Коннор, посмотри прямо, пожалуйста!', 'center'),
    ('посмотри в центр', 'center'), ('look straight ahead', 'center'),
    ('look center', 'center'), ('look to the centre', 'center'),
])
def test_gaze_imperatives(text, direction):
    assert gaze.parse_request(text, ['Коннор']) == (direction, False)


@pytest.mark.parametrize('text', [
    'поверни голову вправо', 'что видит робот?', 'не посмотри вправо',
    'не смотри вправо', 'скажи «посмотри вправо»', '«посмотри вправо»',
    'как сказать посмотри вправо', 'он посмотрел вправо',
    'посмотри вправо и влево', 'посмотри вправо на 90 градусов',
    'посмотри вправо\nсфоткай', 'look right tomorrow', "don't look left",
    'не посмотри прямо', 'скажи «посмотри прямо»', '«посмотри прямо»',
    'посмотри прямо и вправо', 'посмотри прямо на 90 градусов',
])
def test_gaze_rejects_embedded_negative_and_compound_commands(text):
    assert gaze.parse_request(text) is None


def test_named_device_and_ambiguity():
    register();register('rover', 'Тележка', ['тележка'])
    assert gaze.find_request('посмотри вправо').problem == 'ambiguous'
    assert gaze.find_request('Робот, посмотри вправо').device['id'] == 'pi'
    assert gaze.find_request('тележка,посмотри влево').device['id'] == 'rover'


def test_offline_and_unsupported_never_use_plain_snapshot(monkeypatch):
    register();devices.leave('pi')
    command = MagicMock();monkeypatch.setattr(devices, 'command', command)
    with pytest.raises(screen_look.ScreenLookError, match='не на связи'):
        gaze.capture_input(Local(), gaze.find_request('посмотри вправо'), 'посмотри вправо')
    command.assert_not_called()
    register(capabilities=['camera.snapshot', 'head.turn'])
    with pytest.raises(screen_look.ScreenLookError, match='пока не подключён'):
        gaze.capture_input(Local(), gaze.find_request('посмотри вправо'), 'посмотри вправо')
    command.assert_not_called()


def test_capture_sends_one_composite_command_and_only_image_to_local_vision(monkeypatch):
    register();local=Local();events=[]
    command=MagicMock(return_value=result());monkeypatch.setattr(devices,'command',command)
    text, image=gaze.capture_input(local,gaze.find_request('посмотри вправо'),'посмотри вправо','ru',lambda shot: events.append(shot))
    assert command.call_count==1 and command.call_args.args[:2]==('pi','head.look')
    args=command.call_args.args[2]
    assert args['direction']=='right' and 0 < args['expires_at']-time.time() <= 8
    assert local.images==[JPEG] and events==[JPEG] and image==JPEG
    assert 'Requested view: right' in text and 'справа стоит шкаф' in text


def test_idle_enabled_photo_note_does_not_promise_permanent_position(monkeypatch):
    register()
    value=result()
    value['data'].update(pwm_off=False, holding=True, hold_protocol=1, watchdog_sec=45, idle_enabled=True)
    monkeypatch.setattr(devices, 'command', MagicMock(return_value=value))
    text, _=gaze.capture_input(Local(),gaze.find_request('посмотри вправо'),'посмотри вправо')
    assert 'Automatic idle movements may change its position later' in text
    assert 'until another command' not in text


@pytest.mark.parametrize('bad', [
    result(direction='left'), result(image=b'not-a-jpeg'),
    result(data={'direction':'right','pwm_off':False,'captured_while_held':True}),
    result(data={'direction':'right','pwm_off':True,'captured_while_held':False}),
    result(data=['invalid']),
])
def test_bad_capture_evidence_is_never_described(monkeypatch,bad):
    register();local=Local();callback=MagicMock()
    monkeypatch.setattr(devices,'command',MagicMock(return_value=bad))
    with pytest.raises(screen_look.ScreenLookError,match='не подтвердило'):
        gaze.capture_input(local,gaze.find_request('посмотри вправо'),'посмотри вправо',on_shot=callback)
    assert not local.images;callback.assert_not_called()


@pytest.mark.parametrize('direction,phrase', [('right', 'вправо'), ('center', 'прямо')])
def test_api_routes_direction_before_ordinary_camera(monkeypatch,direction,phrase):
    from app.api import server
    from app.api.schemas import ChatRequest
    register();bot=api_bot();photos=[]
    command=MagicMock(return_value=result(direction));monkeypatch.setattr(devices,'command',command)
    text,shown=server._prepare_turn_input(bot,ChatRequest(persona='connor',message=f'робот, посмотри {phrase}'),photos)
    assert command.call_count==1 and command.call_args.args[1]=='head.look'
    assert command.call_args.args[2]['direction']==direction
    assert shown==f'📷 робот, посмотри {phrase}' and 'справа стоит шкаф' in text
    assert photos==[(JPEG,f'Робот · {phrase}')]


@pytest.mark.parametrize('source',['skin','phone','control','unauthorized'])
def test_api_source_and_owner_gates(monkeypatch,source):
    from app.api import server
    from app.api.schemas import ChatRequest
    from fastapi import HTTPException
    register();command=MagicMock();monkeypatch.setattr(devices,'command',command)
    req=ChatRequest(persona='connor',message='робот, посмотри вправо',from_skin=source=='skin')
    if source=='phone':req=req.model_copy(update={'phone_action':object()})
    if source=='unauthorized':
        with pytest.raises(HTTPException) as error:server._prepare_turn_input(api_bot(allowed=False),req)
        assert error.value.status_code==403
    else:
        assert server._prepare_turn_input(api_bot(control=source=='control'),req)==(req.message,None)
    command.assert_not_called()


def test_unsupported_compound_is_not_replaced_by_unturned_photo(monkeypatch):
    from app.api import server
    from app.api.schemas import ChatRequest
    register();command=MagicMock();monkeypatch.setattr(devices,'command',command)
    req=ChatRequest(persona='connor',message='робот, посмотри вправо на 90 градусов')
    assert server._prepare_turn_input(api_bot(),req)==(req.message,None)
    command.assert_not_called()


@pytest.mark.parametrize('event',['reconnect','leave'])
def test_connection_change_cancels_old_gaze(event):
    register();out={}
    def call():
        try:gaze.capture_input(Local(),gaze.find_request('посмотри вправо'),'посмотри вправо')
        except screen_look.ScreenLookError as error:out['error']=str(error)
    thread=threading.Thread(target=call);thread.start()
    for _ in range(100):
        if devices._queues.get('pi'):break
        time.sleep(0.01)
    register() if event=='reconnect' else devices.leave('pi')
    thread.join(timeout=2)
    assert not thread.is_alive() and 'подтверждённый снимок' in out['error']
    assert not devices._queues.get('pi')


@pytest.mark.parametrize('raw,options', [('',{}),('посмотри вправо',{'from_skin':True}),
                                        ('посмотри вправо',{'turn_note':'phone result'})])
def test_direct_channel_synthesized_text_never_moves(monkeypatch,raw,options):
    register();bot=turn_bot();capture=MagicMock();monkeypatch.setattr(gaze,'capture_input',capture)
    class Ordinary(Exception):pass
    bot._music_player_turn=lambda *a: (_ for _ in ()).throw(Ordinary())
    with pytest.raises(Ordinary):bot._process_message_impl('посмотри вправо',raw_user_text=raw,user_id='owner',chat_id='chat',**options)
    capture.assert_not_called()


def test_direct_channel_captures_once_and_api_prepared_input_is_not_recaptured(monkeypatch):
    register();bot=turn_bot();called=[]
    def capture(local,request,question,lang,on_shot=None):
        called.append(question);on_shot(JPEG)
        return 'LOCAL DESCRIPTION of right view',JPEG
    monkeypatch.setattr(gaze,'capture_input',capture)
    class Ordinary(Exception):pass
    bot._music_player_turn=lambda *a: (_ for _ in ()).throw(Ordinary())
    with pytest.raises(Ordinary):bot._process_message_impl('посмотри вправо',user_id='owner',chat_id='chat')
    assert called==['посмотри вправо'] and bot._pending_photos['chat'][0]['data']==JPEG
    with pytest.raises(Ordinary):bot._process_message_impl('LOCAL DESCRIPTION of right view',raw_user_text='посмотри вправо',user_id='owner',chat_id='chat')
    assert len(called)==1


class StreamBot:
    features = {}
    router = SimpleNamespace(_last_provider=None, active_provider=None)
    def __init__(self, local):
        self._local_router=local;self.stm=[];self.photos={}
        self.memory=SimpleNamespace(stm=SimpleNamespace(
            get_messages=lambda **kw:list(self.stm),
            pop_last_n=lambda n,key:[self.stm.pop() for _ in range(min(n,len(self.stm)))],
            add_message=lambda role,text,uid,key:self.stm.append({'role':role,'content':text,'timestamp':time.time()})))
    def _address_names(self):return []
    def _cc_allowed(self,*a):return True
    def control_mode_on(self,*a):return False
    def chat_user_language(self,*a):return 'ru'
    def process_message(self,text,chat_id=None,**kw):
        self.input=text;self.photos[str(chat_id)]=[]
        self.stm=[{'role':'user','content':text,'timestamp':time.time()},
                  {'role':'assistant','content':'Справа шкаф.','timestamp':time.time()}]
        return 'Справа шкаф.'
    def add_pending_photo(self,key,data,caption=''):self.photos.setdefault(str(key),[]).append({'data':data,'caption':caption})
    def pop_pending_photos(self,key):return self.photos.pop(str(key),[])
    def _turn_stm_tail(self,key):return self.stm
    def pop_pending_split_messages(self,*a):return []
    def pop_pending_list_messages(self,*a):return []
    def pop_pending_question_kind(self,*a):return None
    def pop_pending_files(self,*a):return []
    def pop_reply_history_text(self,*a):return None
    def cc_answer_options(self,*a):return None
    def cc_task_card(self,*a):return None


def test_stream_delivers_directed_photo_before_description_finishes(monkeypatch):
    from app.api import server
    from app.api.schemas import ChatRequest
    register();released=threading.Event();waited=[]
    class SlowLocal(Local):
        def ocr_image(self,image_bytes,question='',lang=None,**kwargs):
            waited.append(released.wait(5))
            return super().ocr_image(image_bytes,question,lang,**kwargs)
    bot=StreamBot(SlowLocal())
    async def get_bot(*args):return bot
    async def touch(*args):pass
    monkeypatch.setattr(server,'_get_bot',get_bot)
    monkeypatch.setattr(server,'_touch_persona',touch)
    command=MagicMock(return_value=result());monkeypatch.setattr(devices,'command',command)
    async def consume():
        req=ChatRequest(persona='connor',message='посмотри вправо',chat_id='gaze-stream')
        transport=SimpleNamespace(headers={'host':'127.0.0.1:8000'},client=SimpleNamespace(host='127.0.0.1'))
        response=await server.chat_stream(req,transport)
        events=[]
        async for chunk in response.body_iterator:
            for frame in chunk.split('\n\n'):
                if frame.startswith('data: '):
                    event=json.loads(frame[6:]);events.append(event)
                    if 'photo' in event:released.set()
        return events
    events=asyncio.run(consume())
    photo=next(i for i,event in enumerate(events) if 'photo' in event)
    token=next(i for i,event in enumerate(events) if 'token' in event)
    url='data:image/jpeg;base64,'+base64.b64encode(JPEG).decode()
    assert waited==[True] and photo<token
    assert events[photo]['photo']==url
    assert events[-1]['done'] and events[-1]['images']==[url] and events[-1]['reply']=='Справа шкаф.'
    assert command.call_count==1 and command.call_args.args[1]=='head.look'


def test_held_gaze_accepts_guarded_position_and_describes_holding_truthfully(monkeypatch):
    dev=register()
    data={'direction':'right','pwm_off':False,'holding':True,'hold_protocol':1,'watchdog_sec':45,
          'captured_while_held':True,'direction_reference':'observer_facing_robot'}
    monkeypatch.setattr(devices,'command',MagicMock(return_value=result(data=data)))
    marker=object();describe=MagicMock(return_value=marker)
    monkeypatch.setattr(gaze.device_look,'describe_snapshot',describe)
    assert gaze.capture_input(Local(),gaze.device_head.Request('right',dev),'посмотри вправо','ru') is marker
    note=describe.call_args.args[-1]
    assert 'position remains held' in note and 'motor signal is now off' not in note
