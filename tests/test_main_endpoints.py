from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from app import main


async def _passthrough_with_timeout(coro, **_kwargs):
    return await coro


class FakeMongo:
    def __init__(self):
        self.deleted_repo_name = None
        self.job = None
        self.delete_job_ok = True
        self.health = {"status": "healthy", "response_time_ms": 1.0}

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

    def get_job(self, _job_id):
        return self.job

    def delete_job(self, _job_id):
        return self.delete_job_ok

    def health_check(self):
        return self.health

    def close(self):
        return None


@pytest.fixture
def fake_mongo(monkeypatch):
    mongo = FakeMongo()
    monkeypatch.setattr(main, "with_timeout", _passthrough_with_timeout)
    monkeypatch.setattr(main, "get_mongo_client", lambda: mongo)
    return mongo


@pytest.mark.asyncio
async def test_create_conversation(fake_mongo):
    resp = await main.create_conversation(main.ConversationCreateRequest(repo_name="repo-a"))
    assert resp.repo_name == "repo-a"
    assert resp.conversation_id == "507f1f77bcf86cd799439011"


@pytest.mark.asyncio
async def test_list_conversations(fake_mongo):
    rows = await main.list_conversations(repo_name="repo-a", limit=5, offset=0)
    assert len(rows) == 1
    assert rows[0].title == "First"


@pytest.mark.asyncio
async def test_list_conversation_messages_not_found(fake_mongo):
    with pytest.raises(HTTPException) as exc:
        await main.list_conversation_messages("507f1f77bcf86cd799439012")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_conversation_messages_found(fake_mongo):
    resp = await main.list_conversation_messages("507f1f77bcf86cd799439011")
    assert resp.conversation_id == "507f1f77bcf86cd799439011"
    assert resp.messages[0].content == "hi"


@pytest.mark.asyncio
async def test_delete_conversation_404(fake_mongo):
    with pytest.raises(HTTPException) as exc:
        await main.delete_conversation("507f1f77bcf86cd799439012")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_chat_validation_errors():
    with pytest.raises(HTTPException):
        await main.chat(main.ChatRequest(conversation_id="", message="x"))
    with pytest.raises(HTTPException):
        await main.chat(main.ChatRequest(conversation_id="id", message=""))


@pytest.mark.asyncio
async def test_stateless_chat_validation_errors():
    with pytest.raises(HTTPException):
        await main.stateless_chat(main.StatelessChatRequest(repo_name="", message="x"))
    with pytest.raises(HTTPException):
        await main.stateless_chat(main.StatelessChatRequest(repo_name="repo-a", message=""))


@pytest.mark.asyncio
async def test_ingest_repo_not_found(fake_mongo, monkeypatch, tmp_path: Path):
    missing = tmp_path / "repo-a"
    monkeypatch.setattr(main, "get_repo_path", lambda _repo_name: missing)
    with pytest.raises(HTTPException) as exc:
        await main.ingest_repo(BackgroundTasks(), main.IngestRequest(repo_name="repo-a"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_ingest_repo_success(fake_mongo, monkeypatch, tmp_path: Path):
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
    assert resp["status"] == "queued"
    assert scheduled["func"] is main.start_ingestion_pipeline
    assert scheduled["kwargs"]["repo_name"] == "repo-a"


@pytest.mark.asyncio
async def test_delete_repo_invalid_path(fake_mongo, monkeypatch, tmp_path: Path):
    safe_base = tmp_path / "safe"
    safe_base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.setattr(main.Config, "BASE_REPO_DIR", str(safe_base))
    monkeypatch.setattr(main, "get_repo_path", lambda _repo_name: outside)

    with pytest.raises(HTTPException) as exc:
        await main.delete_repo("repo-a", delete_files=False)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_repo_cache_error(fake_mongo, monkeypatch, tmp_path: Path):
    safe_base = tmp_path / "safe"
    repo_dir = safe_base / "repo-a"
    repo_dir.mkdir(parents=True)

    monkeypatch.setattr(main.Config, "BASE_REPO_DIR", str(safe_base))
    monkeypatch.setattr(main, "get_repo_path", lambda _repo_name: repo_dir)
    monkeypatch.setattr(main, "_delete_repo_lsp_cache_files", lambda _path: (_ for _ in ()).throw(RuntimeError("x")))

    with pytest.raises(HTTPException) as exc:
        await main.delete_repo("repo-a", delete_files=False)
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_delete_repo_success(fake_mongo, monkeypatch, tmp_path: Path):
    safe_base = tmp_path / "safe"
    repo_dir = safe_base / "repo-a"
    repo_dir.mkdir(parents=True)

    monkeypatch.setattr(main.Config, "BASE_REPO_DIR", str(safe_base))
    monkeypatch.setattr(main, "get_repo_path", lambda _repo_name: repo_dir)

    resp = await main.delete_repo("repo-a", delete_files=False)
    assert resp["total_deleted"] == 7
    assert fake_mongo.deleted_repo_name == "repo-a"


@pytest.mark.asyncio
async def test_get_status_by_job_not_found(fake_mongo):
    with pytest.raises(HTTPException) as exc:
        await main.get_status(job_id="00000000-0000-0000-0000-000000000000")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_status_list(fake_mongo):
    resp = await main.get_status(status="completed", repo_name="repo-a", limit=10, skip=0)
    assert resp["count"] == 1
    assert resp["total"] == 1


@pytest.mark.asyncio
async def test_list_repos(fake_mongo):
    resp = await main.list_repos()
    assert resp["repos"] == ["repo-a", "repo-b"]


@pytest.mark.asyncio
async def test_delete_job_status_paths(fake_mongo):
    fake_mongo.job = None
    with pytest.raises(HTTPException) as exc_not_found:
        await main.delete_job("00000000-0000-0000-0000-000000000001")
    assert exc_not_found.value.status_code == 404

    fake_mongo.job = {"status": "running"}
    with pytest.raises(HTTPException) as exc_conflict:
        await main.delete_job("00000000-0000-0000-0000-000000000001")
    assert exc_conflict.value.status_code == 409

    fake_mongo.job = {"status": "completed"}
    fake_mongo.delete_job_ok = False
    with pytest.raises(HTTPException) as exc_failed:
        await main.delete_job("00000000-0000-0000-0000-000000000001")
    assert exc_failed.value.status_code == 500

    fake_mongo.delete_job_ok = True
    resp = await main.delete_job("00000000-0000-0000-0000-000000000001")
    assert resp["deleted"] is True


@pytest.mark.asyncio
async def test_health_healthy_and_unhealthy(fake_mongo, monkeypatch):
    healthy_resp = await main.health()
    assert healthy_resp.status_code == 200

    fake_mongo.health = {"status": "unhealthy", "error": "down"}
    unhealthy_resp = await main.health()
    assert unhealthy_resp.status_code == 503

    class BoomMongo:
        def health_check(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(main, "get_mongo_client", lambda: BoomMongo())
    error_resp = await main.health()
    assert error_resp.status_code == 503
