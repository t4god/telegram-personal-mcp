import asyncio
from types import SimpleNamespace
import pytest
from telegram_dot.event_queue import EventQueue, QueueError
from telegram_dot.store import Store
from telegram_dot.server import Connector


def test_queue_deduplication(tmp_path):
    q=EventQueue(tmp_path/'events.db')
    e={'dedupe':'test:1','type':'message.created','text':'example'}
    q.append([e]); q.append([e])
    assert len(q.page()['events'])==1


def test_explicit_initial_cursor(tmp_path):
    q=EventQueue(tmp_path/'events.db')
    with pytest.raises(QueueError): q.read('consumer','worker')


def test_store_recovers_uncertain(tmp_path):
    s=Store(tmp_path/'requests.db')
    with s.connection() as db:
        db.execute("INSERT INTO requests VALUES('id','key','1','test','example',NULL,'digest',9999999999,'dispatching',NULL,1)")
    s=Store(tmp_path/'requests.db')
    with s.connection() as db:
        assert db.execute('SELECT status FROM requests').fetchone()[0]=='uncertain'


class Telegram:
    def __init__(self,fail=False): self.sends=0; self.fail=fail; self.text=None
    async def send_message(self,entity,text,**kwargs):
        self.sends+=1; self.text=text
        if self.fail: raise TimeoutError()
        return SimpleNamespace(id=42)
    async def get_messages(self,entity,ids): return SimpleNamespace(raw_text=self.text)


def connector(tmp_path, monkeypatch, fail=False):
    c=Connector.__new__(Connector)
    c.store=Store(tmp_path/'requests.db');c.lock=asyncio.Lock();c.telegram=Telegram(fail)
    async def resolve(chat): return SimpleNamespace(id=int(chat))
    c.resolve=resolve
    monkeypatch.setattr('telegram_dot.server.utils.get_peer_id',lambda e:e.id)
    monkeypatch.setattr('telegram_dot.server.utils.get_display_name',lambda e:'Test recipient')
    return c

@pytest.mark.asyncio
async def test_send_and_reply_idempotent(tmp_path,monkeypatch):
    c=connector(tmp_path,monkeypatch);monkeypatch.setenv('DOT_SEND_ENABLED','true')
    p=await c.prepare('123','Hello','request-123',7)
    assert p['reply_to_message_id']==7
    a=await c.dispatch(p['request_id'],'123','Hello')
    b=await c.dispatch(p['request_id'],'123','Hello')
    assert a['readback_verified'] and b['status']=='sent' and c.telegram.sends==1

@pytest.mark.asyncio
async def test_send_mismatch_rejected(tmp_path,monkeypatch):
    c=connector(tmp_path,monkeypatch);monkeypatch.setenv('DOT_SEND_ENABLED','true')
    p=await c.prepare('123','Hello','request-123',None)
    with pytest.raises(ValueError): await c.dispatch(p['request_id'],'124','Hello')
    assert c.telegram.sends==0

@pytest.mark.asyncio
async def test_send_uncertain_no_retry(tmp_path,monkeypatch):
    c=connector(tmp_path,monkeypatch,True);monkeypatch.setenv('DOT_SEND_ENABLED','true')
    p=await c.prepare('123','Hello','request-123',None)
    assert (await c.dispatch(p['request_id'],'123','Hello'))['status']=='uncertain'
    await c.dispatch(p['request_id'],'123','Hello')
    assert c.telegram.sends==1

@pytest.mark.asyncio
async def test_send_owner_switch(tmp_path,monkeypatch):
    c=connector(tmp_path,monkeypatch);monkeypatch.setenv('DOT_SEND_ENABLED','false')
    p=await c.prepare('123','Hello','request-123',None)
    with pytest.raises(PermissionError): await c.dispatch(p['request_id'],'123','Hello')
    assert c.telegram.sends==0


class SavedTelegram:
    def __init__(self):
        self.forward_calls=[]
        self.me=SimpleNamespace(id=123)
        self.saved=SimpleNamespace(id=123)
    async def get_me(self): return self.me
    async def get_input_entity(self,peer):
        assert peer=='me'
        return self.saved
    async def get_messages(self,source,ids): return [SimpleNamespace(id=i) for i in ids]
    async def forward_messages(self,destination,ids,from_peer):
        self.forward_calls.append((destination,ids,from_peer))
        return [SimpleNamespace(id=i+100) for i in ids]
    async def iter_messages(self,entity,limit,offset_id):
        yield SimpleNamespace(id=12,fwd_from=SimpleNamespace(from_id=SimpleNamespace(id=77),
            saved_from_peer=SimpleNamespace(id=456),saved_from_msg_id=9),
            date=SimpleNamespace(isoformat=lambda:'2026-01-01T00:00:00+00:00'),raw_text='forwarded',media=object())


def saved_connector(monkeypatch):
    c=Connector.__new__(Connector);c.owner=123;c.lock=asyncio.Lock();c.telegram=SavedTelegram()
    async def resolve(chat): return SimpleNamespace(id=int(chat))
    c.resolve=resolve
    monkeypatch.setattr('telegram_dot.server.utils.get_peer_id',lambda e:e.id)
    return c


@pytest.mark.asyncio
async def test_forward_saved_requires_exact_approval_and_owner_destination(monkeypatch):
    c=saved_connector(monkeypatch)
    monkeypatch.setenv('DOT_SEND_ENABLED','false')
    with pytest.raises(PermissionError,match='disabled'):
        await c.forward_saved('456',[7],'123','456',[7],'123')
    assert c.telegram.forward_calls==[]
    monkeypatch.setenv('DOT_SEND_ENABLED','true')
    with pytest.raises(ValueError,match='exactly match'):
        await c.forward_saved('456',[7],'123','456',[8],'123')
    with pytest.raises(PermissionError,match='Saved Messages'):
        await c.forward_saved('456',[7],'789','456',[7],'789')
    assert c.telegram.forward_calls==[]
    result=await c.forward_saved('456',[7,8],'123','456',[7,8],'123')
    assert result['forwarded_message_ids']==[107,108]
    assert c.telegram.forward_calls==[(c.telegram.saved,[7,8],SimpleNamespace(id=456))]


@pytest.mark.asyncio
async def test_saved_preview_groups_recoverable_origins(monkeypatch):
    c=saved_connector(monkeypatch)
    result=await c.preview_saved(10,0)
    group=result['groups'][0]
    assert group['original_sender_id']=='77'
    assert group['messages'][0]['source_chat_id']=='456'
    assert group['messages'][0]['source_message_id']==9
    assert group['messages'][0]['forwardable'] is True


@pytest.mark.asyncio
async def test_saved_preview_handles_empty_telegram_history_entries(monkeypatch):
    from telethon.tl import types

    empty=types.MessageEmpty(id=11,peer_id=types.PeerUser(123))
    c=saved_connector(monkeypatch)
    class EmptyHistoryTelegram(SavedTelegram):
        async def iter_messages(self,entity,limit,offset_id):
            yield empty
    c.telegram=EmptyHistoryTelegram()

    result=await c.preview_saved(10,0)

    assert result['items_scanned']==1
    assert result['unforwardable_count']==1
    assert result['next_before_message_id']==11
    item=result['groups'][0]['messages'][0]
    assert item['saved_message_id']==11
    assert item['date_utc'] is None
    assert item['source_chat_id'] is None
    assert item['forwardable'] is False


@pytest.mark.asyncio
async def test_saved_preview_tolerates_unreadable_optional_fields(monkeypatch):
    class PartialMessage:
        id=13
        @property
        def fwd_from(self): raise RuntimeError('synthetic unavailable field')
        @property
        def date(self): raise RuntimeError('synthetic unavailable field')
        @property
        def raw_text(self): raise RuntimeError('synthetic unavailable field')
        @property
        def media(self): raise RuntimeError('synthetic unavailable field')

    c=saved_connector(monkeypatch)
    class PartialHistoryTelegram(SavedTelegram):
        async def iter_messages(self,entity,limit,offset_id):
            yield PartialMessage()
    c.telegram=PartialHistoryTelegram()

    result=await c.preview_saved(1,0)

    assert result['items_scanned']==1
    assert result['next_before_message_id']==13
    item=result['groups'][0]['messages'][0]
    assert item['date_utc'] is None
    assert item['text_preview']==''
    assert item['has_media'] is False
    assert item['forwardable'] is False


@pytest.mark.asyncio
async def test_saved_preview_reports_only_sanitized_iteration_error(monkeypatch):
    c=saved_connector(monkeypatch)
    class FailedHistoryTelegram(SavedTelegram):
        async def iter_messages(self,entity,limit,offset_id):
            if False: yield None
            raise RuntimeError('synthetic private message content')
    c.telegram=FailedHistoryTelegram()

    with pytest.raises(RuntimeError,match=r'history iteration failed \(RuntimeError\)') as caught:
        await c.preview_saved(1,1433603)

    assert 'synthetic private message content' not in str(caught.value)


@pytest.mark.asyncio
async def test_saved_preview_reports_missing_id_field_without_content(monkeypatch):
    c=saved_connector(monkeypatch)
    class MissingIdTelegram(SavedTelegram):
        async def iter_messages(self,entity,limit,offset_id):
            yield SimpleNamespace(raw_text='synthetic private message content')
    c.telegram=MissingIdTelegram()

    with pytest.raises(RuntimeError,match='parser rejected field=id') as caught:
        await c.preview_saved(1,1433603)

    assert 'synthetic private message content' not in str(caught.value)


@pytest.mark.asyncio
async def test_saved_preview_cursor_enumerates_multiple_pages_once(monkeypatch):
    from datetime import datetime,timezone

    c=saved_connector(monkeypatch)
    messages=[SimpleNamespace(id=mid,fwd_from=None,date=datetime(2026,1,1,tzinfo=timezone.utc),
                              raw_text='',media=None) for mid in range(200,93,-1)]
    class PagedTelegram(SavedTelegram):
        async def iter_messages(self,entity,limit,offset_id):
            available=[m for m in messages if not offset_id or m.id<offset_id]
            for m in available[:limit]:
                yield m
    c.telegram=PagedTelegram()

    seen=[];before=0;page_count=0
    while True:
        page=await c.preview_saved(50,before)
        page_count+=1
        rows=page['groups'][0]['messages'] if page['groups'] else []
        seen.extend(row['saved_message_id'] for row in rows)
        assert page['has_more']==(page['items_scanned']==50)
        if not page['has_more']:
            break
        before=page['next_before_message_id']

    assert page_count==3
    assert seen==list(range(200,93,-1))
    assert len(set(seen))==len(messages)

def test_app_constructs(tmp_path,monkeypatch):
    (tmp_path/'private').mkdir()
    (tmp_path/'private/pairing_key').write_text('test-fixture-not-a-real-credential')
    monkeypatch.setenv('DOT_BASE_URL','https://telegram.example.com')
    monkeypatch.setenv('DOT_OWNER_ID','123')
    c=Connector(tmp_path,client=object())
    assert any(getattr(r,'path',None)=='/mcp' for r in c.app().routes)

@pytest.mark.asyncio
async def test_custom_callback_exact(tmp_path,monkeypatch):
    from telegram_dot.oauth import OwnerOAuth
    from mcp.shared.auth import OAuthClientInformationFull
    from mcp.server.auth.provider import RegistrationError
    key=tmp_path/'pairing';key.write_text('test-fixture-not-a-real-credential')
    oauth=OwnerOAuth(Store(tmp_path/'store.db'),'https://telegram.example.com',key,123)
    monkeypatch.setenv('DOT_OAUTH_REDIRECT_URIS','https://client.example.com/callback')
    good=OAuthClientInformationFull(client_id='test',redirect_uris=['https://client.example.com/callback'])
    await oauth.register_client(good)
    bad=OAuthClientInformationFull(client_id='other',redirect_uris=['https://client.example.com/other'])
    with pytest.raises(RegistrationError): await oauth.register_client(bad)

def test_queue_lease_and_ack(tmp_path):
    q=EventQueue(tmp_path/'queue.db')
    q.append([{'dedupe':'one','type':'message.created'}])
    batch=q.read('consumer','worker',0)
    assert q.read('consumer','worker')['batch_id']==batch['batch_id']
    with pytest.raises(QueueError): q.read('consumer','another-worker')
    with pytest.raises(QueueError): q.ack('consumer',batch['batch_id'],'wrong')
    q.ack('consumer',batch['batch_id'],batch['receipt'])
    assert q.ack('consumer',batch['batch_id'],batch['receipt'])['already_acknowledged']
    assert q.read('consumer','worker')['events']==[]

@pytest.mark.asyncio
async def test_setup_short_password_never_connects(tmp_path,monkeypatch):
    from telegram_dot import setup
    monkeypatch.setenv('DOT_ROOT',str(tmp_path))
    monkeypatch.setattr('builtins.input',lambda _: '12345')
    answers=iter(['fake-api-hash','short'])
    monkeypatch.setattr(setup.getpass,'getpass',lambda _: next(answers))
    monkeypatch.setattr(setup,'TelegramClient',lambda *args: pytest.fail('Must not construct client'))
    with pytest.raises(SystemExit): await setup.setup()
    assert not (tmp_path/'telegram-session/work.session').exists()

@pytest.mark.asyncio
async def test_setup_never_overwrites_session(tmp_path,monkeypatch):
    from telegram_dot import setup
    monkeypatch.setenv('DOT_ROOT',str(tmp_path))
    folder=tmp_path/'telegram-session';folder.mkdir()
    (folder/'work.session').write_text('fake fixture')
    with pytest.raises(SystemExit): await setup.setup()
    assert (folder/'work.session').read_text()=='fake fixture'

@pytest.mark.asyncio
async def test_setup_expands_home_before_session_check(tmp_path,monkeypatch):
    from telegram_dot import setup
    monkeypatch.setenv('HOME',str(tmp_path))
    monkeypatch.setenv('DOT_ROOT','~/private-runtime')
    folder=tmp_path/'private-runtime/telegram-session';folder.mkdir(parents=True)
    (folder/'work.session').write_text('fake fixture')
    with pytest.raises(SystemExit,match='Session already exists'): await setup.setup()
