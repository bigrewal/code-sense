"""
Database metrics collector for observability and monitoring.

This module tracks database operation counts, durations, error rates,
and slow queries for production monitoring and debugging.
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class OperationMetrics:
    """Metrics for a single database operation."""

    operation_name: str
    start_time: float
    end_time: Optional[float] = None
    success: bool = False
    error_type: Optional[str] = None
    duration_ms: Optional[float] = None

    def complete(self, success: bool = True, error: Optional[Exception] = None):
        """Mark the operation as complete."""
        self.end_time = time.time()
        self.success = success
        self.duration_ms = (self.end_time - self.start_time) * 1000

        if error:
            self.error_type = type(error).__name__


class DatabaseMetrics:
    """
    In-memory metrics collector for database operations.

    Tracks operation counts, durations, errors, and slow queries.
    Provides summary for health check and metrics endpoints.

    Example:
        metrics = DatabaseMetrics()
        operation = metrics.start_operation("batch_create_nodes")
        try:
            # ... perform operation
            metrics.end_operation(operation, success=True)
        except Exception as e:
            metrics.end_operation(operation, success=False, error=e)
            raise
    """

    def __init__(self, slow_query_threshold_ms: float = 1000.0):
        """
        Initialize metrics collector.

        Args:
            slow_query_threshold_ms: Threshold in milliseconds for slow query detection
        """
        self.slow_query_threshold_ms = slow_query_threshold_ms
        self.operations: List[OperationMetrics] = []
        self.operation_counts: Dict[str, int] = defaultdict(int)
        self.error_counts: Dict[str, int] = defaultdict(int)
        self.slow_queries: List[OperationMetrics] = []

        # Keep only recent operations to prevent memory growth
        self.max_operations_history = 10000

    def start_operation(self, operation_name: str) -> OperationMetrics:
        """
        Start tracking an operation.

        Args:
            operation_name: Name of the operation (e.g., "batch_create_nodes")

        Returns:
            OperationMetrics object to pass to end_operation()
        """
        metric = OperationMetrics(
            operation_name=operation_name,
            start_time=time.time(),
        )

        self.operation_counts[operation_name] += 1

        return metric

    def end_operation(
        self,
        metric: OperationMetrics,
        success: bool = True,
        error: Optional[Exception] = None,
    ):
        """
        Complete tracking an operation.

        Args:
            metric: OperationMetrics object from start_operation()
            success: Whether the operation succeeded
            error: Exception if operation failed
        """
        metric.complete(success=success, error=error)

        # Track errors
        if not success and error:
            error_type = type(error).__name__
            self.error_counts[error_type] += 1

        # Track slow queries
        if metric.duration_ms and metric.duration_ms > self.slow_query_threshold_ms:
            self.slow_queries.append(metric)

            # Keep only recent slow queries
            if len(self.slow_queries) > 100:
                self.slow_queries = self.slow_queries[-100:]

        # Add to operations history
        self.operations.append(metric)

        # Trim operations history if too large
        if len(self.operations) > self.max_operations_history:
            self.operations = self.operations[-self.max_operations_history :]

    def get_summary(self) -> Dict[str, Any]:
        """
        Get metrics summary for health check endpoint.

        Returns:
            Dict with metrics summary including:
                - total_operations: Total number of operations tracked
                - operation_counts: Count by operation name
                - error_counts: Count by error type
                - slow_query_count: Number of slow queries detected
                - recent_operations: Last 10 operations with details
        """
        # Calculate average durations per operation type
        operation_durations: Dict[str, List[float]] = defaultdict(list)
        for op in self.operations:
            if op.duration_ms is not None:
                operation_durations[op.operation_name].append(op.duration_ms)

        avg_durations = {
            op_name: sum(durations) / len(durations)
            for op_name, durations in operation_durations.items()
            if durations
        }

        # Recent operations (last 10)
        recent_ops = [
            {
                "operation": op.operation_name,
                "duration_ms": round(op.duration_ms, 2) if op.duration_ms else None,
                "success": op.success,
                "error_type": op.error_type,
            }
            for op in self.operations[-10:]
        ]

        # Recent slow queries (last 10)
        recent_slow = [
            {
                "operation": op.operation_name,
                "duration_ms": round(op.duration_ms, 2) if op.duration_ms else None,
            }
            for op in self.slow_queries[-10:]
        ]

        return {
            "total_operations": len(self.operations),
            "operation_counts": dict(self.operation_counts),
            "error_counts": dict(self.error_counts),
            "slow_query_count": len(self.slow_queries),
            "slow_query_threshold_ms": self.slow_query_threshold_ms,
            "average_durations_ms": {
                op_name: round(dur, 2) for op_name, dur in avg_durations.items()
            },
            "recent_operations": recent_ops,
            "recent_slow_queries": recent_slow,
        }

    def reset(self):
        """Reset all metrics (useful for testing)."""
        self.operations.clear()
        self.operation_counts.clear()
        self.error_counts.clear()
        self.slow_queries.clear()
