"""Offline regression coverage added by independent release review."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from telegram_dot.server import Connector
from telegram_dot.attachments import Attachments


def test_relative_runtime_is_normalized(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    key=Path('runtime/private/pairing_key')
    key.parent.mkdir(parents=True)
    key.write_text('fixture-only-not-an-actual-secret')
    monkeypatch.setenv('DOT_BASE_URL','https://telegram.example.com')
    monkeypatch.setenv('DOT_OWNER_ID','123')
    c=Connector(Path('./runtime'),client=object())
    assert c.root == tmp_path/'runtime'
    assert (c.root/'telegram-session/work.session').as_uri().startswith('file:///')


@pytest.mark.asyncio
async def test_attachment_stream_limit_removes_partial_file(tmp_path):
    cache=Attachments(tmp_path)
    cache.max_file=4
    class Fake:
        async def download_media(self,message,file):
            file.write(b'123')
            file.write(b'45')
    msg=SimpleNamespace(file=SimpleNamespace(size=None,mime_type='text/plain',name='fixture'))
    with pytest.raises(ValueError,match='TOO_LARGE'):
        await cache.download(Fake(),msg)
    assert list(tmp_path.glob('*.bin')) == []


@pytest.mark.asyncio
async def test_attachment_chunks_roundtrip_and_handle_validation(tmp_path):
    import base64
    cache=Attachments(tmp_path)
    class Fake:
        async def download_media(self,message,file):file.write(b'hello')
    msg=SimpleNamespace(file=SimpleNamespace(size=5,mime_type='text/plain',name='fixture'))
    result=await cache.download(Fake(),msg)
    chunk=cache.chunk(result['handle'])
    assert base64.b64decode(chunk['data_base64']) == b'hello'
    assert chunk['eof'] is True
    with pytest.raises(ValueError,match='INVALID_ATTACHMENT_HANDLE'):cache.chunk('../secret')
