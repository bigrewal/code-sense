import pytest

from app import db
from app.db_exceptions import InvalidParameterError, QueryError
from app.models.data_model import IngestionJobStatus, IngestionStage, IngestionStageStatus


def _client(tmp_path):
    client = db.SQLiteClient.__new__(db.SQLiteClient)
    client._db_path = str(tmp_path / "test.sqlite3")
    client._conn = None
    client._thread_lock = db.threading.RLock()
    client._initialized = True
    client.connect()
    return client


def test_validate_repo_name_rejects_bad_values():
    with pytest.raises(InvalidParameterError):
        db._validate_repo_name("")
    with pytest.raises(InvalidParameterError):
        db._validate_repo_name("bad${name}")


def test_delete_repo_data_and_file_states(tmp_path):
    client = _client(tmp_path)
    created = client.create_conversation("r")
    client.persist_message(
        conversation_id=created["conversation_id"],
        role="user",
        content="hello",
        message_type="chat_message",
    )
    client.upsert_repo_file_states("r", [{"file_path": "a.py", "sha1": "a", "supported": True}])
    client.upsert_mental_model_document(
        repo_name="r",
        file_path="a.py",
        document_type="BRIEF_FILE_OVERVIEW",
        data="brief",
        sha1="a",
    )
    client.add_ingested_repo("r", "job-1")
    client.upsert_ingestion_job(
        IngestionJobStatus(
            job_id="job-1",
            repo_name="r",
            status="completed",
            current_stage=IngestionStage.MENTAL_MODEL,
            stage_status={IngestionStage.MENTAL_MODEL: IngestionStageStatus.COMPLETED},
        )
    )

    assert client.delete_repo_file_states("r", []) == 0
    client.upsert_repo_file_states("r", [{"file_path": "b.py", "sha1": "b", "supported": True}])
    assert client.delete_repo_file_states("r", ["b.py"]) == 1

    result = client.delete_repo_data("r")
    assert result["repo_name"] == "r"
    assert result["collections_processed"] == 6
    assert result["total_deleted"] >= 5


def test_delete_repo_data_wraps_errors(tmp_path):
    client = _client(tmp_path)
    client.close()
    with pytest.raises(QueryError):
        client.delete_repo_data("r")


def test_create_list_and_delete_conversation_flows(tmp_path):
    client = _client(tmp_path)

    created = client.create_conversation("repo-a")
    assert created["repo_name"] == "repo-a"
    assert len(created["conversation_id"]) == 24

    client.persist_message(
        conversation_id=created["conversation_id"],
        role="user",
        content="hi",
        message_type="chat_message",
    )

    rows = client.list_conversations(repo_name="repo-a", limit=500, offset=-1)
    assert len(rows) == 1

    conversation_id = created["conversation_id"]
    assert client.conversation_exists(conversation_id) is True
    assert len(client.list_conversation_messages(conversation_id=conversation_id, limit=1000)) == 1
    client.delete_conversation(conversation_id)

    with pytest.raises(ValueError):
        client.conversation_exists("bad-id")
    with pytest.raises(ValueError):
        client.delete_conversation("bad-id")
    with pytest.raises(KeyError):
        client.delete_conversation("507f1f77bcf86cd799439012")


def test_create_conversation_wraps_errors(tmp_path):
    client = _client(tmp_path)
    client.close()
    with pytest.raises(QueryError):
        client.create_conversation("repo-a")


def test_ingestion_job_and_repo_helpers(tmp_path):
    client = _client(tmp_path)
    local_path = str(tmp_path / "repo-a")
    client.add_ingested_repo("repo-a", "j1", local_path=local_path)
    client.upsert_repo_file_states(
        "repo-a",
        [{"file_path": "a.py", "sha1": "abc", "language": "python", "supported": True}],
    )

    job = IngestionJobStatus(
        job_id="j1",
        repo_name="repo-a",
        status="running",
        current_stage=IngestionStage.PRECHECK,
        stage_status={
            IngestionStage.PRECHECK: {
                "status": "completed",
                "metrics": {"supported_file_count": 1, "secret_metric": 99},
            }
        },
    )
    client.upsert_ingestion_job(job, extra_fields={"operation": "x"})

    status = client.get_job_status("j1")
    assert status["stages"]["precheck"]["metrics"] == {"supported_file_count": 1}
    assert client.get_job_status("missing") is None

    listed, total = client.list_jobs(include_total=True)
    assert len(listed) == 1
    assert total == 1

    assert client.get_active_ingestion_job()["job_id"] == "j1"
    assert client.cancel_active_ingestion_jobs("stop") == 1
    assert client.get_job_status("j1")["status"] == "cancelled"

    assert client.list_ingested_repos() == ["repo-a"]
    assert client.is_repo_ingested("repo-a") is True
    assert client.get_repo_local_path("repo-a") == local_path

    updated_path = str(tmp_path / "repo-a-renamed")
    client.add_ingested_repo("repo-a", "j2", local_path=updated_path)
    assert client.get_repo_local_path("repo-a") == updated_path

    assert client.get_repo_file_states("repo-a") == {
        "a.py": {
            "file_path": "a.py",
            "sha1": "abc",
            "language": "python",
            "supported": True,
            "token_count": 0,
        }
    }


def test_mental_model_helpers(tmp_path):
    client = _client(tmp_path)
    client.upsert_mental_model_document(
        repo_name="repo-a",
        file_path="a.py",
        document_type="BRIEF_FILE_OVERVIEW",
        data="brief",
        sha1="abc",
    )
    client.upsert_mental_model_document(
        repo_name="repo-a",
        file_path="b.py",
        document_type="IGNORED_FILE",
        data="IGNORE",
        sha1="def",
    )
    client.upsert_repo_context("repo-a", "repo context")

    assert client.get_brief_file_overview("repo-a", "a.py") == "brief"
    assert client.get_critical_file_paths("repo-a") == ["a.py"]
    assert client.get_repo_context("repo-a") == "repo context"
    assert client.count_mental_model_documents(repo_name="repo-a", document_type="IGNORED_FILE") == 1

    cached = client.find_mental_model_document(
        repo_name="repo-a",
        file_path="a.py",
        document_types=["BRIEF_FILE_OVERVIEW"],
        sha1="abc",
    )
    assert cached["data"] == "brief"

    deleted = client.delete_mental_model_documents(
        repo_name="repo-a",
        file_paths=["a.py"],
        document_types=["BRIEF_FILE_OVERVIEW"],
    )
    assert deleted == 1
    assert client.get_brief_file_overview("repo-a", "a.py") == ""


def test_list_brief_file_overviews_for_subdir_matches_repo_relative_prefix(tmp_path):
    client = _client(tmp_path)
    for file_path, data in [
        ("src/app.py", "app brief"),
        ("src/utils/helpers.py", "helpers brief"),
        ("src_extra/app.py", "wrong prefix brief"),
    ]:
        client.upsert_mental_model_document(
            repo_name="repo-a",
            file_path=file_path,
            document_type="BRIEF_FILE_OVERVIEW",
            data=data,
            sha1=file_path,
        )

    docs = client.list_brief_file_overviews_for_subdir("repo-a", "src")

    assert [doc["file_path"] for doc in docs] == ["src/app.py", "src/utils/helpers.py"]


def test_list_brief_subdir_options_counts_critical_files_under_each_prefix(tmp_path):
    client = _client(tmp_path)
    for file_path in [
        "backend/app/main.py",
        "backend/app/db.py",
        "backend/tests/test_main.py",
        "frontend/src/App.jsx",
        "root.py",
    ]:
        client.upsert_mental_model_document(
            repo_name="repo-a",
            file_path=file_path,
            document_type="BRIEF_FILE_OVERVIEW",
            data=f"{file_path} brief",
            sha1=file_path,
        )

    assert client.list_brief_subdir_options("repo-a") == [
        {"path": "backend", "file_count": 3},
        {"path": "backend/app", "file_count": 2},
        {"path": "backend/tests", "file_count": 1},
        {"path": "frontend", "file_count": 1},
        {"path": "frontend/src", "file_count": 1},
    ]


def test_job_delete_health_and_close_paths(tmp_path):
    client = _client(tmp_path)
    client.upsert_ingestion_job(
        IngestionJobStatus(
            job_id="j1",
            repo_name="repo-a",
            status="completed",
            current_stage=IngestionStage.PRECHECK,
            stage_status={IngestionStage.PRECHECK: IngestionStageStatus.COMPLETED},
        )
    )

    assert client.get_job("j1")["job_id"] == "j1"
    assert client.delete_job("j1") is True
    assert client.delete_job("missing") is False

    healthy = client.health_check()
    assert healthy["status"] == "healthy"
    assert healthy["collection_count"] >= 6

    client.close()
    unhealthy = client.health_check()
    assert unhealthy["status"] == "unhealthy"
