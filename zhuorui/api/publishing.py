"""Independent holdings publisher; order-triggered reads never delay FOK cancel."""
import json
from datetime import datetime, timezone
from pathlib import Path
import queue
import threading
import time

from .signing import canonical
from .errors import ApiError


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ListenerState:
    def __init__(self, path, **initial):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.values = initial

    def update(self, **values):
        with self.lock:
            self.values.update(values, updated_at=utc_now())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_bytes(canonical(self.values))
            temporary.replace(self.path)

    def holdings_recovered(self, **values):
        with self.lock:
            if self.values.get("last_error") == self.values.get("last_holdings_error"):
                values["last_error"] = None
            self.update(**values, last_holdings_error=None)


class HoldingsPublisher:
    def __init__(self, config, settings, producer, client_provider, state, *, now=time.monotonic):
        self.config, self.settings, self.producer = config, settings, producer
        self.client_provider, self.state, self.now = client_provider, state, now
        self.requests = queue.Queue()
        self.thread = threading.Thread(target=self.run, name="api-holdings-publisher", daemon=False)
        self.published = 0

    def start(self):
        self.thread.start()

    def request(self, reason="order_submission"):
        # A queue, not an event: every new submission gets a publication attempt.
        self.requests.put(reason)

    def publish(self, reason):
        from .snapshot import account_snapshot
        start = self.now()
        client = None
        try:
            client = self.client_provider()
            account = client.query("account")
            cash = client.query("cash")
            holdings = client.query("holdings")
            snapshot = account_snapshot(self.config, holdings, cash, account)
            snapshot["trading_enabled"] = self.settings.live_orders_enabled
            elapsed = self.now() - start
            print(f"Holdings query completed in {elapsed:.3f} seconds.", flush=True)
            self.producer.send(self.settings.holdings_topic, snapshot,
                               key=snapshot["account_id"].encode("utf8")).get(timeout=10)
            self.published += 1
            values = dict(last_holdings_publish=utc_now(), holdings_publish_count=self.published,
                          last_holdings_reason=reason)
            if hasattr(self.client_provider, "report_success"):
                self.client_provider.report_success(client, **values)
            else:
                self.state.holdings_recovered(**values, session_status="valid")
            print("Published account details message.", flush=True)
        except Exception as exc:
            message = str(exc) if isinstance(exc, ApiError) else "Holdings query/publication failed; check the broker session and Kafka connection."
            if hasattr(self.client_provider, "report_error"):
                if self.client_provider.report_error(exc, client=client) is False:
                    return  # Late failure from the token replaced by login.
                if hasattr(self.client_provider, "recovery_message"):
                    message = self.client_provider.recovery_message() or message
            self.state.update(last_holdings_error=message, last_error=message, last_holdings_attempt=utc_now())
            print(f"ERROR publishing holdings: {message}", flush=True)

    def run(self):
        due = self.now()
        while True:
            wait = max(0, due - self.now())
            try:
                reason = self.requests.get(timeout=wait)
            except queue.Empty:
                reason = "periodic"
            if reason is None:
                return
            self.publish(reason)
            if reason == "periodic":
                due += self.settings.holdings_interval_seconds
                if due <= self.now():
                    due = self.now() + self.settings.holdings_interval_seconds

    def close(self):
        if self.thread.ident is None:
            return
        self.requests.put(None)
        self.thread.join(timeout=45)
        if self.thread.is_alive():
            raise ApiError("Waiting for the in-flight holdings publication to finish; do not force-stop the process.")
