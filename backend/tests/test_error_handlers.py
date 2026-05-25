import json

import pytest
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request

from app.error_handlers import http_exception_handler, validation_exception_handler


def _request(path: str = "/v1/test") -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
    )
    request.state.request_id = "request-1"
    return request


@pytest.mark.asyncio
async def test_http_exception_payload_includes_error_and_detail():
    response = await http_exception_handler(_request(), HTTPException(status_code=404, detail="Not found"))
    payload = json.loads(response.body)

    assert response.status_code == 404
    assert payload["error"] == "Not found"
    assert payload["detail"] == "Not found"
    assert payload["request_id"] == "request-1"


@pytest.mark.asyncio
async def test_validation_exception_payload_is_json_serializable():
    exc = RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", "repo_name"),
                "msg": "Value error, bad repo",
                "input": {},
                "ctx": {"error": ValueError("bad repo")},
            }
        ]
    )

    response = await validation_exception_handler(_request(), exc)
    payload = json.loads(response.body)

    assert response.status_code == 422
    assert payload["error"] == "Validation error"
    assert payload["detail"] == "Validation error"
    assert payload["details"][0]["ctx"]["error"] == "bad repo"
