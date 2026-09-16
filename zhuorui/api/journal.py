"""Durable command claims: never retry a possibly submitted order after a crash."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .errors import ApiError
from .signing import canonical


class CommandJournal:
    def __init__(self, path, binding):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS commands (
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
            reference TEXT, cancel_due REAL, cancel_state TEXT, message TEXT);
        """)
        encoded = canonical(binding).decode()
        existing = self.db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()
        if existing and existing[0] != encoded:
            self.db.close()
            raise ApiError("Command journal belongs to another configured account; use a separate journal file.")
        if "execution" not in {row[1] for row in self.db.execute("PRAGMA table_info(commands)")}:
            self.db.execute("ALTER TABLE commands ADD COLUMN execution TEXT")
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('binding',?)", (encoded,))
        self.db.commit()

    def claim(self, command_id, payload, *, semantic_digest=None):
        encoded = canonical(payload).decode()
        digest = semantic_digest or hashlib.sha256(encoded.encode()).hexdigest()
        now = time.time()
        with self.db:
            old = self.db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise ApiError("Command ID was reused with different order details; refusing it.")
                return False
            self.db.execute("INSERT INTO commands(id,digest,payload,state,created,updated) VALUES (?,?,?,'received',?,?)",
                            (command_id, digest, encoded, now, now))
        return True

    def update(self, command_id, state, *, reference=None, cancel_due=None, cancel_state=None, message=None, execution=None):
        with self.db:
            self.db.execute("""UPDATE commands SET state=?, updated=?, reference=COALESCE(?,reference),
                cancel_due=COALESCE(?,cancel_due), cancel_state=COALESCE(?,cancel_state), message=?,
                execution=COALESCE(?,execution) WHERE id=?""",
                (state, time.time(), reference, cancel_due, cancel_state, message,
                 canonical(execution).decode() if execution is not None else None, command_id))

    def get(self, command_id):
        row = self.db.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        return dict(row) if row else None

    def unresolved(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM commands WHERE state IN ('dispatching','unknown')")]

    def pending_cancellations(self):
        return [dict(row) for row in self.db.execute("""SELECT * FROM commands WHERE state='submitted'
            AND cancel_due IS NOT NULL AND (cancel_state IS NULL OR cancel_state IN ('pending','dispatching','unknown'))""")]

    def close(self):
        self.db.close()


class InstanceLock:
    """An OS file lock, released even after process termination."""
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ApiError("Another API listener already owns this command journal.") from None
        self.handle = handle
        return self

    def __exit__(self, *args):
        self.handle.close()
