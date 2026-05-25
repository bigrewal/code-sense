from app.middleware import _log_level_for_status_code


def test_log_level_for_status_code():
    assert _log_level_for_status_code(200) == "info"
    assert _log_level_for_status_code(302) == "info"
    assert _log_level_for_status_code(404) == "warning"
    assert _log_level_for_status_code(409) == "warning"
    assert _log_level_for_status_code(500) == "error"
    assert _log_level_for_status_code(503) == "error"
