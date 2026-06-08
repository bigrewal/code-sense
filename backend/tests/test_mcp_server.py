import json

import pytest

from app import mcp_server


def _tool_payload(result):
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


@pytest.mark.asyncio
async def test_mcp_exposes_host_agent_tools():
    tools = await mcp_server.mcp.list_tools()
    tool_names = {tool.name for tool in tools}

    assert tool_names == {
        "start_host_agent_ingestion",
        "get_next_file_batch",
        "save_file_briefs",
        "build_repo_context",
        "get_repo_context",
        "get_file_brief",
        "get_subdir_briefs",
    }


@pytest.mark.asyncio
async def test_mcp_start_host_agent_ingestion_delegates(monkeypatch):
    def fake_start_host_agent_ingestion_service(repo_path, repo_name=None):
        assert repo_path == "/tmp/repo-a"
        assert repo_name == "repo-a"
        return {
            "job_id": "job-1",
            "repo_name": repo_name,
            "db_path": "/tmp/repo-a/.codesense/code_sense.sqlite3",
            "pending_files": 3,
        }

    monkeypatch.setattr(
        mcp_server,
        "start_host_agent_ingestion_service",
        fake_start_host_agent_ingestion_service,
    )

    content = await mcp_server.mcp.call_tool(
        "start_host_agent_ingestion",
        {"repo_path": "/tmp/repo-a", "repo_name": "repo-a"},
    )

    payload = _tool_payload(content)
    assert payload["job_id"] == "job-1"
    assert payload["pending_files"] == 3
    assert payload["db_path"].endswith(".codesense/code_sense.sqlite3")


@pytest.mark.asyncio
async def test_mcp_save_file_briefs_delegates(monkeypatch):
    def fake_save_file_briefs_service(job_id, file_results, repo_path=None, db_path=None):
        assert job_id == "job-1"
        assert file_results == [{"file_path": "app.py", "summary": "IGNORE"}]
        assert repo_path is None
        assert db_path == "/tmp/repo-a/.codesense/code_sense.sqlite3"
        return {"job_id": job_id, "saved": 1}

    monkeypatch.setattr(mcp_server, "save_file_briefs_service", fake_save_file_briefs_service)

    content = await mcp_server.mcp.call_tool(
        "save_file_briefs",
        {
            "job_id": "job-1",
            "db_path": "/tmp/repo-a/.codesense/code_sense.sqlite3",
            "file_results": [{"file_path": "app.py", "summary": "IGNORE"}],
        },
    )

    assert _tool_payload(content)["saved"] == 1


@pytest.mark.asyncio
async def test_mcp_get_subdir_briefs_delegates(monkeypatch):
    def fake_get_subdir_briefs_service(repo_name, subdir_path, repo_path=None, db_path=None):
        assert repo_name == "repo-a"
        assert subdir_path == "backend/app"
        assert repo_path is None
        assert db_path == "/tmp/repo-a/.codesense/code_sense.sqlite3"
        return {
            "repo_name": repo_name,
            "subdir_path": subdir_path,
            "file_count": 1,
            "files": ["backend/app/main.py"],
            "context": "`backend/app/main.py` defines the API.",
        }

    monkeypatch.setattr(mcp_server, "get_subdir_briefs_service", fake_get_subdir_briefs_service)

    content = await mcp_server.mcp.call_tool(
        "get_subdir_briefs",
        {
            "repo_name": "repo-a",
            "subdir_path": "backend/app",
            "db_path": "/tmp/repo-a/.codesense/code_sense.sqlite3",
        },
    )

    assert _tool_payload(content)["file_count"] == 1
