"""Order snapshot versions survive retries, restarts and persistence failures."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from zhuorui.api.errors import ApiError
from zhuorui.api.order_publication import MAX_SEQUENCE, OrderPublicationJournal
from zhuorui.api.signing import canonical


BINDING = {"account_id": "ACC-DEMO", "account_num_id": 7, "server_id": "zr-demo"}


def snapshot(**updates):
    result = {"account_id": BINDING["account_id"], "order_id": "broker-order-123",
              "schema_version": 1, "symbol": "DEMO", "side": "buy", "order_type": "limit",
              "status": "open", "quantity": Decimal("2.5"), "filled_quantity": Decimal("0"),
              "limit_price": Decimal("12.123456789123456789"),
              "created_at": "2026-09-29T13:30:00+00:00",
              "updated_at": "2026-09-29T13:31:00+00:00"}
    result.update(updates)
    return result


class OrderPublicationJournalTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "order-snapshots.sqlite3"
        self.journal = OrderPublicationJournal(self.path, BINDING)
        self.addCleanup(lambda: self.journal.close())

    def restart(self):
        self.journal.close()
        self.journal = OrderPublicationJournal(self.path, BINDING)

    def saved(self):
        return self.journal.db.execute("SELECT * FROM order_publications").fetchone()

    def test_first_snapshot_is_persisted_before_send_and_input_is_unchanged(self):
        source = snapshot()
        prepared = self.journal.prepare(source)
        self.assertNotIn("sequence", source)
        self.assertEqual(prepared["sequence"], 1)
        row = self.saved()
        self.assertEqual(row["payload"].encode("utf-8"), canonical(prepared))
        self.assertEqual(row["acknowledged_sequence"], 0)
        self.assertEqual(json.loads(row["id"]), [BINDING["account_id"], source["order_id"]])

    def test_unchanged_poll_after_restart_reuses_exact_acked_payload(self):
        prepared = self.journal.prepare(snapshot())
        self.assertTrue(self.journal.ack(prepared))
        self.restart()
        repeated = self.journal.prepare(snapshot(updated_at="2026-09-29T14:00:00+00:00"))
        self.assertEqual(canonical(repeated), canonical(prepared))
        self.assertEqual(repeated["limit_price"], Decimal("12.123456789123456789"))
        self.assertEqual(self.saved()["acknowledged_sequence"], 1)

    def test_failed_kafka_send_survives_restart_with_identical_pending_version(self):
        prepared = self.journal.prepare(snapshot())
        self.restart()  # No acknowledgment: the send may or may not have reached Kafka.
        repeated = self.journal.prepare(snapshot(updated_at="2026-09-29T14:00:00Z"))
        self.assertEqual(canonical(repeated), canonical(prepared))
        self.assertEqual(self.saved()["acknowledged_sequence"], 0)
        self.assertTrue(self.journal.ack(repeated))
        self.assertEqual(self.saved()["acknowledged_sequence"], 1)

    def test_pending_excludes_acknowledged_orders_and_survives_restart(self):
        prepared = self.journal.prepare(snapshot())
        acknowledged = self.journal.prepare(snapshot(order_id="acknowledged-order"))
        self.assertTrue(self.journal.ack(acknowledged))
        self.restart()
        pending = self.journal.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(canonical(pending[0]), canonical(prepared))
        self.assertEqual(pending[0]["limit_price"], Decimal("12.123456789123456789"))
        self.assertTrue(self.journal.ack(pending[0]))
        self.assertEqual(self.journal.pending(), [])

    def test_pending_advances_to_latest_replacement_without_old_versions(self):
        initial = self.journal.prepare(snapshot())
        self.assertTrue(self.journal.ack(initial))
        changed = self.journal.prepare(snapshot(status="cancelled", updated_at="2026-09-29T13:32:00Z"))
        latest = self.journal.prepare(snapshot(status="filled", filled_quantity=Decimal("2.5"),
                                              updated_at="2026-09-29T13:33:00Z"))
        self.assertFalse(self.journal.ack(changed))
        self.assertEqual([canonical(item) for item in self.journal.pending()], [canonical(latest)])

    def test_numeric_formatting_changes_reuse_original_payload_and_sequence(self):
        initial = self.journal.prepare(snapshot(quantity=Decimal("1000.00"), filled_quantity=Decimal("-0.00"),
                                                limit_price=Decimal("12.1234567891234567891234567890000")))
        repeated = self.journal.prepare(snapshot(quantity=1000, filled_quantity=Decimal("0.0"),
                                                 limit_price=Decimal("12.123456789123456789123456789"),
                                                 updated_at="2026-09-29T14:00:00Z"))
        self.assertEqual(canonical(initial), canonical(repeated))
        self.assertEqual(repeated["sequence"], 1)
        changed = self.journal.prepare(snapshot(quantity=1000, filled_quantity=Decimal("0.0"),
                                                limit_price=Decimal("12.123456789123456789123456788"),
                                                updated_at="2026-09-29T14:00:01Z"))
        self.assertEqual(changed["sequence"], 2)

    def test_changed_order_strictly_increments_sequence_and_retries_latest(self):
        initial = self.journal.prepare(snapshot())
        self.assertTrue(self.journal.ack(initial))
        update = snapshot(status="partially_filled", filled_quantity=Decimal("1.25"),
                          updated_at="2026-09-29T13:32:00Z")
        changed = self.journal.prepare(update)
        self.assertEqual(changed["sequence"], 2)
        self.assertEqual(changed["created_at"], initial["created_at"])
        self.assertEqual(self.saved()["acknowledged_sequence"], 1)
        self.restart()
        update["updated_at"] = "2026-09-29T14:00:00Z"
        self.assertEqual(canonical(self.journal.prepare(update)), canonical(changed))
        latest = self.journal.prepare(snapshot(status="filled", filled_quantity=Decimal("2.5"),
                                              updated_at="2026-09-29T14:01:00Z"))
        self.assertEqual(latest["sequence"], 3)

    def test_same_timestamp_can_contain_a_new_broker_state(self):
        self.journal.prepare(snapshot())
        result = self.journal.prepare(snapshot(status="cancelled"))
        self.assertEqual(result["sequence"], 2)

    def test_stale_or_modified_ack_never_acknowledges_latest_version(self):
        initial = self.journal.prepare(snapshot())
        changed = self.journal.prepare(snapshot(status="cancelled", updated_at="2026-09-29T13:32:00Z"))
        self.assertFalse(self.journal.ack(initial))
        altered = deepcopy(changed)
        altered["status"] = "filled"
        self.assertFalse(self.journal.ack(altered))
        self.assertEqual(self.saved()["acknowledged_sequence"], 0)
        self.assertTrue(self.journal.ack(changed))

    def test_ack_persistence_failure_keeps_saved_payload_for_restart_retry(self):
        prepared = self.journal.prepare(snapshot())
        self.journal.db.execute("""CREATE TRIGGER reject_ack BEFORE UPDATE OF acknowledged_sequence
            ON order_publications BEGIN SELECT RAISE(ABORT, 'private-marker'); END""")
        self.journal.db.commit()
        with self.assertRaises(ApiError) as caught:
            self.journal.ack(prepared)
        self.assertNotIn("private-marker", str(caught.exception))
        self.assertEqual(self.saved()["acknowledged_sequence"], 0)
        self.restart()
        self.assertEqual(canonical(self.journal.prepare(snapshot())), canonical(prepared))
        self.journal.db.execute("DROP TRIGGER reject_ack")
        self.journal.db.commit()
        self.assertTrue(self.journal.ack(prepared))

    def test_prepare_persistence_failure_cannot_release_an_unstored_version(self):
        self.journal.prepare(snapshot())
        self.journal.db.execute("""CREATE TRIGGER reject_snapshot BEFORE UPDATE OF payload
            ON order_publications BEGIN SELECT RAISE(ABORT, 'private-marker'); END""")
        self.journal.db.commit()
        with self.assertRaises(ApiError) as caught:
            self.journal.prepare(snapshot(status="cancelled", updated_at="2026-09-29T13:32:00Z"))
        self.assertNotIn("private-marker", str(caught.exception))
        self.assertEqual(self.saved()["sequence"], 1)
        self.assertEqual(json.loads(self.saved()["payload"])["status"], "open")

    def test_changed_binding_or_account_cannot_share_state(self):
        prepared = self.journal.prepare(snapshot())
        for change in ({"account_id": "OTHER"}, {"account_num_id": 8}, {"server_id": "OTHER"}):
            with self.subTest(change=change), self.assertRaisesRegex(ApiError, "another configured account"):
                OrderPublicationJournal(self.path, {**BINDING, **change})
        with self.assertRaisesRegex(ApiError, "does not match"):
            self.journal.prepare(snapshot(account_id="OTHER"))
        self.assertEqual(canonical(self.journal.prepare(snapshot())), canonical(prepared))

    def test_independent_orders_have_independent_sequences(self):
        first = self.journal.prepare(snapshot())
        second = self.journal.prepare(snapshot(order_id="another-order"))
        self.assertEqual(first["sequence"], second["sequence"])
        self.assertEqual(self.journal.prepare(snapshot(status="cancelled"))["sequence"], 2)
        self.assertEqual(self.journal.prepare(snapshot(order_id="another-order"))["sequence"], 1)

    def test_creation_time_is_immutable_and_failed_validation_preserves_state(self):
        initial = self.journal.prepare(snapshot())
        with self.assertRaisesRegex(ApiError, "creation timestamp changed"):
            self.journal.prepare(snapshot(created_at="2026-09-29T13:30:01+00:00"))
        self.assertEqual(canonical(self.journal.prepare(snapshot())), canonical(initial))

    def test_changed_snapshot_rejects_time_regression_by_instant(self):
        self.journal.prepare(snapshot())
        with self.assertRaisesRegex(ApiError, "moved backward"):
            self.journal.prepare(snapshot(status="cancelled", updated_at="2026-09-29T09:30:59-04:00"))
        valid = self.journal.prepare(snapshot(status="cancelled", updated_at="2026-09-29T09:31:01-04:00"))
        self.assertEqual(valid["sequence"], 2)

    def test_sequence_limit_blocks_changes_but_allows_exact_replay(self):
        prepared = self.journal.prepare(snapshot())
        prepared["sequence"] = MAX_SEQUENCE
        with self.journal.db:
            self.journal.db.execute("UPDATE order_publications SET sequence=?, payload=?",
                                    (MAX_SEQUENCE, canonical(prepared).decode("utf-8")))
        self.assertEqual(self.journal.prepare(snapshot())["sequence"], MAX_SEQUENCE)
        with self.assertRaisesRegex(ApiError, "sequence limit reached"):
            self.journal.prepare(snapshot(status="cancelled", updated_at="2026-09-29T13:32:00Z"))
        self.assertEqual(self.saved()["sequence"], MAX_SEQUENCE)

    def test_invalid_timestamp_sequence_and_json_are_safe_errors(self):
        for updates in ({"updated_at": "2026-09-29T13:31:00"}, {"created_at": "private-marker"},
                        {"sequence": 7}, {"limit_price": Decimal("NaN")}, {"order_id": None}):
            with self.subTest(updates=updates), self.assertRaises(ApiError) as caught:
                self.journal.prepare(snapshot(**updates))
            self.assertNotIn("private-marker", str(caught.exception))
        self.assertIsNone(self.saved())

    def test_corrupt_saved_payload_fails_safely(self):
        self.journal.prepare(snapshot())
        with self.journal.db:
            self.journal.db.execute("UPDATE order_publications SET payload='private-marker'")
        with self.assertRaisesRegex(ApiError, "Stored order snapshot state is invalid") as caught:
            self.journal.prepare(snapshot())
        self.assertNotIn("private-marker", str(caught.exception))

    def test_corrupt_pending_payload_fails_safely(self):
        self.journal.prepare(snapshot())
        with self.journal.db:
            self.journal.db.execute("UPDATE order_publications SET payload='private-marker'")
        with self.assertRaisesRegex(ApiError, "Stored order snapshot state is invalid") as caught:
            self.journal.pending()
        self.assertNotIn("private-marker", str(caught.exception))

    def test_journal_can_be_used_by_worker_thread(self):
        results = []
        errors = []
        def worker():
            try:
                result = self.journal.prepare(snapshot())
                results.append(self.journal.ack(result))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [True])

    def test_corrupt_database_or_file_creation_errors_are_safe(self):
        corrupted = Path(self.folder.name) / "corrupt.sqlite3"
        corrupted.write_text("private-marker", encoding="utf-8")
        with self.assertRaises(ApiError) as caught:
            OrderPublicationJournal(corrupted, BINDING)
        self.assertNotIn("private-marker", str(caught.exception))
        with patch.object(Path, "mkdir", side_effect=OSError("private-marker")), self.assertRaises(ApiError) as caught:
            OrderPublicationJournal(Path(self.folder.name) / "blocked.sqlite3", BINDING)
        self.assertNotIn("private-marker", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
