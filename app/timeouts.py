"""
Timeout utilities for async operations.

Provides timeout protection for database, LLM, and streaming operations
to prevent requests from hanging indefinitely.
"""

import asyncio
from typing import TypeVar, Awaitable
from fastapi import HTTPException, status
from loguru import logger

T = TypeVar('T')


async def with_timeout(
    coro: Awaitable[T],
    timeout_seconds: float,
    operation_name: str = "Operation"
) -> T:
    """
    Execute coroutine with timeout protection.

    Args:
        coro: Coroutine to execute
        timeout_seconds: Timeout in seconds
        operation_name: Name for error message and logging

    Returns:
        Result of the coroutine

    Raises:
        HTTPException: 504 Gateway Timeout if operation exceeds timeout

    Example:
        result = await with_timeout(
            asyncio.to_thread(expensive_operation),
            timeout_seconds=30,
            operation_name="Database query"
        )
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.error(
            f"{operation_name} exceeded timeout: timeout_seconds={timeout_seconds}"
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"{operation_name} exceeded timeout of {timeout_seconds}s"
        )
