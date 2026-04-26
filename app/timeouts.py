import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from fastapi import HTTPException
from loguru import logger

T = TypeVar("T")


async def with_timeout(
    coro: Awaitable[T],
    timeout_seconds: float,
    operation_name: str = "Operation",
) -> T:
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        logger.error("{} exceeded timeout: timeout_seconds={}", operation_name, timeout_seconds)
        raise HTTPException(
            status_code=504,
            detail=f"{operation_name} exceeded timeout of {timeout_seconds}s",
        ) from exc
