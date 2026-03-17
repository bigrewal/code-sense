class DatabaseError(Exception):
    def __init__(self, message: str, *, code: str = "DB_ERROR", details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


class ConnectionError(DatabaseError):
    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("code", "CONNECTION_ERROR")
        super().__init__(message, **kwargs)


class QueryError(DatabaseError):
    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("code", "QUERY_ERROR")
        super().__init__(message, **kwargs)


class ValidationError(DatabaseError):
    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("code", "VALIDATION_ERROR")
        super().__init__(message, **kwargs)


class InvalidParameterError(ValidationError):
    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("code", "INVALID_PARAMETER")
        super().__init__(message, **kwargs)


class InvalidConnectionStringError(ValidationError):
    def __init__(self, message: str, **kwargs):
        kwargs.setdefault("code", "INVALID_CONNECTION_STRING")
        super().__init__(message, **kwargs)
