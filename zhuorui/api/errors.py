class ApiError(RuntimeError):
    """An error message safe to show without broker response values."""


class SessionError(ApiError):
    pass


class SessionExpired(SessionError):
    pass


class LoggedInElsewhere(SessionError):
    pass


class BrokerRejected(ApiError):
    """A completed broker response explicitly rejected the request."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


class LoginBlocked(SessionError):
    """Login needs corrected credentials, verification or account review."""


class OrderOutcomeUnknown(ApiError):
    """A write may have reached the broker; it must not be resubmitted."""
