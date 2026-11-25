import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager


class Store:
    def __init__(self,path):
        self.path=path
        path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''CREATE TABLE IF NOT EXISTS requests(
                id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, recipient TEXT NOT NULL,
                title TEXT NOT NULL, text TEXT NOT NULL, reply_to INTEGER, digest TEXT NOT NULL,
                expires REAL NOT NULL, status TEXT NOT NULL, result_id INTEGER, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS saved_reply_drafts(
                id TEXT PRIMARY KEY, draft_key TEXT UNIQUE NOT NULL, recipient TEXT NOT NULL,
                title TEXT NOT NULL, text TEXT NOT NULL, reply_to INTEGER, digest TEXT NOT NULL,
                source_chat_id TEXT NOT NULL, source_message_id INTEGER NOT NULL,
                source_after_sequence INTEGER, source_through_sequence INTEGER,
                status TEXT NOT NULL DEFAULT 'saved', created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(source_chat_id,source_message_id));
                UPDATE requests SET status='uncertain' WHERE status='dispatching';''')
            db.execute('DELETE FROM saved_reply_drafts WHERE created < ?', (time.time() - 30 * 86400,))
        path.chmod(0o600)

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=30)
        db.row_factory=sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def save_reply_draft(self, source_chat_id, source_message_id, recipient, title, text, reply_to=None,
                         source_after_sequence=None, source_through_sequence=None):
        """Persist a local-only draft; this table is intentionally separate from send requests."""
        now = time.time()
        draft_key = f'telegram:{source_chat_id}:{source_message_id}'
        digest = hashlib.sha256(
            json.dumps(
                [str(source_chat_id), source_message_id, str(recipient), text, reply_to,
                 source_after_sequence, source_through_sequence],
                ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM saved_reply_drafts WHERE source_chat_id=? AND source_message_id=?',
                             (source_chat_id, source_message_id)).fetchone()
            if row and row['digest'] != digest:
                raise ValueError('source chat/message already used for a different draft')
            if not row:
                draft_id = 'draft_' + secrets.token_hex(24)
                db.execute('''INSERT INTO saved_reply_drafts
                    (id,draft_key,recipient,title,text,reply_to,digest,source_chat_id,source_message_id,
                     source_after_sequence,source_through_sequence,status,created,updated)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (draft_id, draft_key, str(recipient), title, text, reply_to, digest,
                     source_chat_id, source_message_id, source_after_sequence, source_through_sequence,
                     'saved', now, now))
            else:
                draft_id = row['id']
                db.execute('UPDATE saved_reply_drafts SET updated=? WHERE id=?', (now, draft_id))
            saved = db.execute('SELECT * FROM saved_reply_drafts WHERE id=?', (draft_id,)).fetchone()
        return dict(saved)

    def prune_saved_reply_drafts(self):
        with self.connection() as db:
            db.execute('DELETE FROM saved_reply_drafts WHERE created < ?', (time.time() - 30 * 86400,))

    def get_saved_reply_draft(self, draft_id):
        with self.connection() as db:
            row = db.execute('''SELECT * FROM saved_reply_drafts
                WHERE id=? AND created>=?''', (draft_id, time.time() - 30 * 86400)).fetchone()
        if not row:
            raise ValueError('Unknown saved reply draft')
        return dict(row)

    def list_saved_reply_drafts(self, limit=50):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        with self.connection() as db:
            rows = db.execute('''SELECT * FROM saved_reply_drafts
                WHERE created>=? ORDER BY created DESC LIMIT ?''',
                (time.time() - 30 * 86400, limit)).fetchall()
        return [dict(row) for row in rows]
