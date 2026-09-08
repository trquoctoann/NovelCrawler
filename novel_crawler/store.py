from contextlib import contextmanager
from pathlib import Path
import sqlite3


SCHEMA = '''
CREATE TABLE IF NOT EXISTS books (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, expected INTEGER NOT NULL,
 source_url TEXT NOT NULL, glossary_hash TEXT, toc_ready INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chapters (
 book_id TEXT NOT NULL REFERENCES books(id), number INTEGER NOT NULL,
 url TEXT NOT NULL, title TEXT NOT NULL, source TEXT, source_hash TEXT,
 translated TEXT, edited TEXT, summary TEXT,
 state TEXT NOT NULL DEFAULT 'discovered', error TEXT,
 PRIMARY KEY(book_id,number), UNIQUE(book_id,url)
);
CREATE TABLE IF NOT EXISTS calls (
 id INTEGER PRIMARY KEY, book_id TEXT NOT NULL, chapter INTEGER NOT NULL,
 stage TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
 created REAL NOT NULL, day TEXT NOT NULL, reserved INTEGER NOT NULL,
 usage INTEGER, status TEXT NOT NULL DEFAULT 'reserved', response TEXT,
 prompt_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule (
 book_id TEXT NOT NULL, job TEXT NOT NULL, next_due REAL NOT NULL,
 PRIMARY KEY(book_id,job)
);
CREATE TABLE IF NOT EXISTS delivery (
 book_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, state TEXT NOT NULL,
 message_id TEXT NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS source_scans (
 book_id TEXT NOT NULL, source_id TEXT NOT NULL, signature TEXT NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL, retry_at REAL NOT NULL,
 error TEXT, mapped INTEGER NOT NULL, PRIMARY KEY(book_id,source_id)
);
CREATE TABLE IF NOT EXISTS source_candidates (
 id INTEGER PRIMARY KEY, book_id TEXT NOT NULL, source_id TEXT NOT NULL,
 url TEXT NOT NULL, source_number INTEGER NOT NULL, title TEXT NOT NULL,
 canonical_number INTEGER, proof TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, error TEXT,
 UNIQUE(book_id,source_id,url)
);
CREATE INDEX IF NOT EXISTS candidates_chapter ON source_candidates(book_id,canonical_number);
CREATE TABLE IF NOT EXISTS source_provenance (
 book_id TEXT NOT NULL, number INTEGER NOT NULL, source_id TEXT NOT NULL,
 url TEXT NOT NULL, source_number INTEGER NOT NULL, title TEXT NOT NULL,
 proof TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(book_id,number)
);
CREATE TABLE IF NOT EXISTS source_reviews (
 book_id TEXT NOT NULL, number INTEGER NOT NULL, reason TEXT NOT NULL,
 PRIMARY KEY(book_id,number)
);
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def register(self, book):
        old = self.db.execute('SELECT * FROM books WHERE id=?', (book['id'],)).fetchone()
        if old and (old['expected'] != book['expected_chapters'] or old['source_url'] != book['source_url']):
            raise ValueError('Changing source or chapter count requires a new book ID (avoid mixing editions)')
        self.db.execute('INSERT OR IGNORE INTO books(id,title,expected,source_url) VALUES(?,?,?,?)',
                        (book['id'], book['title'], book['expected_chapters'], book['source_url']))

    def close(self):
        self.db.close()


@contextmanager
def pipeline_lock(database):
    """OS lock released on process exit; serialize all workers sharing this DB."""
    path = Path(str(database) + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as f:
        f.seek(0)
        if not f.read(1):
            f.write(b'0')
            f.flush()
        f.seek(0)
        try:
            import msvcrt
        except ImportError:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        else:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
