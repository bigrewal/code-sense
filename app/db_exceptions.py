class DatabaseError(Exception):
    def __init__(self, message: str, *, code: str = "DB_ERROR", details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


class ConnectionError(DatabaseError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="CONNECTION_ERROR", **kwargs)


class QueryError(DatabaseError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="QUERY_ERROR", **kwargs)


class ValidationError(DatabaseError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="VALIDATION_ERROR", **kwargs)


class InvalidParameterError(ValidationError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="INVALID_PARAMETER", **kwargs)


class InvalidConnectionStringError(ValidationError):
    def __init__(self, message: str, **kwargs):
        super().__init__(message, code="INVALID_CONNECTION_STRING", **kwargs)
