"""Owner-enabled, cursor-only delivery to an Orca webhook automation."""
import asyncio
import ipaddress
import json
import os
import socket
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from .event_queue import (
    DEFAULT_READ_LIMIT,
    ORCA_CONSUMER_ID,
    ORCA_WORKER_ID,
    QueueError,
    canonical,
)


# The generated webhook URL is the bearer secret and is the exact destination
# allowlist. Never accept a host from a queue signal or request payload.
MAX_RESPONSE_BYTES = 4096
MAX_URL_LENGTH = 2048


def _validate_url(url):
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        raise ValueError('INVALID_ORCA_WEBHOOK_URL')
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme != 'https' or parsed.port not in (None, 443) or
            parsed.username or parsed.password or parsed.fragment or
            not host or not parsed.path.startswith('/') or
            any(character.isspace() for character in url)):
        raise ValueError('ORCA_WEBHOOK_DESTINATION_NOT_ALLOWED')
    return urlunsplit(('https', host, parsed.path or '/', parsed.query, ''))


async def pinned_post(url, body, headers):
    """POST to the exact configured Orca URL without redirects or proxy env vars."""
    normalized = _validate_url(url)
    parsed = urlsplit(normalized)
    host = parsed.hostname
    addresses = await asyncio.get_running_loop().getaddrinfo(
        host, 443, type=socket.SOCK_STREAM)
    ips = {entry[4][0] for entry in addresses}
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError('ORCA_WEBHOOK_NON_PUBLIC_ADDRESS')
    ip = sorted(ips)[0]
    authority = '[' + ip + ']' if ':' in ip else ip
    pinned = urlunsplit(('https', authority, parsed.path or '/', parsed.query, ''))
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        async with client.stream(
                'POST', pinned, content=body,
                headers={**headers, 'Host': host},
                extensions={'sni_hostname': host}) as response:
            result = b''
            async for chunk in response.aiter_bytes():
                result += chunk
                if len(result) > MAX_RESPONSE_BYTES:
                    raise ValueError('ORCA_WEBHOOK_RESPONSE_TOO_LARGE')
            return response.status_code, result


class OrcaWebhook:
    """Durable, opt-in queue signal delivery; disabled unless fully configured."""

    def __init__(self, queue, post=pinned_post):
        self.queue = queue
        self.post = post
        self.url = os.environ.get('DOT_ORCA_WEBHOOK_URL', '')
        configured = bool(self.url)
        self.enabled = configured
        self.configured = False
        self.error = None
        self._lock = asyncio.Lock()
        if not configured:
            return
        self.url = _validate_url(self.url)
        start = os.environ.get('DOT_ORCA_WEBHOOK_START_CURSOR')
        if start is None or not start.isdigit():
            raise ValueError('ORCA_WEBHOOK_START_CURSOR_REQUIRED')
        self.start_cursor = int(start)
        if self.start_cursor < 0:
            raise ValueError('ORCA_WEBHOOK_START_CURSOR_INVALID')
        with self.queue.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS orca_notifications(
                id INTEGER PRIMARY KEY CHECK(id=1), initial_cursor INTEGER NOT NULL,
                cursor INTEGER NOT NULL, pending TEXT, batch_id TEXT, receipt TEXT,
                batch_after INTEGER, batch_through INTEGER,
                attempt INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL,
                last_delivery REAL, last_error TEXT)''')
            columns={row['name'] for row in db.execute('PRAGMA table_info(orca_notifications)')}
            for name,definition in (
                    ('initial_cursor','INTEGER'),('batch_id','TEXT'),('receipt','TEXT'),
                    ('batch_after','INTEGER'),('batch_through','INTEGER')):
                if name not in columns:
                    db.execute(f'ALTER TABLE orca_notifications ADD COLUMN {name} {definition}')
            row = db.execute('SELECT * FROM orca_notifications WHERE id=1').fetchone()
            if not row:
                stats = self.queue.stats()
                floor = stats['earliest_after_sequence']
                high = stats['latest_sequence'] or floor
                if not floor <= self.start_cursor <= high:
                    raise ValueError('ORCA_WEBHOOK_START_CURSOR_OUT_OF_BOUNDS')
                db.execute('''INSERT INTO orca_notifications
                    (id,initial_cursor,cursor,pending,batch_id,receipt,batch_after,batch_through,attempt,due)
                    VALUES(1,?,?,NULL,NULL,NULL,NULL,NULL,0,?)''',
                           (self.start_cursor, self.start_cursor, time.time()))
            else:
                initial=row['initial_cursor'] if row['initial_cursor'] is not None else row['cursor']
                if initial != self.start_cursor:
                    raise ValueError('ORCA_WEBHOOK_START_CURSOR_IS_INITIALIZATION_ONLY')
                if row['initial_cursor'] is None:
                    db.execute('UPDATE orca_notifications SET initial_cursor=? WHERE id=1',(initial,))
        self.configured = True

    def status(self):
        if not self.configured:
            return {'enabled': False, 'configured': False}
        with self.queue.db() as db:
            row = db.execute('SELECT * FROM orca_notifications WHERE id=1').fetchone()
        return {
            'enabled': self.enabled,
            'configured': True,
            'destination_host': urlsplit(self.url).hostname,
            'initial_cursor': row['initial_cursor'],
            'cursor': row['cursor'],
            'pending': row['pending'] is not None,
            'batch_id': row['batch_id'],
            'batch_after': row['batch_after'],
            'batch_through': row['batch_through'],
            'attempt': row['attempt'],
            'last_delivery': row['last_delivery'],
            'last_error': row['last_error'],
        }

    async def tick(self):
        if not self.configured:
            return
        async with self._lock:
            stats = self.queue.stats()
            floor = stats['earliest_after_sequence']
            with self.queue.db() as db:
                row = db.execute('SELECT * FROM orca_notifications WHERE id=1').fetchone()
            if row['pending'] and not row['batch_id']:
                with self.queue.db() as db:
                    db.execute('UPDATE orca_notifications SET pending=NULL,due=? WHERE id=1',(time.time(),))
                row=dict(row)
                row.update(pending=None,due=time.time())
            if row['pending'] and row['batch_id'] and row['receipt']:
                try:
                    batch=self.queue.batch_status(ORCA_CONSUMER_ID,row['batch_id'],row['receipt'])
                except QueueError:
                    batch={'status':'expired'}
                if batch['status']=='acked':
                    if (batch['after_sequence']!=row['batch_after'] or
                            batch['through_sequence']!=row['batch_through']):
                        raise QueueError('ORCA_BATCH_STATE_CONFLICT')
                    with self.queue.db() as db:
                        db.execute('''UPDATE orca_notifications SET cursor=?,pending=NULL,batch_id=NULL,
                            receipt=NULL,batch_after=NULL,batch_through=NULL,attempt=0,due=?,last_error=NULL WHERE id=1''',
                                   (batch['through_sequence'],time.time()))
                    return
                if batch['status']=='expired':
                    with self.queue.db() as db:
                        db.execute('''UPDATE orca_notifications SET pending=NULL,batch_id=NULL,receipt=NULL,
                            batch_after=NULL,batch_through=NULL,due=? WHERE id=1''',(time.time(),))
                    row=dict(row)
                    row.update(pending=None,batch_id=None,receipt=None,batch_after=None,batch_through=None,due=time.time())
            if row['pending'] and row['batch_id']:
                if row['due'] > time.time():
                    return
                payload=json.loads(row['pending'])
                batch_id=row['batch_id']
                receipt=row['receipt']
            else:
                high = stats['latest_sequence'] or floor
                if high <= row['cursor']:
                    return
                try:
                    batch=self.queue.read(
                        ORCA_CONSUMER_ID, ORCA_WORKER_ID, row['cursor'], DEFAULT_READ_LIMIT)
                except QueueError as exc:
                    if exc.code!='CURSOR_EXPIRED' or row['cursor']>=floor:
                        raise
                    with self.queue.db() as db:
                        db.execute('UPDATE consumers SET cursor=? WHERE id=?',(floor,ORCA_CONSUMER_ID))
                    batch=self.queue.read(
                        ORCA_CONSUMER_ID, ORCA_WORKER_ID, floor, DEFAULT_READ_LIMIT)
                if not batch.get('batch_id'):
                    return
                payload = {
                    'after_sequence': batch['after_sequence'],
                    'through_sequence': batch['through_sequence'],
                    'earliest_after_sequence': floor,
                    'truncated': row['cursor'] < floor,
                }
                batch_id=batch['batch_id']
                receipt=batch['receipt']
                with self.queue.db() as db:
                    db.execute('''UPDATE orca_notifications SET pending=?,batch_id=?,receipt=?,
                        batch_after=?,batch_through=?,attempt=0,due=? WHERE id=1''',
                               (canonical(payload),batch_id,receipt,payload['after_sequence'],
                                payload['through_sequence'],time.time()))
            body = canonical(payload).encode('utf-8')
            try:
                code, _ = await self.post(self.url, body, {'Content-Type': 'application/json'})
                error = None if 200 <= code < 300 else 'HTTP_' + str(code)
            except Exception as exc:
                code = 0
                error = type(exc).__name__
            with self.queue.db() as db:
                if 200 <= code < 300:
                    db.execute('''UPDATE orca_notifications
                        SET attempt=0,due=?,last_delivery=?,last_error=NULL WHERE id=1''',
                               (time.time() + 45, time.time()))
                else:
                    attempt = min(8,row['attempt'] + 1)
                    due = time.time() + min(3600, 15 * 2 ** attempt)
                    db.execute('UPDATE orca_notifications SET attempt=?,due=?,last_error=? WHERE id=1',
                               (attempt, due, error))
