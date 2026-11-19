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
