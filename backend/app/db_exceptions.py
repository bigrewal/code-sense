class DatabaseError(Exception):
    code = "DB_ERROR"

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.details = details or {}


class ConnectionError(DatabaseError):
    code = "CONNECTION_ERROR"


class QueryError(DatabaseError):
    code = "QUERY_ERROR"


class ValidationError(DatabaseError):
    code = "VALIDATION_ERROR"


class InvalidParameterError(ValidationError):
    code = "INVALID_PARAMETER"


class InvalidConnectionStringError(ValidationError):
    code = "INVALID_CONNECTION_STRING"
