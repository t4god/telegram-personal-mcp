"""Durable bounded journal, source checkpoints and leased consumer batches."""
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager


ORCA_CONSUMER_ID = 'telegram-orca-home-drafts'
ORCA_WORKER_ID = 'telegram-orca-home-drafts'
MAX_READ_LIMIT = 30
DEFAULT_READ_LIMIT = MAX_READ_LIMIT


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


class QueueError(ValueError):
    def __init__(self,code,**details):
        super().__init__(canonical(dict(error=code,**details)))
        self.code=code


class EventQueue:
    def __init__(self,path,retention_days=30,max_events=100000):
        self.path=path
        self.retention_days=retention_days
        self.max_events=max_events
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        path.touch(mode=0o600,exist_ok=True)
        path.chmod(0o600)
        with self.db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id TEXT UNIQUE NOT NULL,dedupe TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,
                  chat_id TEXT,message_id INTEGER,occurred_at TEXT,received REAL NOT NULL,payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_message ON events(chat_id,message_id);
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS consumers(id TEXT PRIMARY KEY,cursor INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,consumer TEXT NOT NULL,
                  worker TEXT NOT NULL,after_seq INTEGER NOT NULL,through_seq INTEGER NOT NULL,
                  receipt TEXT NOT NULL,expires REAL NOT NULL,acked INTEGER NOT NULL DEFAULT 0);
            ''')
            db.execute("INSERT OR IGNORE INTO state VALUES('floor','0')")
        path.chmod(0o600)
        self.prune()

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def get_state(self,key,default=None,db=None):
        if db is None:
            with self.db() as c:return self.get_state(key,default,c)
        r=db.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_state(self,key,value,db=None):
        if db is None:
            with self.db() as c:return self.set_state(key,value,c)
        db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,canonical(value)))

    def prune(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT max(sequence) FROM events WHERE received < ?',
                           (time.time()-86400*self.retention_days,)).fetchone()[0] or 0
            cap=db.execute('SELECT sequence FROM events ORDER BY sequence DESC LIMIT 1 OFFSET ?',
                           (self.max_events,)).fetchone()
            floor=max(self.get_state('floor',0,db),old,cap[0] if cap else 0)
            db.execute('DELETE FROM events WHERE sequence<=?',(floor,))
            self.set_state('floor',floor,db)
            db.execute('DELETE FROM batches WHERE expires<?',(time.time()-86400*self.retention_days,))

    def append(self,events,checkpoint=None):
        """Commit all events before advancing an independent source checkpoint."""
        inserted=[]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for e in events:
                dedupe=e['dedupe']
                eid='evt_'+hashlib.sha256(dedupe.encode()).hexdigest()
                row=db.execute('''INSERT OR IGNORE INTO events(sequence,event_id,dedupe,kind,chat_id,message_id,occurred_at,received,payload)
                    VALUES(?,?,?,?,?,?,?,?,?)''',(e.get('legacy_sequence'),eid,dedupe,e['type'],e.get('chat_id'),e.get('message_id'),
                    e.get('occurred_at'),e.get('received_at',time.time()),canonical(e)))
                if row.rowcount:inserted.append(row.lastrowid)
            if checkpoint:
                for key,value in checkpoint.items():self.set_state(key,value,db)
        return inserted

    def gap(self,reason,**details):
        key='gap:'+reason+':'+hashlib.sha256(canonical(details).encode()).hexdigest()
        return self.append([dict(dedupe=key,type='gap',occurred_at=None,chat_id=None,message_id=None,
                                 sender_id=None,text=None,attachments=[],reply=None,reason=reason,details=details)])

    def _validate_cursor(self,db,cursor):
        floor=self.get_state('floor',0,db)
        high=db.execute('SELECT max(sequence) FROM events').fetchone()[0] or floor
        if not isinstance(cursor,int) or isinstance(cursor,bool) or cursor<0:
            raise QueueError('INVALID_CURSOR')
        if cursor<floor:raise QueueError('CURSOR_EXPIRED',requested=cursor,earliest_after_sequence=floor,latest_sequence=high)
        if cursor>high:raise QueueError('CURSOR_AHEAD',latest_sequence=high)

    @staticmethod
    def _event(row):
        data=json.loads(row['payload'])
        data.pop('dedupe',None)
        return dict(data,event_id=row['event_id'],sequence=row['sequence'],received_at=row['received'])

    def page(self,after_sequence=0,limit=30):
        if not 1<=limit<=100:raise QueueError('INVALID_LIMIT')
        self.prune()
        with self.db() as db:
            self._validate_cursor(db,after_sequence)
            rows=db.execute('SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?',
                            (after_sequence,limit+1)).fetchall()
        return dict(events=[self._event(r) for r in rows[:limit]],more_available=len(rows)>limit,
                    next_sequence=rows[min(len(rows),limit)-1]['sequence'] if rows else after_sequence)

    def read(self,consumer_id,worker_id,after_sequence=None,limit=DEFAULT_READ_LIMIT):
        if not all(isinstance(v,str) and 1<=len(v)<=128 for v in (consumer_id,worker_id)):
            raise QueueError('INVALID_CONSUMER_OR_WORKER')
        if not isinstance(limit,int) or isinstance(limit,bool) or not 1<=limit<=MAX_READ_LIMIT:
            raise QueueError('INVALID_LIMIT',maximum=MAX_READ_LIMIT)
        self.prune()
        now=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            c=db.execute('SELECT cursor FROM consumers WHERE id=?',(consumer_id,)).fetchone()
            if not c:
                if after_sequence is None:raise QueueError('START_CURSOR_REQUIRED',earliest_after_sequence=self.get_state('floor',0,db))
                self._validate_cursor(db,after_sequence)
                db.execute('INSERT INTO consumers VALUES(?,?)',(consumer_id,after_sequence))
                cursor=after_sequence
            else:
                cursor=c[0]
                if after_sequence is not None and after_sequence!=cursor:
                    raise QueueError('CURSOR_CONFLICT',acknowledged_sequence=cursor)
            self._validate_cursor(db,cursor)
            b=db.execute('SELECT * FROM batches WHERE consumer=? AND acked=0 AND expires>? ORDER BY expires DESC LIMIT 1',
                         (consumer_id,now)).fetchone()
            if b and b['worker']!=worker_id:raise QueueError('CONSUMER_BUSY',retry_after_seconds=max(1,int(b['expires']-now)))
            if b:
                rows=db.execute('SELECT * FROM events WHERE sequence>? AND sequence<=? ORDER BY sequence',
                                (b['after_seq'],b['through_seq'])).fetchall()
                if len(rows)>limit:
                    raise QueueError('LEASE_EXCEEDS_LIMIT',requested_limit=limit,
                                     leased_event_count=len(rows),lease_expires_at=b['expires'])
            else:
                rows=db.execute('SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?',(cursor,limit)).fetchall()
                if not rows:return dict(events=[],batch_id=None,acknowledged_sequence=cursor)
                b=dict(id=secrets.token_hex(16),consumer=consumer_id,worker=worker_id,after_seq=cursor,
                       through_seq=rows[-1]['sequence'],receipt=secrets.token_hex(24),expires=now+300,acked=0)
                db.execute('INSERT INTO batches VALUES(:id,:consumer,:worker,:after_seq,:through_seq,:receipt,:expires,:acked)',b)
            return dict(events=[self._event(r) for r in rows],batch_id=b['id'],receipt=b['receipt'],
                        lease_expires_at=b['expires'],after_sequence=cursor,through_sequence=b['through_seq'],
                        acknowledged=False,instruction='Acknowledge this exact batch only after processing; reading does not acknowledge.')

    def ack(self,consumer_id,batch_id,receipt):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            b=db.execute('SELECT * FROM batches WHERE id=? AND consumer=?',(batch_id,consumer_id)).fetchone()
            if not b or not secrets.compare_digest(b['receipt'],receipt):raise QueueError('INVALID_BATCH_RECEIPT')
            if b['acked']:return dict(acknowledged=True,through_sequence=b['through_seq'],already_acknowledged=True)
            if b['expires']<time.time():raise QueueError('BATCH_EXPIRED')
            cursor=db.execute('SELECT cursor FROM consumers WHERE id=?',(consumer_id,)).fetchone()[0]
            self._validate_cursor(db,cursor)
            if cursor!=b['after_seq']:raise QueueError('ACK_CONFLICT')
            db.execute('UPDATE consumers SET cursor=? WHERE id=?',(b['through_seq'],consumer_id))
            db.execute('UPDATE batches SET acked=1 WHERE id=?',(batch_id,))
            return dict(acknowledged=True,through_sequence=b['through_seq'])

    def batch_status(self,consumer_id,batch_id,receipt):
        """Return the state of an exact leased batch without changing it."""
        if not all(isinstance(v,str) and 1<=len(v)<=128 for v in (consumer_id,batch_id,receipt)):
            raise QueueError('INVALID_BATCH_RECEIPT')
        with self.db() as db:
            batch=db.execute('SELECT * FROM batches WHERE id=? AND consumer=?',(batch_id,consumer_id)).fetchone()
        if not batch or not secrets.compare_digest(batch['receipt'],receipt):
            raise QueueError('INVALID_BATCH_RECEIPT')
        if batch['acked']:
            return dict(status='acked',after_sequence=batch['after_seq'],through_sequence=batch['through_seq'])
        if batch['expires']<time.time():
            return dict(status='expired',after_sequence=batch['after_seq'],through_sequence=batch['through_seq'])
        return dict(status='active',after_sequence=batch['after_seq'],through_sequence=batch['through_seq'])

    def validate_orca_event(self,source_chat_id,source_message_id,after_sequence,through_sequence):
        """Validate draft provenance against Orca's active lease, not caller claims alone."""
        if (not isinstance(source_chat_id,str) or not 1<=len(source_chat_id)<=128 or
                not source_chat_id.lstrip('-').isdigit() or
                not isinstance(source_message_id,int) or isinstance(source_message_id,bool) or source_message_id<=0 or
                not isinstance(after_sequence,int) or isinstance(after_sequence,bool) or after_sequence<0 or
                not isinstance(through_sequence,int) or isinstance(through_sequence,bool) or
                through_sequence<after_sequence):
            raise QueueError('INVALID_DRAFT_SOURCE')
        with self.db() as db:
            batch=db.execute('''SELECT * FROM batches
                WHERE consumer=? AND worker=? AND after_seq=? AND through_seq=? AND acked=0 AND expires>?
                ORDER BY expires DESC LIMIT 1''',
                (ORCA_CONSUMER_ID,ORCA_WORKER_ID,after_sequence,through_sequence,time.time())).fetchone()
            if not batch or not batch['receipt']:
                raise QueueError('DRAFT_SOURCE_BATCH_NOT_ACTIVE')
            row=db.execute('''SELECT sequence FROM events
                WHERE sequence>? AND sequence<=? AND chat_id=? AND message_id=? LIMIT 1''',
                (batch['after_seq'],batch['through_seq'],source_chat_id,source_message_id)).fetchone()
        if not row:
            raise QueueError('DRAFT_SOURCE_EVENT_NOT_IN_BATCH')
        status=self.batch_status(ORCA_CONSUMER_ID,batch['id'],batch['receipt'])
        if status['status']!='active':
            raise QueueError('DRAFT_SOURCE_BATCH_NOT_ACTIVE')
        return dict(after_sequence=batch['after_seq'],through_sequence=batch['through_seq'],
                    batch_id=batch['id'])

    def previous_chat(self,message_id):
        with self.db() as db:
            rows=db.execute("SELECT DISTINCT chat_id FROM events WHERE message_id=? AND CAST(chat_id AS INTEGER)>-1000000000000 AND chat_id IS NOT NULL",(message_id,)).fetchall()
        return rows[0][0] if len(rows)==1 else None

    def stats(self):
        with self.db() as db:
            r=db.execute('SELECT count(*) AS count,min(sequence) AS earliest_sequence,max(sequence) AS latest_sequence,max(received) AS last_received FROM events').fetchone()
            return dict(r,earliest_after_sequence=self.get_state('floor',0,db),retention_days=self.retention_days,
                        max_events=self.max_events,gaps=db.execute("SELECT count(*) FROM events WHERE kind='gap'").fetchone()[0])

    def migrate_legacy(self,path):
        if self.get_state('legacy_migrated') or not path.exists():return
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as source:
            first=source.execute('SELECT min(sequence) FROM messages').fetchone()[0]
            if first and self.stats()['count']==0:self.set_state('floor',first-1)
            for seq,received,payload in source.execute('SELECT sequence,received,payload FROM messages ORDER BY sequence'):
                p=json.loads(payload)
                p.update(type='message.created',dedupe='create:'+p['chat_id']+':'+str(p['message_id']),
                         occurred_at=p.get('date_utc'),received_at=received,legacy_sequence=seq,
                         provenance='legacy_new_message_inbox',attachments=[],reply={'message_id':p.get('reply_to_message_id')})
                self.append([p])
        self.set_state('legacy_migrated',True)
        self.prune()
