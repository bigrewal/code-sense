from datetime import datetime, timezone

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Config


def _error_payload(request: Request, **extra) -> dict:
    if "error" in extra and "detail" not in extra:
        extra["detail"] = extra["error"]

    return jsonable_encoder(extra | {
        "path": str(request.url.path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": getattr(request.state, "request_id", None),
    })


def _validation_errors(exc: RequestValidationError) -> list[dict]:
    errors = []
    for raw_error in exc.errors():
        error = dict(raw_error)
        ctx = error.get("ctx")
        if isinstance(ctx, dict):
            error["ctx"] = {key: str(value) for key, value in ctx.items()}
        errors.append(error)
    return errors


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(
            request,
            error=exc.detail,
            status_code=exc.status_code,
        ),
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_error_payload(
            request,
            error="Validation error",
            details=_validation_errors(exc),
        ),
    )


async def general_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception("Unhandled exception: request_id={} path={} error={}", request_id, request.url.path, exc)

    error_detail = "Internal server error"
    if Config.LOG_DB_QUERIES:
        error_detail = f"{type(exc).__name__}: {exc}"

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_error_payload(request, error=error_detail),
    )
