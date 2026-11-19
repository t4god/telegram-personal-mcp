import sqlite3
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
                UPDATE requests SET status='uncertain' WHERE status='dispatching';''')
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
