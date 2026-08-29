"""Domain exceptions and the handlers that turn them into friendly messages.

Spec section 24: the user sees an understandable message; the traceback goes to
the log file and never to the browser.
"""
from __future__ import annotations

import logging
import traceback
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

log = logging.getLogger(__name__)


class AppError(Exception):
    """Base for errors we can explain to the user."""
    status_code = 400
    hint: str | None = None

    def __init__(self, message: str, hint: str | None = None, **detail):
        super().__init__(message)
        self.message = message
        if hint:
            self.hint = hint
        self.detail = detail


class InvalidSearchError(AppError):
    status_code = 422
    hint = "Check your filters and Boolean expression, then try again."


class BooleanSyntaxError(InvalidSearchError):
    hint = ('Use AND / OR / NOT, parentheses, and "double quotes" for exact '
            'phrases. Example: ("medical billing" OR "revenue cycle") AND remote')


class SourceUnavailableError(AppError):
    status_code = 503
    hint = "The data source did not respond. Try again shortly or disable it in Settings."


class SourceNotConfiguredError(AppError):
    status_code = 400
    hint = "Add this source's credentials in Settings, or disable it."


class QuotaExhaustedError(AppError):
    status_code = 429
    hint = "This source's monthly call budget is spent. It resets automatically, or raise the limit in Settings."


class RateLimitedError(AppError):
    status_code = 429
    hint = "The source asked us to slow down. Collection will retry with a longer delay."


class NotFoundError(AppError):
    status_code = 404
    hint = "The record may have been deleted or never collected."


class ExportError(AppError):
    status_code = 500
    hint = "Check that the export folder exists and is writable in Settings."


def _envelope(status: int, message: str, hint: str | None,
              ref: str | None = None, detail: dict | None = None):
    body = {"error": {"message": message}}
    if hint:
        body["error"]["hint"] = hint
    if ref:
        body["error"]["reference"] = ref
    if detail:
        body["error"]["detail"] = detail
    return JSONResponse(status_code=status, content=body)


def register_error_handlers(app: FastAPI) -> None:

    @app.exception_handler(AppError)
    async def _app_error(_req: Request, exc: AppError):
        log.warning("AppError: %s | %s", exc.message, exc.detail)
        return _envelope(exc.status_code, exc.message, exc.hint,
                         detail=exc.detail or None)

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(_req: Request, exc: SQLAlchemyError):
        ref = uuid.uuid4().hex[:8]
        log.error("Database error [%s]\n%s", ref,
                  "".join(traceback.format_exception(exc)))
        return _envelope(
            500,
            "A database error occurred and the operation was rolled back.",
            "Nothing was saved. If this repeats, check the log file in Settings.",
            ref,
        )

    @app.exception_handler(Exception)
    async def _unhandled(_req: Request, exc: Exception):
        ref = uuid.uuid4().hex[:8]
        log.error("Unhandled error [%s]\n%s", ref,
                  "".join(traceback.format_exception(exc)))
        return _envelope(
            500,
            "Something went wrong while handling that request.",
            f"The details were written to the log (reference {ref}).",
            ref,
        )
