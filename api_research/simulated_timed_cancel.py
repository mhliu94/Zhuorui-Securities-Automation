"""One-second cancellation strategy for the offline broker model only.

There is no live transport in this module. Broker lookup by command ID is a
simulation facility, not an assumed Zhuorui API capability.
"""
from dataclasses import dataclass
import time

from simulated_broker import OrderSpec, SimulatedBroker, SimOrder


@dataclass(frozen=True)
class TimedCancelResult:
    order: SimOrder
    started_at: float
    cancel_requested_at: float | None
    acknowledgement_lost: bool


def submit_with_timed_cancel(broker: SimulatedBroker, spec: OrderSpec, *,
                             now=time.monotonic, sleep=time.sleep,
                             lose_acknowledgement=False):
    if not isinstance(broker, SimulatedBroker):
        raise TypeError("This strategy runner supports only the offline simulated broker")
    if spec.kind != "limit":
        raise ValueError("Timed cancellation applies to a limit order")
    started = now()
    acknowledgement_lost = False
    try:
        observed = broker.submit(spec, lose_acknowledgement=lose_acknowledgement)
    except TimeoutError:
        acknowledgement_lost = True
        observed = broker.get_order(spec.command_id)
        if observed is None:
            raise RuntimeError("Order outcome unknown; no resubmission or cancellation attempted")
    if observed.spec != spec:
        raise RuntimeError("Order identity mismatch; cancellation not attempted")
    if observed.status not in {"open", "partially_filled"}:
        return TimedCancelResult(observed, started, None, acknowledgement_lost)
    remaining_delay = started + 1.0 - now()
    if remaining_delay > 0:
        sleep(remaining_delay)
    # Fills may occur between acknowledgement and the cancellation deadline.
    latest = broker.get_order(spec.command_id)
    if latest is None or latest.spec != spec:
        raise RuntimeError("Order identity unavailable; cancellation not attempted")
    if latest.status not in {"open", "partially_filled"}:
        return TimedCancelResult(latest, started, None, acknowledgement_lost)
    cancel_at = now()
    final = broker.cancel(spec.command_id)
    return TimedCancelResult(final, started, cancel_at, acknowledgement_lost)
