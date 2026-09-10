"""Kafka command listener and recurring account publications over HTTP APIs."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from .client import ApiClient
from .commands import decode_command, parse_command, validate_command_age
from .config import load_settings
from .errors import ApiError, SessionError, SessionExpired, LoggedInElsewhere, LoginBlocked
from .execution import CommandExecutor
from .journal import CommandJournal, InstanceLock
from .listener_config import load_listener_settings
from .publishing import HoldingsPublisher, ListenerState, utc_now
from .session import binding, load_session
from .signing import canonical
from .recovery import RecoverySchedule, BEIJING


class ClientProvider:
    def __init__(self, config_path, config, api_settings, settings, state, *, wall=time.time):
        self.config_path, self.config = config_path, config
        self.api_settings, self.settings, self.state = api_settings, settings, state
        self.lock = threading.RLock()
        self.wall = wall
        self.cached = None
        self.session = None
        self.stamp = None
        self.last_import_attempt = 0
        self.recovery_needed = False
        self.displaced = False
        self.recovery_path = api_settings.session_file.with_suffix(".recovery.json")
        self.schedule = RecoverySchedule(retry_seconds=settings.login_retry_seconds)
        self.schedule_generation = None
        if self.recovery_path.exists():
            try:
                saved = json.loads(self.recovery_path.read_text(encoding="utf8"))
                if not isinstance(saved, dict) or saved.get("binding") != binding(config):
                    raise ValueError()
                self.schedule = RecoverySchedule.from_dict(saved["schedule"], retry_seconds=settings.login_retry_seconds)
                self.schedule_generation = saved.get("generation")
            except (ValueError, KeyError, OSError, ApiError):
                raise SessionError("Saved login recovery state cannot be validated for this account.") from None

    def _persist(self):
        self.recovery_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.recovery_path.with_suffix(self.recovery_path.suffix + ".tmp")
        temporary.write_bytes(canonical({"binding": binding(self.config), "generation": self.schedule_generation,
                                         "schedule": self.schedule.to_dict()}))
        temporary.replace(self.recovery_path)

    def _waiting_message(self):
        if not self.settings.auto_login_enabled:
            return "Session is invalid and automatic login is disabled."
        if self.schedule.blocked_reason:
            return "Automatic login is paused after a rejected login. Check credentials or complete verification in the emulator and import its session."
        if self.schedule.in_progress:
            return "Automatic password login is in progress."
        if self.schedule.due_at is not None:
            due = datetime.fromtimestamp(self.schedule.due_at, BEIJING).isoformat(timespec="seconds")
            return f"Session is invalid. Automatic login is scheduled for {due} Beijing time."
        return "Session is unavailable; import the configured emulator's existing identity."

    def _recovery_state(self):
        self.state.update(logout_detected_at=self.schedule.detected_at, login_due_at=self.schedule.due_at,
                          login_attempts=self.schedule.attempts, login_blocked_reason=self.schedule.blocked_reason,
                          last_error=self._waiting_message())

    def _load_client(self):
        if self.schedule.in_progress and self.cached is not None:
            return self.cached
        stamp = self.api_settings.session_file.stat().st_mtime_ns
        if self.cached is None or stamp != self.stamp:
            session = load_session(self.api_settings.session_file, self.config)
            client = ApiClient(session, self.api_settings)
            generation = session.get("generation") or hashlib.sha256(session["headers"]["token"].encode()).hexdigest()
            session = {**session, "generation": generation}
            if self.schedule.detected_at is not None and generation != self.schedule_generation:
                self.schedule.succeeded()
                self.schedule_generation = generation
                self._persist()
                self.state.update(session_status="imported", last_error=None, last_holdings_error=None,
                                  logout_detected_at=None, login_due_at=None, login_blocked_reason=None)
            self.cached, self.session, self.stamp = client, session, stamp
            self.recovery_needed = False
            self.displaced = self.schedule.reason == "logged_in_elsewhere"
        return self.cached

    def __call__(self):
        with self.lock:
            try:
                client = self._load_client()
                if self.schedule.detected_at is not None:
                    error = LoggedInElsewhere if self.displaced else SessionExpired
                    raise error(self._waiting_message())
                return client
            except (LoggedInElsewhere, SessionExpired):
                raise
            except (SessionError, OSError):
                self.recovery_needed = True
                self.state.update(session_status="unavailable", last_error="Import the valid login from the configured emulator.")
                raise SessionError("No usable encrypted session. Import the current emulator login under this Windows user.") from None

    def report_error(self, error, *, client=None):
        with self.lock:
            if client is not None and client is not self.cached:
                return False
            if isinstance(error, (LoggedInElsewhere, SessionExpired)):
                self.displaced = isinstance(error, LoggedInElsewhere)
                reason = "logged_in_elsewhere" if self.displaced else "session_expired"
                if self.schedule.observe_logout(self.wall(), reason):
                    self.schedule_generation = self.session.get("generation") if self.session else None
                    self._persist()
                self.recovery_needed = True
                self.state.update(session_status="logged_in_elsewhere" if self.displaced else "invalid")
                self._recovery_state()
            elif isinstance(error, SessionError):
                self.recovery_needed = True
                self.state.update(session_status="unavailable", last_error=str(error))
            return True

    def report_success(self, client, **values):
        with self.lock:
            if client is not self.cached or self.schedule.detected_at is not None:
                return False
            self.state.holdings_recovered(**values, session_status="valid")
            return True

    def recovery_message(self):
        with self.lock:
            return self._waiting_message() if self.schedule.detected_at is not None else None

    def refresh_from_emulator(self):
        if self.schedule.detected_at is not None or not self.settings.auto_import_session or time.monotonic() - self.last_import_attempt < 30:
            return
        self.last_import_attempt = time.monotonic()
        # Optional local import only; never a password login or token-refresh API.
        try:
            from .emulator import import_emulator_session
            with self.lock:
                import_emulator_session(self.config_path, self.config, self.api_settings)
                self.cached, self.stamp = None, None
            self.state.update(session_status="imported", last_error=None)
        except ApiError:
            self.state.update(session_status="unavailable", last_error="Automatic local import failed; restore emulator login/root access and import again.")

    def bootstrap(self):
        # A direct-login token can be newer than the emulator's saved token.
        # Preserve a usable encrypted identity instead of importing over it.
        try:
            with self.lock:
                self._load_client()
                if self.schedule.detected_at is not None:
                    self._recovery_state()
        except (SessionError, OSError):
            self.refresh_from_emulator()

    def recover(self):
        """Run only on the consumer thread, between complete command flows."""
        from .auth import password_login
        with self.lock:
            try:
                self._load_client()
            except (SessionError, OSError):
                return False  # No verified identity to send a password with.
            if not self.settings.auto_login_enabled or not self.schedule.start_attempt(self.wall()):
                return False
            self._persist()  # Reserve retry deadline before sending credentials.
            self.state.update(session_status="logging_in")
            self._recovery_state()
            previous = self.session
        try:
            session = password_login(self.config, self.api_settings, previous, now=self.wall)
        except LoginBlocked as exc:
            with self.lock:
                self.schedule.failed(self.wall(), blocked_reason="login_rejected")
                self._persist()
                self.state.update(session_status="login_blocked", last_error=str(exc),
                                  last_holdings_error=str(exc), login_blocked_reason="login_rejected")
            return False
        except (ApiError, OSError):
            with self.lock:
                self.schedule.failed(self.wall())
                self._persist()
                self.state.update(session_status="login_retry_wait")
                self._recovery_state()
            return False
        with self.lock:
            self.session = session
            self.cached = ApiClient(session, self.api_settings)
            self.stamp = self.api_settings.session_file.stat().st_mtime_ns
            self.schedule.succeeded()
            self.schedule_generation = session["generation"]
            self.recovery_needed = self.displaced = False
            self._persist()
            self.state.update(session_status="login_succeeded", last_error=None, last_holdings_error=None,
                              logout_detected_at=None, login_due_at=None, login_blocked_reason=None,
                              last_login_at=utc_now(), login_attempts=0)
        print("Automatic password login succeeded; account publication will refresh immediately.", flush=True)
        return True


def status_event(settings, config, command, status, message, **extra):
    event = {"server_id": settings.server_id, "account_id": config.get("account_id"),
             "account_num_id": config.get("account_num_id"), "status": status,
             "message": message, "timestamp": time.time(), "backend": "api"}
    if command is not None:
        event["command_id"] = command.command_id
        if hasattr(command, "symbol"):
            event.update(symbol=command.symbol, side=command.side, order_type=command.order_type)
    event.update(extra)
    return event


def handle_record(record, config, settings, executor, emit):
    message_id = f"kafka:{record.topic}:{record.partition}:{record.offset}"
    command = None
    try:
        payload = decode_command(record.value)
        command = parse_command(payload, config, message_id=message_id, server_id=settings.server_id)
        if command is None:
            return
        stamp = getattr(record, "timestamp", None)
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or stamp <= 0:
            raise ApiError("Kafka command has no trustworthy timestamp; it will not be executed.")
        validate_command_age(payload, now=time.time(), max_age_seconds=settings.command_max_age_seconds, kafka_timestamp_ms=stamp)
        executor.execute(command)
    except ApiError as exc:
        emit(command, "rejected", str(exc), command_id=command.command_id if command else message_id)


def run_listener(config_path):
    from kafka import KafkaConsumer, KafkaProducer
    from kafka.structs import TopicPartition, OffsetAndMetadata

    config_path = Path(config_path).resolve()
    config, api_settings = load_settings(config_path)
    settings = load_listener_settings(config_path, config)
    state = ListenerState(settings.state_file, backend="api", running=True,
                          started_at=utc_now(), live_orders_enabled=settings.live_orders_enabled,
                          session_status="unchecked", holdings_interval_seconds=settings.holdings_interval_seconds,
                          last_holdings_publish=None, last_error=None)
    lock_file = settings.journal_file.with_suffix(settings.journal_file.suffix + ".lock")
    with InstanceLock(lock_file):
        settings.stop_file.unlink(missing_ok=True)
        state.update(pid=os.getpid())
        journal = CommandJournal(settings.journal_file, binding(config))
        clients = ClientProvider(config_path, config, api_settings, settings, state)
        producer = consumer = publisher = None
        try:
            producer = KafkaProducer(bootstrap_servers=settings.bootstrap_servers, client_id=settings.client_id,
                value_serializer=canonical, acks="all", retries=3, max_block_ms=10000,
                request_timeout_ms=10000, bootstrap_timeout_ms=10000)
            # Publish statuses for every command, including disabled/rejected
            # commands, so a controller never mistakes a received command for a fill.
            def emit(command, status, message, **extra):
                event = status_event(settings, config, command, status, message, **extra)
                try:
                    producer.send(settings.order_status_topic, event,
                                  key=str(event.get("command_id", settings.server_id)).encode()).get(timeout=10)
                except Exception:
                    state.update(last_error="Could not publish a command result to Kafka; inspect the local journal.")
                print(f"API command {status}: {message}", flush=True)
            clients.bootstrap()
            publisher = HoldingsPublisher(config, settings, producer, clients, state)
            publisher.start()
            executor = CommandExecutor(settings, api_settings, journal, clients, publisher, emit)
            executor.recover_pending_cancellations()
            consumer = KafkaConsumer(settings.command_topic, bootstrap_servers=settings.bootstrap_servers,
                client_id=settings.client_id, group_id=settings.group_id, auto_offset_reset="latest",
                enable_auto_commit=False, max_poll_records=1, max_poll_interval_ms=300000,
                request_timeout_ms=40000, session_timeout_ms=30000, bootstrap_timeout_ms=10000)
            state.update(kafka_connected=True)
            print(f"Zhuorui API server {settings.server_id} consuming commands and publishing holdings every {settings.holdings_interval_seconds:g} seconds.", flush=True)
            print("Live order execution is " + ("ENABLED." if settings.live_orders_enabled else "DISABLED; command receipt/validation and holdings publication are active."), flush=True)
            next_heartbeat = time.monotonic()
            while not settings.stop_file.exists():
                if clients.recover():
                    publisher.request("login_recovery")
                records = consumer.poll(timeout_ms=max(1, int(settings.poll_seconds * 1000)), max_records=1)
                if not publisher.thread.is_alive():
                    raise ApiError("Holdings publisher stopped unexpectedly; order processing has been stopped.")
                for messages in records.values():
                    for record in messages:
                        if settings.stop_file.exists():
                            break
                        handle_record(record, config, settings, executor, emit)
                        # Journal is committed before this offset. Redelivery is
                        # safe even if this commit fails or the process crashes.
                        consumer.commit({TopicPartition(record.topic, record.partition): OffsetAndMetadata(record.offset + 1)})
                if time.monotonic() >= next_heartbeat:
                    state.update(kafka_connected=True, unresolved_submissions=len(journal.unresolved()))
                    next_heartbeat = time.monotonic() + 15
                    if clients.recovery_needed and clients.schedule.detected_at is None:
                        clients.refresh_from_emulator()
            print("Stopping API listener after the active command and queued holdings publications finish.", flush=True)
        except KeyboardInterrupt:
            print("Stopping Zhuorui API listener.", flush=True)
        except Exception as exc:
            message = str(exc) if isinstance(exc, ApiError) else f"API listener stopped after a Kafka/runtime error ({type(exc).__name__}); inspect the local journal before restart."
            state.update(last_error=message)
            raise ApiError(message) from None
        finally:
            cleanup_errors = []
            def cleanup(label, operation):
                try:
                    operation()
                except Exception:
                    cleanup_errors.append(label)
            if consumer is not None:
                cleanup("consumer", lambda: consumer.close(autocommit=False, timeout_ms=10000))
            if publisher is not None:
                cleanup("holdings publisher", publisher.close)
                # Retain the instance lock while any publication is in flight.
                # The stop launcher deliberately never force-kills this process.
                if publisher.thread.ident is not None:
                    while publisher.thread.is_alive():
                        publisher.thread.join(timeout=1)
            if producer is not None:
                cleanup("producer flush", lambda: producer.flush(timeout=10))
                cleanup("producer close", lambda: producer.close(timeout=10))
            cleanup("journal", journal.close)
            state.update(running=False, kafka_connected=False,
                         shutdown_errors=cleanup_errors)
    return 0
