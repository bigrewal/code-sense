"""
Custom exception hierarchy for database operations.

This module defines specific exceptions for database errors, following the pattern
from data_model.py and enabling precise error handling throughout the application.
"""


class DatabaseError(Exception):
    """Base exception for all database-related errors."""

    def __init__(self, message: str, *, code: str = "DB_ERROR", details: dict = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


# Connection Errors (typically retryable)

class ConnectionError(DatabaseError):
    """Base exception for database connection errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="CONNECTION_ERROR", **kwargs)


class ConnectionPoolExhaustedError(ConnectionError):
    """Exception raised when connection pool is exhausted."""

    def __init__(self, message: str = "Connection pool exhausted", **kwargs):
        super().__init__(message, code="POOL_EXHAUSTED", **kwargs)


class ConnectionTimeoutError(ConnectionError):
    """Exception raised when connection attempt times out."""

    def __init__(self, message: str = "Connection timeout", **kwargs):
        super().__init__(message, code="CONNECTION_TIMEOUT", **kwargs)


class AuthenticationError(ConnectionError):
    """Exception raised when database authentication fails."""

    def __init__(self, message: str = "Authentication failed", **kwargs):
        super().__init__(message, code="AUTH_FAILED", **kwargs)


# Query Errors (may be retryable depending on type)

class QueryError(DatabaseError):
    """Base exception for database query errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="QUERY_ERROR", **kwargs)


class QueryTimeoutError(QueryError):
    """Exception raised when query execution times out."""

    def __init__(self, message: str = "Query timeout", **kwargs):
        super().__init__(message, code="QUERY_TIMEOUT", **kwargs)


class DeadlockError(QueryError):
    """Exception raised when database deadlock occurs (retryable)."""

    def __init__(self, message: str = "Database deadlock detected", **kwargs):
        super().__init__(message, code="DEADLOCK", **kwargs)


class TransactionError(QueryError):
    """Exception raised when transaction fails."""

    def __init__(self, message: str = "Transaction failed", **kwargs):
        super().__init__(message, code="TRANSACTION_ERROR", **kwargs)


# Validation Errors (non-retryable)

class ValidationError(DatabaseError):
    """Base exception for validation errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="VALIDATION_ERROR", **kwargs)


class InvalidParameterError(ValidationError):
    """Exception raised when invalid parameter is provided."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="INVALID_PARAMETER", **kwargs)


class InvalidConnectionStringError(ValidationError):
    """Exception raised when connection string is invalid."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="INVALID_CONNECTION_STRING", **kwargs)


class InvalidCollectionNameError(ValidationError):
    """Exception raised when collection name is invalid."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="INVALID_COLLECTION_NAME", **kwargs)


# Operation Errors

class BatchOperationError(DatabaseError):
    """Exception raised when batch operation fails."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="BATCH_OPERATION_ERROR", **kwargs)


class IndexCreationError(DatabaseError):
    """Exception raised when index creation fails."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="INDEX_CREATION_ERROR", **kwargs)


# Health Check Errors

class HealthCheckError(DatabaseError):
    """Base exception for health check errors."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="HEALTH_CHECK_ERROR", **kwargs)


class Neo4jHealthCheckError(HealthCheckError):
    """Exception raised when Neo4j health check fails."""

    def __init__(self, message: str = "Neo4j health check failed", **kwargs):
        super().__init__(message, code="NEO4J_HEALTH_CHECK_ERROR", **kwargs)


class MongoHealthCheckError(HealthCheckError):
    """Exception raised when MongoDB health check fails."""

    def __init__(self, message: str = "MongoDB health check failed", **kwargs):
        super().__init__(message, code="MONGO_HEALTH_CHECK_ERROR", **kwargs)


# Retryable exception types (for retry decorator)
RETRYABLE_EXCEPTIONS = (
    ConnectionError,
    ConnectionTimeoutError,
    ConnectionPoolExhaustedError,
    QueryTimeoutError,
    DeadlockError,
)
