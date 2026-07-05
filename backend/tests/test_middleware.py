import pytest
from starlette.requests import Request
from starlette.responses import Response

from app import middleware
from app.middleware import (
    RequestLoggingMiddleware,
    _is_job_status_poll,
    _log_level_for_status_code,
)


def test_job_status_poll_detection():
    assert _is_job_status_poll("GET", "/v1/jobs/6742c9b1-646d-4348-a5fa-9d02e8623cc3")
    assert _is_job_status_poll("get", "/v1/jobs/job-id/")
    assert not _is_job_status_poll("DELETE", "/v1/jobs/job-id")
    assert not _is_job_status_poll("GET", "/v1/jobs")
    assert not _is_job_status_poll("GET", "/v1/jobs/job-id/details")


@pytest.mark.asyncio
async def test_successful_job_status_poll_is_silent_but_keeps_request_id(monkeypatch):
    info_logs = []
    monkeypatch.setattr(middleware.logger, "info", lambda *args, **kwargs: info_logs.append(args))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/jobs/6742c9b1-646d-4348-a5fa-9d02e8623cc3",
            "raw_path": b"/v1/jobs/6742c9b1-646d-4348-a5fa-9d02e8623cc3",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
            "root_path": "",
        }
    )
    request_middleware = RequestLoggingMiddleware(lambda *_args, **_kwargs: None)

    async def call_next(_request):
        return Response(status_code=200)

    response = await request_middleware.dispatch(request, call_next)

    assert info_logs == []
    assert response.headers["X-Request-ID"] == request.state.request_id


def test_log_level_for_status_code():
    assert _log_level_for_status_code(200) == "info"
    assert _log_level_for_status_code(302) == "info"
    assert _log_level_for_status_code(404) == "warning"
    assert _log_level_for_status_code(409) == "warning"
    assert _log_level_for_status_code(500) == "error"
    assert _log_level_for_status_code(503) == "error"
