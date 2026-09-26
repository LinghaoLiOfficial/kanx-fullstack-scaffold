import logging
from time import perf_counter
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from backend_foundation.core.config import Settings
from backend_foundation.core.request_context import (
    reset_request_id,
    set_business_context,
    set_request_id,
    set_trace_context,
)

logger = logging.getLogger(__name__)
REQUEST_ID_HEADER = "X-Request-ID"
MAX_REQUEST_ID_LENGTH = 128


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "-"))


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": _request_id(request),
                "details": details,
            }
        },
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "").strip()
        request_id = incoming if 0 < len(incoming) <= MAX_REQUEST_ID_LENGTH else str(uuid4())
        request.state.request_id = request_id
        token = set_request_id(request_id)
        parts = request.headers.get("traceparent", "").split("-")
        trace_id = parts[1] if len(parts) > 1 and len(parts[1]) == 32 else uuid4().hex
        span_id = uuid4().hex[:16]
        set_trace_context(trace_id, span_id)
        started = perf_counter()
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            response.headers["traceparent"] = f"00-{trace_id}-{span_id}-01"
            logger.info(
                "request.completed",
                extra={
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status": response.status_code,
                },
            )
            return response
        finally:
            reset_request_id(token)
            set_business_context()
            set_trace_context("-", "-")


def install_error_handling(app: FastAPI, settings: Settings) -> None:
    app.add_middleware(RequestContextMiddleware)

    async def http_error(
        request: Request, error: HTTPException | StarletteHTTPException
    ) -> JSONResponse:
        message = error.detail if isinstance(error.detail, str) else "Request failed"
        details = None if isinstance(error.detail, str) else error.detail
        return error_response(
            request,
            status_code=error.status_code,
            code=f"http_{error.status_code}",
            message=message,
            details=details,
        )

    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return error_response(
            request,
            status_code=422,
            code="validation_error",
            message="Request validation failed",
            details=error.errors(),
        )

    async def unexpected_error(request: Request, error: Exception) -> JSONResponse:
        logger.exception("Unhandled request error", exc_info=error)
        message = "Internal server error" if settings.production else str(error)
        return error_response(
            request,
            status_code=500,
            code="internal_error",
            message=message,
        )

    handler = cast(Any, http_error)
    app.add_exception_handler(HTTPException, handler)
    app.add_exception_handler(StarletteHTTPException, handler)
    app.add_exception_handler(RequestValidationError, cast(Any, validation_error))
    app.add_exception_handler(Exception, cast(Any, unexpected_error))
