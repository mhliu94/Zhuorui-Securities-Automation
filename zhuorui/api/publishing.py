"""Independent holdings and order publisher; reads never delay FOK cancel."""
from zhuorui.api import latency
import json
import heapq
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import queue
import threading
import tempfile
import time

from .signing import canonical
from .errors import ApiError
from zhuorui.common.runtime_logging import log_event


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ListenerState:
    def __init__(self, path, **initial):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.values = initial
        self._write_failed = False

    @staticmethod
    def _notice(message):
        log_event("api.state", message, level="WARNING" if message.startswith("WARNING:") else "INFO")

    def update(self, **values):
        """Update memory and attempt an atomic diagnostic snapshot.

        Windows readers can temporarily prevent replacement of an open file.
        Keep the previous complete snapshot on any filesystem failure and retry
        the newest values on the next update/heartbeat. Do not sleep while holding
        this lock: order and session processing share this state.
        """
        with self.lock:
            self.values.update(values, updated_at=utc_now())
            payload = canonical(self.values)
            temporary = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="wb", dir=self.path.parent,
                                                 prefix=self.path.name + ".", suffix=".tmp",
                                                 delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(payload)
                temporary.replace(self.path)
            except OSError:
                if not self._write_failed:
                    self._notice("WARNING: Could not save listener status; keeping the previous snapshot and retrying on the next update.")
                self._write_failed = True
                return False
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass  # Only clean up our own temporary file.
            if self._write_failed:
                self._notice("Listener status file writes recovered.")
            self._write_failed = False
            return True

    def holdings_recovered(self, **values):
        with self.lock:
            if self.values.get("last_error") == self.values.get("last_holdings_error"):
                values["last_error"] = None
            return self.update(**values, last_holdings_error=None)

    def orders_recovered(self, **values):
        with self.lock:
            if self.values.get("last_error") == self.values.get("last_orders_error"):
                values["last_error"] = None
            return self.update(**values, last_orders_error=None)


class HoldingsPublisher:
    def __init__(self, config, settings, producer, client_provider, state, *, now=time.monotonic,
                 wall=time.time):
        self.config, self.settings, self.producer = config, settings, producer
        self.client_provider, self.state, self.now = client_provider, state, now
        self.wall = wall
        self.requests = queue.Queue()
        self.thread = threading.Thread(target=self.run, name="api-holdings-publisher", daemon=False)
        self.published = 0
        self.failures = 0
        self.orders_published = 0
        self.order_failures = 0
        self.order_journal = None
        self.history_cursor = None
        from .account_delta import AccountDeltaPublisher, OrderChanges
        self._account_deltas = AccountDeltaPublisher()
        self._order_changes = OrderChanges()
        self._refresh_until = 0.0
        self._orders_in_scheduler = False

    def start(self):
        self.thread.start()

    def request(self, reason="order_submission", *, delay_seconds=0):
        # Record the deadline at request time, not when a busy worker reads it.
        # Every request is retained; a later immediate refresh can pass it.
        self.requests.put((self.now() + delay_seconds, reason))

    def publish(self, reason):
        from .snapshot import account_snapshot
        start = self.now()
        client = None
        holdings_attempted = False
        stage = "session"
        log_event("api.publisher", "Account publication started.", reason=reason)
        try:
            client = self.client_provider()
            stage = "account_query"
            account = client.query("account")
            stage = "cash_query"
            cash = client.query("cash")
            stage = "holdings_query"
            holdings_attempted = True
            holdings = client.query("holdings")
            stage = "snapshot"
            snapshot = account_snapshot(self.config, holdings, cash, account)
            snapshot["trading_enabled"] = self.settings.live_orders_enabled
            elapsed = self.now() - start
            log_event("api.publisher", f"Holdings query completed in {elapsed:.3f} seconds.")
            stage = "kafka_send"
            def deliver(event):
                nonlocal stage
                stage = "kafka_send"
                future = self.producer.send(self.settings.holdings_topic, event,
                    key=snapshot["account_id"].encode("utf8"))
                stage = "kafka_ack"
                return future.get(timeout=10)
            self._account_deltas.publish(snapshot, deliver)
            self.published += 1
            stage = "record_success"
            values = dict(last_holdings_publish=utc_now(), holdings_publish_count=self.published,
                          last_holdings_reason=reason)
            if hasattr(self.client_provider, "report_success"):
                self.client_provider.report_success(client, **values)
            else:
                self.state.holdings_recovered(**values, session_status="valid")
            log_event("api.publisher", "Published account details message.", reason=reason,
                      published=self.published, recovered_after_failures=self.failures,
                      total_seconds=round(self.now() - start, 3))
            self.failures = 0
        except Exception as exc:
            self.failures += 1
            log_event("api.publisher", "Account publication failed.", level="ERROR", error=exc,
                      stage=stage, reason=reason, consecutive_failures=self.failures,
                      elapsed_seconds=round(self.now() - start, 3))
            message = str(exc) if isinstance(exc, ApiError) else "Holdings query/publication failed; check the broker session and Kafka connection."
            if hasattr(self.client_provider, "report_error"):
                if self.client_provider.report_error(exc, client=client) is False:
                    return  # Late failure from the token replaced by login.
                if hasattr(self.client_provider, "recovery_message"):
                    message = self.client_provider.recovery_message() or message
            self.state.update(last_holdings_error=message, last_error=message, last_holdings_attempt=utc_now())
        finally:
            if holdings_attempted and not self._orders_in_scheduler:
                self.publish_orders(reason)

    def publish_orders(self, reason):
        """Publish all orders created today in Eastern time, including terminal ones.

        Keep failures separate from account publication. The explicit history
        window supplements the broker's own today query across date boundaries.
        All requests and Kafka acknowledgements stay on this background worker.
        """
        from .execution import order_rows
        from .market_data import NEW_YORK
        from .order_snapshot import order_snapshots, order_detail_snapshot
        from .order_publication import OrderPublicationJournal
        from .session import binding

        start, client, stage = self.now(), None, "session"
        try:
            client = self.client_provider()
            sweep_ms = int(self.wall() * 1000)
            day = datetime.fromtimestamp(sweep_ms / 1000, NEW_YORK).date()
            stage = "order_history_query"
            if self.history_cursor is None or self.history_cursor[0] != day or sweep_ms < self.history_cursor[1]:
                history = order_rows(client.query_orders_for_date(day))
            elif sweep_ms > self.history_cursor[1]:
                history = order_rows(client.query_orders_in_window(self.history_cursor[1], sweep_ms))
            else:
                history = []
            stage = "today_orders_query"
            current = order_rows(client.query("orders"))
            # Each endpoint must have unique stable references. Current order
            # state takes precedence when history contains the same order.
            merged = {}
            for rows in (history, current):
                references = set()
                for row in rows:
                    reference = row.get("orderTxnReference")
                    if not isinstance(reference, str) or not reference or reference in references:
                        raise ApiError("Order query contains missing or duplicate transaction references.")
                    references.add(reference)
                    merged[reference] = row
            observed_at = datetime.fromtimestamp(self.wall(), timezone.utc)
            if observed_at.astimezone(NEW_YORK).date() != day:
                raise ApiError("Eastern date changed during order reads; the next refresh will retry.")
            stage = "order_snapshot"
            snapshots = order_snapshots(self.config, {"code": "000000", "data": list(merged.values())},
                                        now=observed_at)
            path = getattr(self.settings, "order_snapshot_journal_file", None)
            if self.order_journal is None and path is not None and Path(path).exists():
                self.order_journal = OrderPublicationJournal(path, binding(self.config))
            visible = {snapshot["order_id"] for snapshot in snapshots}
            if self.order_journal is not None:
                for saved in self.order_journal.tracked():
                    if saved["order_id"] in visible:
                        continue
                    created = datetime.fromisoformat(saved["created_at"].replace("Z", "+00:00"))
                    stamp = round(created.timestamp() * 1000)
                    try:
                        detail = client.query_order_detail(saved["order_id"], stamp)
                        snapshots.append(order_detail_snapshot(self.config, detail, saved, now=observed_at, tracked=True))
                    except Exception as exc:
                        from .errors import SessionExpired
                        if isinstance(exc, SessionExpired):
                            raise
                        log_event("api.publisher", "Tracked ticket detail unavailable; retaining previous state.",
                                  level="WARNING", stage="order_detail_query", error=exc)
            for index, snapshot in enumerate(snapshots):
                if snapshot["filled_quantity"] > 0 and snapshot["average_fill_price"] is None:
                    if self.order_journal is not None:
                        price = self.order_journal.confirmed_terminal_price(snapshot)
                        if price is not None:
                            snapshot["average_fill_price"] = price
                            continue
                    stage = "order_detail_query"
                    row = merged[snapshot["order_id"]]
                    detail = client.query_order_detail(snapshot["order_id"], int(Decimal(str(row["entrustTime"]))))
                    stage = "order_detail_snapshot"
                    snapshots[index] = order_detail_snapshot(
                        self.config, detail, snapshot,
                        now=datetime.fromtimestamp(self.wall(), timezone.utc))
            observed_at = datetime.fromtimestamp(self.wall(), timezone.utc)
            if observed_at.astimezone(NEW_YORK).date() != day:
                raise ApiError("Eastern date changed during order reads; the next refresh will retry.")
            for snapshot in snapshots:
                snapshot["updated_at"] = observed_at.isoformat()
            # Join acknowledgements by the exact reference saved when this command
            # was submitted. A cancel can share its target's reference.
            journal = getattr(self.settings, "journal_file", None)
            if snapshots and journal and Path(journal).is_file():
                import sqlite3
                from contextlib import closing
                with closing(sqlite3.connect(Path(journal).resolve().as_uri() + "?mode=ro", uri=True)) as db:
                    expected = db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()
                    if not expected or json.loads(expected[0]) != binding(self.config):
                        raise ApiError("Command journal belongs to another account.")
                    for snapshot in snapshots:
                        matches = db.execute("SELECT id FROM commands WHERE reference=? "
                            "AND json_extract(payload,'$.order_type') IN ('market','limit','fok') "
                            "AND upper(json_extract(payload,'$.symbol'))=? "
                            "AND upper(json_extract(payload,'$.side'))=?", (snapshot["order_id"], snapshot["symbol"], snapshot["side"])).fetchall()
                        if len(matches) == 1:
                            snapshot["command_id"] = matches[0][0]
            if self._order_changes.observe(snapshots):
                self._refresh_until = self.now() + 10
                self.request('order_change')
            stage = "order_journal"
            path = getattr(self.settings, "order_snapshot_journal_file", None)
            if self.order_journal is None and (snapshots or path is not None and Path(path).exists()):
                self.order_journal = OrderPublicationJournal(path, binding(self.config))
            events = {}
            # Persist the complete batch before any network send. A failed
            # send near midnight must not drop the remaining orders or retries.
            for snapshot in snapshots:
                event = self.order_journal.prepare(snapshot)
                events[(event["account_id"], event["order_id"])] = event
            if self.order_journal is not None:
                for event in self.order_journal.pending():
                    events.setdefault((event["account_id"], event["order_id"]), event)
            for event in events.values():
                if len(canonical(event)) > 65536:
                    raise ApiError("Order snapshot exceeds the KTrader 64 KiB limit.")
                stage = "order_kafka_send"
                key = canonical([event["account_id"], event["order_id"]])
                with latency.span("report_delivery", "order_status", event):
                    self.producer.send(self.settings.order_status_topic, event, key=key).get(timeout=10)
                self.orders_published += 1
                stage = "order_journal_ack"
                if not self.order_journal.ack(event):
                    raise ApiError("Order snapshot state changed before its acknowledgment was recorded.")
            values = dict(last_orders_query=observed_at.isoformat(),
                          orders_publish_count=self.orders_published, last_orders_count=len(snapshots),
                          last_orders_date=day.isoformat(), last_orders_reason=reason)
            if events:
                values["last_orders_publish"] = utc_now()
            if hasattr(self.client_provider, "report_orders_success"):
                self.client_provider.report_orders_success(client, **values)
            else:
                self.state.orders_recovered(**values)
            log_event("api.publisher", "Today's order refresh completed.", reason=reason,
                      orders=len(snapshots), published=self.orders_published,
                      retried_prior_orders=len(events) - len(snapshots),
                      recovered_after_failures=self.order_failures, total_seconds=round(self.now() - start, 3))
            self.order_failures = 0
            self.history_cursor = (day, sweep_ms)
        except Exception as exc:
            self.order_failures += 1
            log_event("api.publisher", "Order query/publication failed.", level="ERROR", error=exc,
                      stage=stage, reason=reason, consecutive_failures=self.order_failures,
                      elapsed_seconds=round(self.now() - start, 3))
            message = str(exc) if isinstance(exc, ApiError) else "Order query/publication failed; check the broker session, order journal and Kafka connection."
            if hasattr(self.client_provider, "report_error"):
                if self.client_provider.report_error(exc, client=client) is False:
                    return
                if hasattr(self.client_provider, "recovery_message"):
                    message = self.client_provider.recovery_message() or message
            self.state.update(last_orders_error=message, last_error=message,
                              last_orders_attempt=utc_now(), orders_publish_count=self.orders_published)

    def run(self):
        try:
            self._run()
        except Exception as exc:
            # The consumer detects the dead worker and stops command processing.
            # Avoid threading's default traceback, which includes raw messages.
            log_event("api.publisher", "Publisher worker exited unexpectedly.", level="ERROR", error=exc)

    def _run(self):
        self._orders_in_scheduler = True
        due = order_due = delta_due = self.now()
        pending, sequence, stopping = [], 0, False
        while True:
            current = self.now()
            if not stopping and order_due <= current:
                started = self.now()
                self.publish_orders('order_poll')
                order_due = max(started + 1.0, self.now() + .01)
            current = self.now()
            if not stopping and due <= current:
                self.publish('periodic')
                due = self.now() + min(30, self.settings.holdings_interval_seconds)
                delta_due = self.now() + 1
            elif not stopping and current < self._refresh_until and delta_due <= current:
                self.publish('fill_settlement')
                delta_due = self.now() + 1
            if pending and pending[0][0] <= self.now():
                _, _, reason = heapq.heappop(pending)
                self.publish(reason)
                delta_due = self.now() + 1
                continue
            if stopping and not pending:
                return
            wake_at = min(due if not stopping else float('inf'),
                          order_due if not stopping else float('inf'),
                          delta_due if not stopping and self.now() < self._refresh_until else float('inf'),
                          pending[0][0] if pending else float('inf'))
            try:
                request = self.requests.get(timeout=max(0, wake_at - self.now()))
            except queue.Empty:
                continue
            if request is None:
                stopping = True
            else:
                ready_at, reason = request
                heapq.heappush(pending, (ready_at, sequence, reason))
                sequence += 1

    def close(self):
        if self.thread.ident is not None:
            self.requests.put(None)
            self.thread.join(timeout=45)
            if self.thread.is_alive():
                raise ApiError("Waiting for the in-flight account/order publication to finish; do not force-stop the process.")
        if self.order_journal is not None:
            self.order_journal.close()
            self.order_journal = None
        from .account_delta import AccountDeltaPublisher, OrderChanges
        self._account_deltas = AccountDeltaPublisher()
        self._order_changes = OrderChanges()
        self._refresh_until = 0.0
        self._orders_in_scheduler = False
