"""Command execution and one-second cancellation, independent of Kafka/Android."""
from dataclasses import asdict
from decimal import Decimal, ROUND_FLOOR
import time

from .commands import CancelCommand, command_fingerprint
from .errors import ApiError, BrokerRejected, SessionError, OrderOutcomeUnknown
from .orders import plan_order, plan_cancel

TERMINAL_STATES = {"8", "6", "F", "5", "G", "9", "J", "FILLED", "CANCELED", "REJECTED", "DONE_FOR_DAY"}
PENDING_CANCEL_STATES = {"3", "4", "PENDING_CANCEL"}
CANCELLABLE_STATES = {"0", "1", "2", "A", "H", "7", "E", "ACK", "PENDING_NEW", "NEW", "PENDING_REPLACE", "REPLACED", "PARTIALLY_FILLED"}


def order_rows(response):
    if not isinstance(response, dict) or response.get("code") != "000000" or not isinstance(response.get("data"), list):
        raise ApiError("Today's orders returned an unsupported response shape.")
    rows = response["data"]
    if not all(isinstance(row, dict) for row in rows):
        raise ApiError("Today's orders returned an invalid record.")
    return rows


def cancel_references(response, command):
    rows = [row for row in order_rows(response) if row.get("ts") == "US"]
    if not command.cancel_all:
        rows = [row for row in rows if row.get("orderTxnReference") == command.order_reference]
        if len(rows) != 1:
            raise ApiError("Cancellation reference is not uniquely present in this account's US orders today.")
    references = []
    for row in rows:
        state = row.get("entrustStatus")
        if state in TERMINAL_STATES | PENDING_CANCEL_STATES:
            continue
        if state not in CANCELLABLE_STATES:
            raise ApiError("An order has an unrecognized cancellation state; no broad cancellation was attempted.")
        reference = row.get("orderTxnReference")
        plan_cancel(reference)
        if reference in references:
            raise ApiError("Today's orders contain duplicate transaction references.")
        references.append(reference)
    return references


class CommandExecutor:
    def __init__(self, settings, api_settings, journal, client_provider, holdings, emit, *, now=time.monotonic, wall=time.time, sleep=time.sleep):
        self.settings, self.api_settings, self.journal = settings, api_settings, journal
        self.client_provider, self.holdings, self.emit = client_provider, holdings, emit
        self.now, self.wall, self.sleep = now, wall, sleep

    def preflight(self, client):
        client.query("account")
        auth = client.query("trade-auth")
        if not isinstance(auth.get("data"), dict) or not auth["data"]:
            raise ApiError("Trading authorization is locked. Unlock it in the same emulator, then send a new command.")

    def _report_session_error(self, error, client):
        reporter = getattr(self.client_provider, "report_error", None)
        if isinstance(error, SessionError) and callable(reporter):
            # Reporting schedules recovery; it must never perform a login or
            # replay a write from inside the active command.
            reporter(error, client=client)

    def execute(self, command):
        if not self.journal.claim(command.command_id, asdict(command), semantic_digest=command_fingerprint(command)):
            previous = self.journal.get(command.command_id)
            self.emit(command, "duplicate", "Command was already recorded; it was not resent.", previous_state=previous["state"])
            return
        if not self.settings.live_orders_enabled:
            self.journal.update(command.command_id, "disabled", message="Live order execution is disabled.")
            self.emit(command, "disabled", "Live order execution is disabled; command received without submitting a trade.")
            return
        if self.journal.unresolved():
            self.journal.update(command.command_id, "blocked", message="Earlier submission has an unknown outcome.")
            self.emit(command, "blocked", "An earlier submission needs reconciliation in the app; no new order was sent.")
            return
        client = None
        try:
            client = self.client_provider()
            self.preflight(client)
            if isinstance(command, CancelCommand):
                self.cancel_command(client, command)
            else:
                self.submit(client, command)
        except Exception as exc:
            self._report_session_error(exc, client)
            record = self.journal.get(command.command_id)
            if record["state"] in {"dispatching", "submitted"}:
                # Every dispatched operation handles its own outcome. A journal
                # failure or unexpected exception after dispatch is uncertain.
                self.journal.update(command.command_id, "unknown", message="Execution interrupted after dispatch.")
                self.emit(command, "unknown", "Execution was interrupted after dispatch; reconcile the account before another order.")
            else:
                message = str(exc) if isinstance(exc, ApiError) else "Command could not be prepared; no broker order was sent."
                self.journal.update(command.command_id, "rejected", message=message)
                self.emit(command, "rejected", message)

    def submit(self, client, command):
        quantity = command.quantity
        if quantity is None:
            quantity = client.quantity_for_notional(command.symbol, command.notional_usd)
        kind = "timed-cancel" if command.order_type == "fok" else command.order_type
        plan_order(command.symbol, command.side, quantity, kind, price=command.limit_price,
                   allow_pre_post=command.allow_pre_post, cancel_after=self.api_settings.cancel_after_seconds)
        cancel_due = self.wall() + self.api_settings.cancel_after_seconds if kind == "timed-cancel" else None
        self.journal.update(command.command_id, "dispatching", cancel_due=cancel_due)
        started = self.now()
        outcome = None
        try:
            response = client.submit_order(command.symbol, command.side, quantity, kind,
                                           price=command.limit_price, allow_pre_post=command.allow_pre_post)
            data = response.get("data")
            reference = data.get("orderTxnReference") if isinstance(data, dict) else None
            try:
                plan_cancel(reference)
            except ApiError:
                raise OrderOutcomeUnknown("Broker acknowledged submission without a usable order reference; reconcile before another order.") from None
            self.journal.update(command.command_id, "submitted", reference=reference,
                                cancel_state="pending" if cancel_due else None)
        except (BrokerRejected, SessionError) as exc:
            self.journal.update(command.command_id, "rejected", message=str(exc))
            self._report_session_error(exc, client)
            outcome = ("rejected", str(exc))
        except Exception:
            self.journal.update(command.command_id, "unknown", message="Submission response is unavailable or incomplete.")
            outcome = ("unknown", "Submission outcome is unknown. The command will not be resubmitted; reconcile today's orders in the app.")
        finally:
            # This starts an independent holdings read immediately after the
            # submission attempt; it cannot consume the cancellation deadline.
            self.holdings.request("order_submission")
        if outcome:
            self.emit(command, *outcome)
            return
        if kind == "timed-cancel":
            remaining = started + self.api_settings.cancel_after_seconds - self.now()
            if remaining > 0:
                self.sleep(remaining)
            self.timed_cancel(client, command.command_id, reference, command=command,
                              deadline_missed=self.now() > started + self.api_settings.cancel_after_seconds + 0.05)
        else:
            self.emit(command, "submitted", "Broker acknowledged the order; acknowledgement does not establish a fill.", order_reference=reference, quantity=quantity)

    def timed_cancel(self, client, command_id, reference, *, command=None, deadline_missed=False):
        self.journal.update(command_id, "submitted", cancel_state="dispatching")
        try:
            client.cancel_order(reference)
            self.journal.update(command_id, "submitted", cancel_state="requested")
            status, message = "cancel_requested", "Timed Limit order acknowledged and cancellation requested; fills remain possible."
        except (BrokerRejected, SessionError) as exc:
            self.journal.update(command_id, "submitted", cancel_state="rejected", message=str(exc))
            self._report_session_error(exc, client)
            status, message = "cancel_rejected", "Timed cancellation was rejected; inspect the order's actual state."
        except Exception:
            self.journal.update(command_id, "submitted", cancel_state="unknown", message="Cancellation outcome unknown.")
            status, message = "cancel_unknown", "Cancellation outcome is unknown; it will not be blindly repeated."
        finally:
            self.holdings.request("order_cancellation")
        self.emit(command, status, message, command_id=command_id, order_reference=reference,
                  cancellation_deadline_missed=deadline_missed, native_fok=False)

    def cancel_command(self, client, command):
        references = cancel_references(client.query("orders"), command)
        if not references:
            self.journal.update(command.command_id, "no_action", message="No cancellable orders matched.")
            self.emit(command, "no_action", "No cancellable US orders matched the command.")
            return
        self.journal.update(command.command_id, "dispatching")
        requested = 0
        for reference in references:
            try:
                client.cancel_order(reference)
                requested += 1
            except (BrokerRejected, SessionError) as exc:
                self.journal.update(command.command_id, "rejected", message=str(exc))
                self._report_session_error(exc, client)
                self.emit(command, "cancel_rejected", str(exc), cancellations_requested=requested)
                return
            except Exception:
                self.journal.update(command.command_id, "unknown", message="Cancellation outcome unknown.")
                self.emit(command, "cancel_unknown", "Cancellation outcome is unknown; no additional cancellations were sent.", cancellations_requested=requested)
                return
            finally:
                self.holdings.request("order_cancellation")
        self.journal.update(command.command_id, "cancel_requested")
        self.emit(command, "cancel_requested", "Cancellation requests acknowledged; query order records to establish the final state.", cancellations_requested=requested)

    def recover_pending_cancellations(self):
        if not self.settings.live_orders_enabled:
            return
        for row in self.journal.pending_cancellations():
            if row["cancel_state"] != "pending":
                self.emit(None, "cancel_unknown", "An interrupted cancellation needs reconciliation; it was not resent.", command_id=row["id"])
                continue
            client = None
            try:
                client = self.client_provider()
                self.preflight(client)
                if row["cancel_due"] > self.wall():
                    self.sleep(row["cancel_due"] - self.wall())
                self.timed_cancel(client, row["id"], row["reference"], deadline_missed=self.wall() > row["cancel_due"] + 0.05)
            except ApiError as exc:
                self._report_session_error(exc, client)
                self.emit(None, "blocked", "A pending timed cancellation could not be recovered; inspect the order in the app.", command_id=row["id"])
