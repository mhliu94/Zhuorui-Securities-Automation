"""Durable per-order snapshots and versions for the order-status topic."""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
import threading

from .errors import ApiError
from .signing import canonical


MAX_SEQUENCE = (1 << 53) - 1
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)


def _timestamp(value):
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        raise ApiError("Order publication timestamps must include a valid RFC3339 timezone.")
    try:
        return datetime.fromisoformat(value.replace("z", "+00:00").replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("Order publication timestamps must include a valid RFC3339 timezone.") from None


def _encoded(value):
    try:
        return canonical(value).decode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise ApiError("Order publication snapshot is not valid finite JSON.") from None


def _semantic_value(value):
    """Normalize JSON numbers exactly, without Decimal context rounding."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite():
            raise ApiError("Order publication snapshot is not valid finite JSON.")
        if number == 0:
            return Decimal(0)
        sign, digits, exponent = number.as_tuple()
        digits = list(digits)
        while len(digits) > 1 and digits[-1] == 0:
            digits.pop()
            exponent += 1
        return Decimal((sign, tuple(digits), exponent))
    if isinstance(value, dict):
        return {key: _semantic_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_semantic_value(item) for item in value]
    return value


class OrderPublicationJournal:
    """Persist a full snapshot before sending, then record its Kafka acknowledgment.

    A polling timestamp alone never consumes a version. Unchanged polls and
    retries return the exact previous payload, including its original timestamp.
    The database belongs to one configured session binding. A lock also allows
    the publisher to be constructed on a different thread from its worker.
    """

    def __init__(self, path, binding):
        if not isinstance(binding, dict) or not isinstance(binding.get("account_id"), str) \
                or not binding["account_id"].strip():
            raise ApiError("Configure an account identity before publishing order snapshots.")
        self.account_id = binding["account_id"]
        self._lock = threading.RLock()
        encoded_binding = _encoded(binding)
        self.db = None
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("""CREATE TABLE IF NOT EXISTS order_publication_metadata
                    (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS order_publications (
                    id TEXT PRIMARY KEY, sequence INTEGER NOT NULL,
                    semantic TEXT NOT NULL, payload TEXT NOT NULL,
                    acknowledged_sequence INTEGER NOT NULL DEFAULT 0)""")
                existing = self.db.execute(
                    "SELECT value FROM order_publication_metadata WHERE key='binding'").fetchone()
                if existing and existing[0] != encoded_binding:
                    raise ApiError("Order snapshot journal belongs to another configured account; use a separate journal file.")
                self.db.execute("INSERT OR IGNORE INTO order_publication_metadata VALUES ('binding',?)",
                                (encoded_binding,))
        except (OSError, sqlite3.Error):
            if self.db is not None:
                self.db.close()
            raise ApiError("Could not open durable order snapshot state; check the order snapshot journal.") from None
        except ApiError:
            if self.db is not None:
                self.db.close()
            raise

    def _identity(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("account_id") != self.account_id:
            raise ApiError("Order snapshot account does not match its configured publication journal.")
        order_id = snapshot.get("order_id")
        if not isinstance(order_id, str) or not order_id.strip():
            raise ApiError("Order snapshots require a stable order identity.")
        return _encoded([self.account_id, order_id])

    @staticmethod
    def _semantic(snapshot):
        return _encoded(_semantic_value({key: value for key, value in snapshot.items()
                                         if key not in ("sequence", "updated_at")}))

    def _restore(self, row, identity):
        try:
            snapshot = json.loads(row["payload"], parse_float=Decimal)
            sequence = row["sequence"]
            if not isinstance(sequence, int) or not 1 <= sequence <= MAX_SEQUENCE \
                    or snapshot.get("sequence") != sequence \
                    or self._identity(snapshot) != identity \
                    or self._semantic(snapshot) != row["semantic"]:
                raise ValueError
            _timestamp(snapshot.get("created_at"))
            _timestamp(snapshot.get("updated_at"))
            return snapshot
        except (TypeError, ValueError, AttributeError, ApiError):
            raise ApiError("Stored order snapshot state is invalid; repair the order snapshot journal before publishing.") from None

    def prepare(self, snapshot):
        """Return a persisted full snapshot with a stable per-order sequence."""
        identity = self._identity(snapshot)
        if "sequence" in snapshot:
            raise ApiError("Order snapshot sequence must be assigned by its publication journal.")
        _timestamp(snapshot.get("created_at"))
        updated_at = _timestamp(snapshot.get("updated_at"))
        semantic = self._semantic(snapshot)
        # Decode the persisted representation so callers cannot mutate input
        # references and fractional JSON values retain Decimal precision.
        prepared = json.loads(_encoded(snapshot), parse_float=Decimal)
        try:
            with self._lock, self.db:
                self.db.execute("BEGIN IMMEDIATE")
                row = self.db.execute("SELECT * FROM order_publications WHERE id=?", (identity,)).fetchone()
                sequence = 1
                if row:
                    previous = self._restore(row, identity)
                    if prepared["created_at"] != previous["created_at"]:
                        raise ApiError("Order creation timestamp changed; refusing an inconsistent order snapshot.")
                    if semantic == row["semantic"]:
                        return previous
                    if updated_at < _timestamp(previous["updated_at"]):
                        raise ApiError("Order update timestamp moved backward; refusing a stale order snapshot.")
                    if row["sequence"] == MAX_SEQUENCE:
                        raise ApiError("Order snapshot sequence limit reached; cannot publish another version.")
                    sequence = row["sequence"] + 1
                prepared["sequence"] = sequence
                payload = _encoded(prepared)
                self.db.execute("""INSERT INTO order_publications(id,sequence,semantic,payload)
                    VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    sequence=excluded.sequence, semantic=excluded.semantic, payload=excluded.payload""",
                                (identity, sequence, semantic, payload))
            return prepared
        except sqlite3.Error:
            raise ApiError("Could not persist order snapshot before publication; check the order snapshot journal.") from None

    def pending(self):
        """Return each latest unacknowledged payload, including earlier days."""
        try:
            with self._lock:
                rows = self.db.execute("""SELECT * FROM order_publications
                    WHERE acknowledged_sequence < sequence ORDER BY id""").fetchall()
                return [self._restore(row, row["id"]) for row in rows]
        except sqlite3.Error:
            raise ApiError("Could not read pending order publication state; check the order snapshot journal.") from None

    def ack(self, snapshot):
        """Acknowledge only the exact latest prepared payload; ignore stale acks."""
        identity = self._identity(snapshot)
        payload = _encoded(snapshot)
        sequence = snapshot.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or not 1 <= sequence <= MAX_SEQUENCE:
            raise ApiError("Order acknowledgment requires a valid prepared sequence.")
        try:
            with self._lock, self.db:
                self.db.execute("BEGIN IMMEDIATE")
                result = self.db.execute("""UPDATE order_publications SET acknowledged_sequence=?
                    WHERE id=? AND sequence=? AND payload=?""", (sequence, identity, sequence, payload))
                return result.rowcount == 1
        except sqlite3.Error:
            raise ApiError("Could not persist order publication acknowledgment; retry the saved snapshot.") from None

    def close(self):
        with self._lock:
            self.db.close()
