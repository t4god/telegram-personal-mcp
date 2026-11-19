"""Bounded local inbox of new Telegram messages; never sends notifications."""
import json
import sqlite3
import time
from contextlib import contextmanager


class MessageInbox:
    def __init__(self,path,retention_days=30,max_messages=100000):
        self.path=path
        self.retention_days=retention_days
        self.max_messages=max_messages
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS messages(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,message_id INTEGER NOT NULL,
                received REAL NOT NULL,payload TEXT NOT NULL,
                UNIQUE(chat_id,message_id))''')
        path.chmod(0o600)
        self.prune()

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        db.row_factory=sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def prune(self,db=None):
        if db is None:
            with self.connect() as connection:
                self.prune(connection)
            return
        db.execute('DELETE FROM messages WHERE received < ?',
                   (time.time()-self.retention_days*86400,))
        db.execute('''DELETE FROM messages WHERE sequence <= COALESCE(
            (SELECT sequence FROM messages ORDER BY sequence DESC LIMIT 1 OFFSET ?),-1)''',
            (self.max_messages,))

    def record(self,payload):
        with self.connect() as db:
            result=db.execute('INSERT OR IGNORE INTO messages(chat_id,message_id,received,payload) VALUES(?,?,?,?)',
                (payload['chat_id'],payload['message_id'],time.time(),json.dumps(payload,ensure_ascii=False)))
            inserted=result.rowcount==1
            self.prune(db)
        return inserted

    def read(self,after_sequence=0,limit=30,chat_id=None):
        if after_sequence<0 or not 1<=limit<=100:
            raise ValueError('after_sequence must be >=0; limit must be 1..100')
        self.prune()
        with self.connect() as db:
            where='sequence > ?'
            args=[after_sequence]
            if chat_id is not None:
                where+=' AND chat_id = ?'
                args.append(chat_id)
            rows=db.execute('SELECT sequence,payload FROM messages WHERE '+where+' ORDER BY sequence LIMIT ?',args+[limit+1]).fetchall()
        return {'messages':[dict(sequence=r['sequence'],**json.loads(r['payload'])) for r in rows[:limit]],
                'next_after_sequence':rows[min(len(rows),limit)-1]['sequence'] if rows else after_sequence,
                'more_available':len(rows)>limit,
                'coverage':'New events received while connected; offline gaps and Telegram delivery limits may apply. Not a complete archive.'}

    def stats(self):
        with self.connect() as db:
            row=db.execute('SELECT count(*) AS count,max(sequence) AS last_sequence,max(received) AS last_received FROM messages').fetchone()
        return dict(row)
