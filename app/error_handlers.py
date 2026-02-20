from datetime import datetime, timezone

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Config


def _base_error_payload(request: Request) -> dict:
    return {
        "path": str(request.url.path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": getattr(request.state, "request_id", None),
    }


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status_code": exc.status_code,
            **_base_error_payload(request),
        },
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "Validation error",
            "details": exc.errors(),
            **_base_error_payload(request),
        },
    )


async def general_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception("Unhandled exception: request_id={} path={} error={}", request_id, request.url.path, exc)

    error_detail = "Internal server error"
    if Config.LOG_DB_QUERIES:
        error_detail = f"{type(exc).__name__}: {exc}"

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": error_detail,
            **_base_error_payload(request),
        },
    )
