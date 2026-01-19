"""Request logging middleware."""

import time
import uuid
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from loguru import logger
from typing import Callable


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        start_time = time.time()

        logger.info(
            f"Request started: request_id={request_id}, method={request.method}, "
            f"path={request.url.path}, client={request.client.host if request.client else 'unknown'}"
        )

        try:
            response = await call_next(request)
            duration_ms = (time.time() - start_time) * 1000

            logger.info(
                f"Request completed: request_id={request_id}, method={request.method}, "
                f"path={request.url.path}, status_code={response.status_code}, duration_ms={duration_ms:.2f}"
            )

            response.headers["X-Request-ID"] = request_id
            return response

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(
                f"Request failed: request_id={request_id}, method={request.method}, "
                f"path={request.url.path}, error={str(e)}, duration_ms={duration_ms:.2f}"
            )
            raise
