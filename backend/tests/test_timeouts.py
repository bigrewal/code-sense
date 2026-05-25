"""Unit tests for timeout utilities."""

import pytest
import asyncio
from fastapi import HTTPException
from app.timeouts import with_timeout


class TestWithTimeout:
    @pytest.mark.asyncio
    async def test_operation_completes_within_timeout(self):
        async def fast_operation():
            await asyncio.sleep(0.1)
            return "success"

        result = await with_timeout(fast_operation(), timeout_seconds=1.0, operation_name="Fast op")
        assert result == "success"

    @pytest.mark.asyncio
    async def test_operation_exceeds_timeout(self):
        async def slow_operation():
            await asyncio.sleep(2.0)
            return "should not reach here"

        with pytest.raises(HTTPException) as exc_info:
            await with_timeout(slow_operation(), timeout_seconds=0.5, operation_name="Slow op")

        assert exc_info.value.status_code == 504
        assert "Slow op" in str(exc_info.value.detail)
        assert "0.5s" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_operation_with_exception(self):
        async def failing_operation():
            raise ValueError("Operation failed")

        with pytest.raises(ValueError, match="Operation failed"):
            await with_timeout(failing_operation(), timeout_seconds=1.0, operation_name="Failing op")

    @pytest.mark.asyncio
    async def test_zero_timeout(self):
        async def instant_operation():
            return "done"

        # Even instant operations might exceed 0s timeout
        with pytest.raises(HTTPException) as exc_info:
            await with_timeout(instant_operation(), timeout_seconds=0, operation_name="Zero timeout")

        assert exc_info.value.status_code == 504
