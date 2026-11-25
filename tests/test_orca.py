import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from telegram_dot.event_queue import (
    ORCA_CONSUMER_ID,
    ORCA_WORKER_ID,
    EventQueue,
    QueueError,
)
from telegram_dot.orca import OrcaWebhook
from telegram_dot.store import Store


def configure(monkeypatch, start='0'):
    monkeypatch.setenv('DOT_ORCA_WEBHOOK_URL', 'https://hooks.example.test/automation/secret')
    monkeypatch.setenv('DOT_ORCA_WEBHOOK_START_CURSOR', start)


def append_message(queue, dedupe='one', chat_id='-100', message_id=7):
    queue.append([{
        'dedupe': dedupe,
        'type': 'message.created',
        'chat_id': chat_id,
        'message_id': message_id,
        'text': 'fixture',
    }])


def test_initial_seed_must_be_in_current_bounds(tmp_path, monkeypatch):
    queue = EventQueue(tmp_path / 'events.db')
    append_message(queue)
    configure(monkeypatch, '2')
    with pytest.raises(ValueError, match='OUT_OF_BOUNDS'):
        OrcaWebhook(queue)


def test_existing_seed_survives_retention_prune(tmp_path, monkeypatch):
    queue = EventQueue(tmp_path / 'events.db')
    append_message(queue)
    configure(monkeypatch, '0')
    OrcaWebhook(queue)
    with queue.db() as db:
        db.execute('UPDATE events SET received=?', (time.time() - 31 * 86400,))
    queue.prune()
    append_message(queue, dedupe='two', message_id=8)
    # The persisted seed is intentionally outside the current retention window.
    restarted = OrcaWebhook(queue)
    assert restarted.status()['initial_cursor'] == 0
    assert restarted.status()['cursor'] == 0


def test_seed_change_after_state_exists_is_rejected(tmp_path, monkeypatch):
    queue = EventQueue(tmp_path / 'events.db')
    append_message(queue)
    configure(monkeypatch, '0')
    OrcaWebhook(queue)
    monkeypatch.setenv('DOT_ORCA_WEBHOOK_START_CURSOR', '1')
    with pytest.raises(ValueError, match='INITIALIZATION_ONLY'):
        OrcaWebhook(queue)


@pytest.mark.asyncio
async def test_retry_waits_for_exact_ack_and_duplicate_is_safe(tmp_path, monkeypatch):
    queue = EventQueue(tmp_path / 'events.db')
    append_message(queue)
    configure(monkeypatch)
    calls = []
    responses = iter([(500, b''), (202, b'')])

    async def post(url, body, headers):
        calls.append((url, json.loads(body)))
        return next(responses)

    notifier = OrcaWebhook(queue, post=post)
    await notifier.tick()
    assert notifier.status()['cursor'] == 0
    assert notifier.status()['pending'] is True
    with queue.db() as db:
        db.execute('UPDATE orca_notifications SET due=0')
    await notifier.tick()
    assert notifier.status()['cursor'] == 0
    assert len(calls) == 2
    with queue.db() as db:
        row = db.execute('SELECT batch_id,receipt FROM orca_notifications').fetchone()
    queue.ack(ORCA_CONSUMER_ID, row['batch_id'], row['receipt'])
    await notifier.tick()
    assert notifier.status()['cursor'] == 1
    assert notifier.status()['pending'] is False
    await notifier.tick()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_orca_lease_fits_default_read_events_page(tmp_path, monkeypatch):
    queue = EventQueue(tmp_path / 'events.db')
    for message_id in range(35):
        append_message(queue, dedupe=f'message-{message_id}', message_id=message_id + 1)
    configure(monkeypatch)

    async def post(url, body, headers):
        return 202, b''

    notifier = OrcaWebhook(queue, post=post)
    await notifier.tick()
    batch = queue.read(ORCA_CONSUMER_ID, ORCA_WORKER_ID, after_sequence=0)

    assert len(batch['events']) == 30
    assert batch['through_sequence'] == 30
    with queue.db() as db:
        row = db.execute('SELECT batch_id,receipt FROM orca_notifications').fetchone()
    queue.ack(ORCA_CONSUMER_ID, row['batch_id'], row['receipt'])
    await notifier.tick()
    assert notifier.status()['cursor'] == 30


@pytest.mark.parametrize('limit', [31, 100])
def test_read_events_rejects_caller_limits_over_hard_max(tmp_path, limit):
    queue = EventQueue(tmp_path / 'events.db')
    append_message(queue)

    with pytest.raises(QueueError, match='INVALID_LIMIT') as error:
        queue.read(ORCA_CONSUMER_ID, ORCA_WORKER_ID, after_sequence=0, limit=limit)

    assert error.value.code == 'INVALID_LIMIT'
    with queue.db() as db:
        assert db.execute('SELECT count(*) FROM consumers').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM batches').fetchone()[0] == 0


def test_oversized_legacy_lease_rejects_replay_until_expired(tmp_path):
    queue = EventQueue(tmp_path / 'events.db')
    for message_id in range(35):
        append_message(queue, dedupe=f'legacy-{message_id}', message_id=message_id + 1)
    old_batch_id = 'legacy-oversized-batch'
    old_receipt = 'legacy-oversized-receipt'
    now = time.time()
    with queue.db() as db:
        db.execute('INSERT INTO consumers VALUES(?,?)', (ORCA_CONSUMER_ID, 0))
        db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?,?)',
                   (old_batch_id, ORCA_CONSUMER_ID, ORCA_WORKER_ID, 0, 35,
                    old_receipt, now + 300, 0))

    with pytest.raises(QueueError, match='LEASE_EXCEEDS_LIMIT') as error:
        queue.read(ORCA_CONSUMER_ID, ORCA_WORKER_ID, after_sequence=0, limit=30)
    assert error.value.code == 'LEASE_EXCEEDS_LIMIT'
    with queue.db() as db:
        assert db.execute('SELECT cursor FROM consumers WHERE id=?',
                          (ORCA_CONSUMER_ID,)).fetchone()[0] == 0
        old = db.execute('SELECT after_seq,through_seq,receipt,acked FROM batches WHERE id=?',
                         (old_batch_id,)).fetchone()
        assert tuple(old) == (0, 35, old_receipt, 0)

    with queue.db() as db:
        db.execute('UPDATE batches SET expires=? WHERE id=?', (time.time() - 1, old_batch_id))
    with pytest.raises(QueueError, match='BATCH_EXPIRED'):
        queue.ack(ORCA_CONSUMER_ID, old_batch_id, old_receipt)

    batch = queue.read(ORCA_CONSUMER_ID, ORCA_WORKER_ID, after_sequence=0, limit=30)
    assert len(batch['events']) == 30
    assert batch['through_sequence'] == 30
    assert queue.ack(ORCA_CONSUMER_ID, batch['batch_id'], batch['receipt'])['through_sequence'] == 30
    with queue.db() as db:
        assert db.execute('SELECT cursor FROM consumers WHERE id=?',
                          (ORCA_CONSUMER_ID,)).fetchone()[0] == 30


@pytest.mark.asyncio
async def test_orca_provenance_requires_active_batch_event(tmp_path, monkeypatch):
    queue = EventQueue(tmp_path / 'events.db')
    append_message(queue)
    configure(monkeypatch)

    async def post(url, body, headers):
        return 202, b''

    notifier = OrcaWebhook(queue, post=post)
    await notifier.tick()
    with queue.db() as db:
        row = db.execute('SELECT batch_id,receipt,batch_after,batch_through FROM orca_notifications').fetchone()
    assert queue.validate_orca_event('-100', 7, row['batch_after'], row['batch_through'])['batch_id'] == row['batch_id']
    with pytest.raises(QueueError, match='EVENT_NOT_IN_BATCH'):
        queue.validate_orca_event('-200', 7, row['batch_after'], row['batch_through'])
    with pytest.raises(QueueError, match='BATCH_NOT_ACTIVE'):
        queue.validate_orca_event('-100', 7, row['batch_after'], row['batch_through'] + 1)
    queue.ack(ORCA_CONSUMER_ID, row['batch_id'], row['receipt'])
    with pytest.raises(QueueError, match='BATCH_NOT_ACTIVE'):
        queue.validate_orca_event('-100', 7, row['batch_after'], row['batch_through'])


@pytest.mark.asyncio
async def test_saved_draft_requires_active_orca_lease_and_cannot_send(tmp_path, monkeypatch):
    pytest.importorskip('mcp.server.mcpserver')
    from telegram_dot.server import Connector

    queue = EventQueue(tmp_path / 'events.db')
    append_message(queue)
    configure(monkeypatch)

    async def post(url, body, headers):
        return 202, b''

    notifier = OrcaWebhook(queue, post=post)
    await notifier.tick()
    with queue.db() as db:
        lease = db.execute('SELECT batch_after,batch_through FROM orca_notifications').fetchone()

    connector = Connector.__new__(Connector)
    connector.queue = queue
    connector.store = Store(tmp_path / 'connector.db')
    connector.lock = asyncio.Lock()

    async def resolve(chat):
        return SimpleNamespace(id=int(chat))

    connector.resolve = resolve
    monkeypatch.setattr('telegram_dot.server.utils.get_peer_id', lambda entity: entity.id)
    monkeypatch.setattr('telegram_dot.server.utils.get_display_name', lambda entity: 'Draft target')
    draft = await connector.save_draft(
        '456', 'Draft', '-100', 7, None, lease['batch_after'], lease['batch_through'])
    duplicate = await connector.save_draft(
        '456', 'Draft', '-100', 7, None, lease['batch_after'], lease['batch_through'])
    assert duplicate['id'] == draft['id']

    monkeypatch.setenv('DOT_SEND_ENABLED', 'true')
    with pytest.raises(PermissionError, match='cannot be sent'):
        await connector.dispatch(draft['id'], '456', 'Draft')

    with queue.db() as db:
        row = db.execute('SELECT batch_id,receipt FROM orca_notifications').fetchone()
    queue.ack(ORCA_CONSUMER_ID, row['batch_id'], row['receipt'])
    with pytest.raises(QueueError, match='DRAFT_SOURCE_BATCH_NOT_ACTIVE'):
        queue.validate_orca_event('-100', 7, lease['batch_after'], lease['batch_through'])
