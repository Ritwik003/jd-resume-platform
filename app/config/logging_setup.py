"""Logging configuration and exception handlers."""

from __future__ import annotations

import logging
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.config import DEBUG
from app.utils.exceptions import AppError

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.DEBUG if DEBUG else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # DEBUG is for *our* modules. Left alone, these libraries log every LLM
    # request body, every response header and every Set-Cookie to the console,
    # which buries the application's own output and writes prompt text to logs.
    for noisy in (
        "httpx", "httpcore", "openai", "anthropic", "groq",
        "passlib", "urllib3", "asyncio", "watchfiles", "multipart",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    #: Postgres cancels a statement that waits too long for a lock, and the
    #: message it raises for that is the same OperationalError class as a
    #: genuinely broken query. These two markers are what tell them apart.
    _LOCK_MARKERS = ("statement timeout", "lock timeout", "deadlock detected")

    @app.exception_handler(OperationalError)
    async def _db_busy(request: Request, exc: OperationalError) -> JSONResponse:
        """A row someone else is holding is a "try again", not a server fault.

        Generation runs several sequential model calls and can take minutes,
        and the whole request is one transaction — so while it runs, the
        session row it has written stays locked. Anything else touching that
        session (deleting the draft, advancing its status) waits behind it,
        gives up at the database's 30s ``statement_timeout``, and used to come
        back as a bare 500 with a stack trace in the log: the operator was
        told the server had broken when the true answer was "this resume is
        busy being written, come back in a moment".

        Reported as 409 because it is the same shape as every other conflict
        here — the request was valid and the state was wrong for it right now.
        """
        detail = str(getattr(exc, "orig", exc)).lower()
        if not any(marker in detail for marker in _LOCK_MARKERS):
            logger.exception("Database error on %s %s", request.method, request.url.path)
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "internal_error", "message": "An unexpected error occurred"}},
            )
        # One line, not a stack trace: nothing is broken and there is nothing
        # in the traceback that the message below does not already say.
        logger.warning(
            "Lock wait timed out on %s %s - another request is holding this row",
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "resource_busy",
                    "message": (
                        "This resume is busy — another operation on it is still running "
                        "(generation can take a few minutes). Try again once it finishes."
                    ),
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "An unexpected error occurred"}},
        )
