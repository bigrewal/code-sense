"""
Retry decorator with exponential backoff for database operations.

This module provides retry logic for transient database failures, following
the pattern from llm_grok.py with configurable attempts and backoff.
"""

import asyncio
import logging
import time
from functools import wraps
from typing import Callable, Tuple, Type, TypeVar, cast

from .db_exceptions import RETRYABLE_EXCEPTIONS

logger = logging.getLogger(__name__)

# Type variables for generic function signatures
F = TypeVar('F', bound=Callable)


def with_retry(
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    backoff_multiplier: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = RETRYABLE_EXCEPTIONS,
) -> Callable[[F], F]:
    """
    Decorator for synchronous functions with retry and exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay in seconds before first retry (default: 1.0)
        backoff_multiplier: Multiplier for delay on each retry (default: 2.0)
        exceptions: Tuple of exception types to retry on (default: RETRYABLE_EXCEPTIONS)

    Returns:
        Decorated function with retry logic

    Example:
        @with_retry(max_attempts=3, initial_delay=1.0)
        def query_database():
            # Database operation that may fail transiently
            pass
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)

                except exceptions as e:
                    last_exception = e

                    # Last attempt - raise the exception
                    if attempt == max_attempts - 1:
                        logger.error(
                            "Max retry attempts (%d) reached for %s: %s",
                            max_attempts,
                            func.__name__,
                            str(e),
                        )
                        raise

                    # Calculate wait time with exponential backoff
                    wait_time = initial_delay * (backoff_multiplier ** attempt)

                    logger.warning(
                        "Retryable error in %s (attempt %d/%d): %s. Retrying in %.2fs",
                        func.__name__,
                        attempt + 1,
                        max_attempts,
                        str(e),
                        wait_time,
                    )

                    time.sleep(wait_time)

                except Exception as e:
                    # Non-retryable exception - raise immediately
                    logger.error(
                        "Non-retryable error in %s: %s",
                        func.__name__,
                        str(e),
                    )
                    raise

            # Should never reach here, but raise last exception if we do
            if last_exception:
                raise last_exception

        return cast(F, wrapper)

    return decorator


def with_retry_async(
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    backoff_multiplier: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = RETRYABLE_EXCEPTIONS,
) -> Callable[[F], F]:
    """
    Decorator for asynchronous functions with retry and exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay in seconds before first retry (default: 1.0)
        backoff_multiplier: Multiplier for delay on each retry (default: 2.0)
        exceptions: Tuple of exception types to retry on (default: RETRYABLE_EXCEPTIONS)

    Returns:
        Decorated async function with retry logic

    Example:
        @with_retry_async(max_attempts=3, initial_delay=1.0)
        async def async_query_database():
            # Async database operation that may fail transiently
            pass
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)

                except exceptions as e:
                    last_exception = e

                    # Last attempt - raise the exception
                    if attempt == max_attempts - 1:
                        logger.error(
                            "Max retry attempts (%d) reached for %s: %s",
                            max_attempts,
                            func.__name__,
                            str(e),
                        )
                        raise

                    # Calculate wait time with exponential backoff
                    wait_time = initial_delay * (backoff_multiplier ** attempt)

                    logger.warning(
                        "Retryable error in %s (attempt %d/%d): %s. Retrying in %.2fs",
                        func.__name__,
                        attempt + 1,
                        max_attempts,
                        str(e),
                        wait_time,
                    )

                    await asyncio.sleep(wait_time)

                except Exception as e:
                    # Non-retryable exception - raise immediately
                    logger.error(
                        "Non-retryable error in %s: %s",
                        func.__name__,
                        str(e),
                    )
                    raise

            # Should never reach here, but raise last exception if we do
            if last_exception:
                raise last_exception

        return cast(F, wrapper)

    return decorator
