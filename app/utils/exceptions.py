"""Domain exceptions mapped to HTTP responses by the handlers in app.config.logging_setup."""

from __future__ import annotations


class AppError(Exception):
    status_code = 400
    code = "app_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class ProfileNotAvailableError(ConflictError):
    """Section 8 — the person is not in a selectable state for this vendor."""

    code = "profile_not_available"


class ReservationExpiredError(ConflictError):
    """Section 31 — the reservation lapsed while the session was open."""

    code = "reservation_expired"


class InvalidTransitionError(ConflictError):
    """Section 8/30/33 — a status was set by hand from a state it cannot follow."""

    code = "invalid_transition"


class LLMError(AppError):
    status_code = 502
    code = "llm_error"


class RenderingError(AppError):
    status_code = 500
    code = "rendering_error"
