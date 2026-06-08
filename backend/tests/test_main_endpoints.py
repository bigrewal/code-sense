import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError

from app import main


async def _passthrough_with_timeout(coro, **_kwargs):
    return await coro


class FakeDB:
    def __init__(self):
        self.deleted_repo_name = None
        self.job = None
        self.delete_job_ok = True
        self.health = {"status": "healthy", "response_time_ms": 1.0}
        self.active_job = None
        self.cancel_reason = None
        self.repo_paths = {}

    def create_conversation(self, repo_name: str):
        return {
            "conversation_id": "507f1f77bcf86cd799439011",
            "repo_name": repo_name,
            "created_at": datetime.now(timezone.utc),
        }

    def list_conversations(self, repo_name=None, limit=50, offset=0):
        _ = (repo_name, limit, offset)
        return [
            {
                "_id": "507f1f77bcf86cd799439011",
                "repo_name": "repo-a",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "title": "First",
            }
        ]

    def conversation_exists(self, conversation_id):
        return conversation_id == "507f1f77bcf86cd799439011"

    def list_conversation_messages(self, conversation_id, limit=200):
        _ = (conversation_id, limit)
        return [{"role": "user", "content": "hi", "created_at": datetime.now(timezone.utc)}]

    def delete_conversation(self, conversation_id):
        if conversation_id != "507f1f77bcf86cd799439011":
            raise KeyError("Conversation not found")

    def upsert_ingestion_job(self, *args, **kwargs):
        _ = (args, kwargs)
        return None

    def delete_repo_data(self, repo_name):
        self.deleted_repo_name = repo_name
        return {"total_deleted": 7}

    def get_job_status(self, job_id):
        if job_id == "00000000-0000-0000-0000-000000000000":
            return None
        return {"job_id": job_id, "status": "completed"}

    def list_jobs(self, **_kwargs):
        return ([{"job_id": "jid-1", "status": "completed"}], 1)

    def list_ingested_repos(self):
        return ["repo-a", "repo-b"]

    def list_brief_subdir_options(self, repo_name):
        assert repo_name == "repo-a"
        return [
            {"path": "backend", "file_count": 3},
            {"path": "backend/app", "file_count": 2},
        ]

    def get_repo_local_path(self, repo_name):
        return self.repo_paths.get(repo_name)

    def get_job(self, _job_id):
        return self.job

    def delete_job(self, _job_id):
        return self.delete_job_ok

    def health_check(self):
        return self.health

    def get_active_ingestion_job(self):
        return self.active_job

    def cancel_active_ingestion_jobs(self, reason):
        self.cancel_reason = reason
        return 1

    def close(self):
        return None


@pytest.fixture
def fake_db(monkeypatch):
    db_client = FakeDB()
    monkeypatch.setattr(main, "with_timeout", _passthrough_with_timeout)
    monkeypatch.setattr(main, "get_db_client", lambda: db_client)
    return db_client


@pytest.mark.asyncio
async def test_create_conversation(fake_db):
    resp = await main.create_conversation(main.ConversationCreateRequest(repo_name="repo-a"))
    assert resp.repo_name == "repo-a"
    assert resp.conversation_id == "507f1f77bcf86cd799439011"


@pytest.mark.asyncio
async def test_list_conversations(fake_db):
    rows = await main.list_conversations(repo_name="repo-a", limit=5, offset=0)
    assert len(rows) == 1
    assert rows[0].title == "First"


@pytest.mark.asyncio
async def test_list_conversation_messages_not_found(fake_db):
    with pytest.raises(HTTPException) as exc:
        await main.list_conversation_messages("507f1f77bcf86cd799439012")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_conversation_messages_found(fake_db):
    resp = await main.list_conversation_messages("507f1f77bcf86cd799439011")
    assert resp.conversation_id == "507f1f77bcf86cd799439011"
    assert resp.messages[0].content == "hi"


@pytest.mark.asyncio
async def test_delete_conversation_404(fake_db):
    with pytest.raises(HTTPException) as exc:
        await main.delete_conversation("507f1f77bcf86cd799439012")
    assert exc.value.status_code == 404


def test_chat_request_rejects_empty_fields():
    with pytest.raises(ValidationError):
        main.ChatRequest(conversation_id="", message="x")
    with pytest.raises(ValidationError):
        main.ChatRequest(conversation_id="id", message="")


def test_stateless_chat_request_rejects_empty_fields():
    with pytest.raises(ValidationError):
        main.StatelessChatRequest(repo_name="", message="x")
    with pytest.raises(ValidationError):
        main.StatelessChatRequest(repo_name="repo-a", message="")


@pytest.mark.asyncio
async def test_chat_404_when_conversation_missing(fake_db):
    req = main.ChatRequest(conversation_id="507f1f77bcf86cd799439012", message="hi")
    with pytest.raises(HTTPException) as exc:
        await main.chat(req)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_chat_returns_streaming_response(fake_db):
    req = main.ChatRequest(conversation_id="507f1f77bcf86cd799439011", message="hi")
    resp = await main.chat(req)
    assert resp.media_type == "application/x-ndjson"


@pytest.mark.asyncio
async def test_browse_local_repos_lists_allowed_directory(fake_db, monkeypatch, tmp_path: Path):
    repo_dir = tmp_path / "repo-a"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    (tmp_path / "file.txt").write_text("not a directory", encoding="utf-8")
    (tmp_path / ".hidden-repo").mkdir()

    monkeypatch.setattr(main.Config, "REPO_BROWSER_ROOTS", [str(tmp_path)])

    resp = await main.browse_local_repos()

    assert resp.path == str(tmp_path.resolve())
    assert resp.parent_path is None
    assert resp.roots[0].path == str(tmp_path.resolve())
    assert [(entry.name, entry.path, entry.has_git) for entry in resp.entries] == [
        ("repo-a", str(repo_dir.resolve()), True)
    ]


@pytest.mark.asyncio
async def test_browse_local_repos_parent_path_within_root(fake_db, monkeypatch, tmp_path: Path):
    child_dir = tmp_path / "parent" / "child"
    child_dir.mkdir(parents=True)

    monkeypatch.setattr(main.Config, "REPO_BROWSER_ROOTS", [str(tmp_path)])

    resp = await main.browse_local_repos(path=str(child_dir))

    assert resp.path == str(child_dir.resolve())
    assert resp.parent_path == str(child_dir.parent.resolve())


@pytest.mark.asyncio
async def test_browse_local_repos_blocks_outside_root(fake_db, monkeypatch, tmp_path: Path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()

    monkeypatch.setattr(main.Config, "REPO_BROWSER_ROOTS", [str(allowed)])

    with pytest.raises(HTTPException) as exc:
        await main.browse_local_repos(path=str(outside))

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_ingest_repo_not_found(fake_db, monkeypatch, tmp_path: Path):
    missing = tmp_path / "repo-a"
    monkeypatch.setattr(main, "get_repo_path", lambda _repo_name: missing)
    with pytest.raises(HTTPException) as exc:
        await main.ingest_repo(BackgroundTasks(), main.IngestRequest(repo_name="repo-a"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_ingest_repo_success(fake_db, monkeypatch, tmp_path: Path):
    repo_dir = tmp_path / "repo-a"
    repo_dir.mkdir()
    monkeypatch.setattr(main, "get_repo_path", lambda _repo_name: repo_dir)

    scheduled = {}

    def _fake_add_task(func, **kwargs):
        scheduled["func"] = func
        scheduled["kwargs"] = kwargs

    background = BackgroundTasks()
    monkeypatch.setattr(background, "add_task", _fake_add_task)

    resp = await main.ingest_repo(background, main.IngestRequest(repo_name="repo-a"))
    assert resp.status == "queued"
    assert scheduled["func"] is main._run_ingestion_job
    assert scheduled["kwargs"]["repo_name"] == "repo-a"
    assert scheduled["kwargs"]["local_repo_path"] == repo_dir.resolve()


@pytest.mark.asyncio
async def test_ingest_repo_path_success_derives_repo_name(fake_db, monkeypatch, tmp_path: Path):
    repo_dir = tmp_path / "repo a"
    repo_dir.mkdir()
    monkeypatch.setattr(main.Config, "REPO_BROWSER_ROOTS", [str(tmp_path)])

    scheduled = {}

    def _fake_add_task(func, **kwargs):
        scheduled["func"] = func
        scheduled["kwargs"] = kwargs

    background = BackgroundTasks()
    monkeypatch.setattr(background, "add_task", _fake_add_task)

    resp = await main.ingest_repo(background, main.IngestRequest(repo_path=str(repo_dir)))

    assert resp.status == "queued"
    assert resp.repo_name == "repo-a"
    assert scheduled["kwargs"]["repo_name"] == "repo-a"
    assert scheduled["kwargs"]["local_repo_path"] == repo_dir.resolve()


@pytest.mark.asyncio
async def test_ingest_repo_path_explicit_name_conflict(fake_db, monkeypatch, tmp_path: Path):
    repo_dir = tmp_path / "repo-a"
    other_dir = tmp_path / "other"
    repo_dir.mkdir()
    other_dir.mkdir()
    fake_db.repo_paths["repo-a"] = str(other_dir)
    monkeypatch.setattr(main.Config, "REPO_BROWSER_ROOTS", [str(tmp_path)])

    with pytest.raises(HTTPException) as exc:
        await main.ingest_repo(
            BackgroundTasks(),
            main.IngestRequest(repo_name="repo-a", repo_path=str(repo_dir)),
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_ingest_repo_path_blocked_outside_browser_roots(fake_db, monkeypatch, tmp_path: Path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setattr(main.Config, "REPO_BROWSER_ROOTS", [str(allowed)])

    with pytest.raises(HTTPException) as exc:
        await main.ingest_repo(
            BackgroundTasks(),
            main.IngestRequest(repo_path=str(outside)),
        )

    assert exc.value.status_code == 400


def test_ingest_request_requires_target():
    with pytest.raises(ValidationError):
        main.IngestRequest()


@pytest.mark.asyncio
async def test_ingest_repo_conflict_when_active_job_exists(fake_db, monkeypatch, tmp_path: Path):
    repo_dir = tmp_path / "repo-a"
    repo_dir.mkdir()
    monkeypatch.setattr(main, "get_repo_path", lambda _repo_name: repo_dir)
    fake_db.active_job = {"job_id": "already-running"}

    with pytest.raises(HTTPException) as exc:
        await main.ingest_repo(BackgroundTasks(), main.IngestRequest(repo_name="repo-a"))

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_run_ingestion_job_marks_cancelled_on_cancelled_error(fake_db, monkeypatch, tmp_path: Path):
    async def _cancelled_pipeline(**_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "start_ingestion_pipeline", _cancelled_pipeline)

    with pytest.raises(asyncio.CancelledError):
        await main._run_ingestion_job(
            local_repo_path=tmp_path,
            repo_name="repo-a",
            job_id="job-cancelled",
        )

    assert "job-cancelled" in fake_db.cancel_reason


@pytest.mark.asyncio
async def test_delete_repo_invalid_path(fake_db, monkeypatch, tmp_path: Path):
    safe_base = tmp_path / "safe"
    safe_base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.setattr(main.Config, "BASE_REPO_DIR", str(safe_base))
    monkeypatch.setattr(main, "get_repo_path", lambda _repo_name: outside)

    with pytest.raises(HTTPException) as exc:
        await main.delete_repo("repo-a", delete_files=True)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_repo_success(fake_db, monkeypatch, tmp_path: Path):
    safe_base = tmp_path / "safe"
    repo_dir = safe_base / "repo-a"
    repo_dir.mkdir(parents=True)

    monkeypatch.setattr(main.Config, "BASE_REPO_DIR", str(safe_base))
    monkeypatch.setattr(main, "get_repo_path", lambda _repo_name: repo_dir)

    resp = await main.delete_repo("repo-a", delete_files=False)
    assert resp["total_deleted"] == 7
    assert fake_db.deleted_repo_name == "repo-a"


@pytest.mark.asyncio
async def test_get_job_not_found(fake_db):
    with pytest.raises(HTTPException) as exc:
        await main.get_job("00000000-0000-0000-0000-000000000000")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_job_found(fake_db):
    resp = await main.get_job("00000000-0000-0000-0000-000000000001")
    assert resp["job_id"] == "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_list_jobs(fake_db):
    resp = await main.list_jobs(status="completed", repo_name="repo-a", limit=10, skip=0)
    assert resp["count"] == 1
    assert resp["total"] == 1


@pytest.mark.asyncio
async def test_list_repos(fake_db):
    resp = await main.list_repos()
    assert resp["repos"] == ["repo-a", "repo-b"]


@pytest.mark.asyncio
async def test_list_repo_subdirs(fake_db):
    resp = await main.list_repo_subdirs("repo-a")

    assert resp["repo_name"] == "repo-a"
    assert resp["subdirs"] == [
        {"path": "backend", "file_count": 3},
        {"path": "backend/app", "file_count": 2},
    ]


@pytest.mark.asyncio
async def test_delete_job_status_paths(fake_db):
    fake_db.job = None
    with pytest.raises(HTTPException) as exc_not_found:
        await main.delete_job("00000000-0000-0000-0000-000000000001")
    assert exc_not_found.value.status_code == 404

    fake_db.job = {"status": "running"}
    with pytest.raises(HTTPException) as exc_conflict:
        await main.delete_job("00000000-0000-0000-0000-000000000001")
    assert exc_conflict.value.status_code == 409

    fake_db.job = {"status": "completed"}
    fake_db.delete_job_ok = False
    with pytest.raises(HTTPException) as exc_failed:
        await main.delete_job("00000000-0000-0000-0000-000000000001")
    assert exc_failed.value.status_code == 500

    fake_db.delete_job_ok = True
    resp = await main.delete_job("00000000-0000-0000-0000-000000000001")
    assert resp["deleted"] is True


@pytest.mark.asyncio
async def test_health_healthy_and_unhealthy(fake_db, monkeypatch):
    healthy_resp = await main.health()
    assert healthy_resp.status_code == 200

    fake_db.health = {"status": "unhealthy", "error": "down"}
    unhealthy_resp = await main.health()
    assert unhealthy_resp.status_code == 503

    class BoomDB:
        def health_check(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(main, "get_db_client", lambda: BoomDB())
    error_resp = await main.health()
    assert error_resp.status_code == 503
