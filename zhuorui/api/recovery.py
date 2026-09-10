"""Pure timing and restart state for confirmed broker logout recovery.

The caller decides which broker errors establish logout, serializes access,
persists ``to_dict()`` atomically, and performs login. This module makes no
network or filesystem calls and never contains credentials or session tokens.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

from .errors import ApiError


BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
LOGOUT_REASONS = {"session_invalid", "session_expired", "logged_out", "logged_in_elsewhere",
                  "000102", "000112"}
BLOCK_REASONS = {"credentials", "verification_required", "unsupported", "manual_intervention",
                 "login_rejected"}


def _seconds(value, label="Recovery time"):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ApiError(f"{label} must include a timezone.")
        try:
            value = value.timestamp()
        except (ValueError, OverflowError, OSError):
            raise ApiError(f"{label} is outside the supported date range.") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(f"{label} must be Unix seconds or a timezone-aware datetime.")
    try:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError()
        datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise ApiError(f"{label} is outside the supported date range.") from None
    return value


def _retry(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError("Recovery retry interval must be a finite positive number.")
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise ApiError("Recovery retry interval must be a finite positive number.") from None
    if not math.isfinite(value) or value <= 0:
        raise ApiError("Recovery retry interval must be a finite positive number.")
    return value


def login_delay_seconds(detected_at) -> int:
    """Wait five minutes when detection is 09:00 <= Beijing time < 16:00.

    Outside that interval the first attempt is immediately due. The resulting
    five-minute deadline is not shortened when it crosses 16:00.
    """
    try:
        instant = datetime.fromtimestamp(_seconds(detected_at), timezone.utc).astimezone(BEIJING)
    except (ValueError, OverflowError, OSError):
        raise ApiError("Logout detection time is outside the supported date range.") from None
    return 300 if 9 <= instant.hour < 16 else 0


@dataclass
class RecoverySchedule:
    """One logout episode, with the first detection retained until success.

    Persist after observing logout, starting an attempt, and recording its
    outcome. ``start_attempt`` advances the retry deadline before the caller
    begins login, protecting restarts during an interrupted attempt.
    ``from_dict`` releases only the in-process marker after restart, retaining
    the saved deadline, attempt count and permanent block.
    """
    retry_seconds: float = 300.0
    detected_at: float | None = None
    reason: str | None = None
    due_at: float | None = None
    attempts: int = 0
    last_attempt_at: float | None = None
    in_progress: bool = False
    blocked_reason: str | None = None

    def __post_init__(self):
        self.retry_seconds = _retry(self.retry_seconds)

    def observe_logout(self, at_utc, reason="session_invalid") -> bool:
        """Record only the first confirmed detection; return whether it is new."""
        if not isinstance(reason, str) or reason not in LOGOUT_REASONS:
            raise ApiError("Recovery requires a recognized logout reason.")
        instant = _seconds(at_utc, "Logout detection time")
        if self.detected_at is not None:
            return False
        due = _seconds(instant + login_delay_seconds(instant), "First recovery deadline")
        self.detected_at, self.reason = instant, reason
        self.due_at = due
        return True

    # Natural-language compatibility for callers referring to an observed event.
    observed_logout = observe_logout

    def ready(self, now) -> bool:
        instant = _seconds(now)
        return (self.detected_at is not None and self.due_at is not None
                and not self.in_progress and self.blocked_reason is None
                and instant >= self.due_at)

    def start_attempt(self, now) -> bool:
        """Claim a due attempt and reserve its next retry time before login."""
        instant = _seconds(now)
        if not self.ready(instant):
            return False
        due = _seconds(instant + self.retry_seconds, "Recovery retry deadline")
        self.attempts += 1
        self.last_attempt_at = instant
        self.due_at = due
        self.in_progress = True
        return True

    def failed(self, now, *, blocked_reason=None):
        """Schedule retry after failure, or pause for explicit operator action.

        Pass only a fixed block category, never a broker error body. Credential
        or verification errors remain blocked across restart until ``unblock``
        or ``succeeded`` is called after operator action or a session change.
        """
        instant = _seconds(now)
        if self.detected_at is None or not self.in_progress or self.last_attempt_at is None:
            raise ApiError("No recovery attempt is in progress.")
        if blocked_reason is not None and (not isinstance(blocked_reason, str) or blocked_reason not in BLOCK_REASONS):
            raise ApiError("Unrecognized recovery block reason.")
        self.due_at = _seconds(max(instant, self.last_attempt_at) + self.retry_seconds, "Recovery retry deadline")
        self.in_progress = False
        self.blocked_reason = blocked_reason

    def unblock(self):
        """Remove an operator-action block without resetting the saved deadline."""
        self.blocked_reason = None

    def succeeded(self):
        """Clear the logout episode only after the caller confirms login success."""
        self.detected_at = self.reason = self.due_at = self.last_attempt_at = None
        self.attempts = 0
        self.in_progress = False
        self.blocked_reason = None

    def to_dict(self) -> dict:
        """Return a JSON-serializable, non-secret snapshot for atomic persistence."""
        return {"version": 1, "retry_seconds": self.retry_seconds,
                "detected_at": self.detected_at, "reason": self.reason, "due_at": self.due_at,
                "attempts": self.attempts, "last_attempt_at": self.last_attempt_at,
                "in_progress": self.in_progress, "blocked_reason": self.blocked_reason}

    @classmethod
    def from_dict(cls, value, *, retry_seconds=None):
        """Restore persisted timing; optionally apply a new interval to future retries.

        Existing deadlines never move when configuration changes. A persisted
        in-progress attempt may have completed before the crash, so restoration
        keeps its reserved retry deadline rather than retrying immediately.
        """
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
            raise ApiError("Unsupported recovery schedule state.")
        result = cls(_retry(value.get("retry_seconds")) if retry_seconds is None else _retry(retry_seconds))
        required = {"detected_at", "reason", "due_at", "attempts", "last_attempt_at", "in_progress", "blocked_reason"}
        if not required <= value.keys():
            raise ApiError("Incomplete recovery schedule state.")
        attempts, progressing = value["attempts"], value["in_progress"]
        if type(attempts) is not int or attempts < 0 or type(progressing) is not bool:
            raise ApiError("Invalid recovery attempt state.")
        detected = value["detected_at"]
        if detected is None:
            if (any(value[key] is not None for key in ("reason", "due_at", "last_attempt_at", "blocked_reason"))
                    or attempts != 0 or progressing):
                raise ApiError("Inactive recovery schedule contains an unfinished attempt.")
            return result
        reason, blocked = value["reason"], value["blocked_reason"]
        if (not isinstance(reason, str) or reason not in LOGOUT_REASONS
                or (blocked is not None and (not isinstance(blocked, str) or blocked not in BLOCK_REASONS))):
            raise ApiError("Unrecognized recovery state reason.")
        detected = _seconds(detected, "Saved logout detection time")
        due = _seconds(value["due_at"], "Saved recovery deadline")
        last = value["last_attempt_at"]
        if due < detected:
            raise ApiError("Recovery deadline precedes logout detection.")
        if attempts == 0:
            if last is not None or progressing or blocked is not None or due != detected + login_delay_seconds(detected):
                raise ApiError("Invalid initial recovery deadline.")
        else:
            last = _seconds(last, "Saved recovery attempt time")
            if last < detected or due <= last or (progressing and blocked is not None):
                raise ApiError("Invalid saved recovery attempt timing.")
        result.detected_at, result.reason, result.due_at = detected, reason, due
        result.attempts, result.last_attempt_at, result.blocked_reason = attempts, last, blocked
        # There is no live attempt owned by this newly restored scheduler.
        result.in_progress = False
        return result
