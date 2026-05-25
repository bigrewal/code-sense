import time
import uuid
from typing import Callable

from fastapi import Request
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware


def _log_level_for_status_code(status_code: int) -> str:
    if status_code >= 500:
        return "error"
    if status_code >= 400:
        return "warning"
    return "info"


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        start_time = time.time()

        logger.info(
            "input request_id={} method={} path={} query={} client={}",
            request_id,
            request.method,
            request.url.path,
            str(request.url.query),
            request.client.host if request.client else "unknown",
        )

        try:
            response = await call_next(request)
            duration_ms = (time.time() - start_time) * 1000
            log = getattr(logger, _log_level_for_status_code(response.status_code))
            log(
                "output request_id={} status_code={} duration_ms={:.2f}",
                request_id,
                response.status_code,
                duration_ms,
            )

            response.headers["X-Request-ID"] = request_id
            return response

        except Exception:
            duration_ms = (time.time() - start_time) * 1000
            logger.exception(
                "output request_id={} status_code=500 duration_ms={:.2f}",
                request_id,
                duration_ms,
            )
            raise
