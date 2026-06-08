from pathlib import Path

import pytest

from app import db
from app.repo_ingestion_pipeline.mental_model_gen import MENTAL_MODEL_TYPES
import app.host_agent_ingestion as host_agent_ingestion


def _read_project_db(db_path):
    return db.create_sqlite_client(db_path)


def test_host_agent_ingestion_flow_persists_repo_context(tmp_path: Path):
    repo = tmp_path / "repo-a"
    repo.mkdir()
    (repo / "app.py").write_text(
        "class App:\n"
        "    def run(self):\n"
        "        return 'ok'\n",
        encoding="utf-8",
    )

    started = host_agent_ingestion.start_host_agent_ingestion(str(repo))

    assert started["repo_name"] == "repo-a"
    assert started["db_path"] == str(repo / ".codesense" / "code_sense.sqlite3")
    assert Path(started["db_path"]).exists()
    assert started["pending_files"] == 1
    assert started["file_changes"]["new_files"] == ["app.py"]

    batch = host_agent_ingestion.get_next_file_batch(started["job_id"], db_path=started["db_path"])
    assert batch["pending_files"] == 1
    assert batch["files"][0]["file_path"] == "app.py"
    assert batch["files"][0]["absolute_path"] == str(repo / "app.py")

    saved = host_agent_ingestion.save_file_briefs(
        started["job_id"],
        [
            {
                "file_path": "app.py",
                "classification": "critical",
                "summary": "`app.py` owns the sample app. It does this by defining App and App.run. "
                "It interacts upstream with callers and downstream with no external modules.",
            }
        ],
        db_path=started["db_path"],
    )

    assert saved["pending_files"] == 0
    assert saved["critical_files"] == 1

    completed = host_agent_ingestion.build_repo_context(started["job_id"], db_path=started["db_path"])
    assert completed["status"] == "completed"
    client = _read_project_db(started["db_path"])
    try:
        assert client.get_repo_local_path("repo-a") == str(repo.resolve())
        assert "sample app" in client.get_repo_context("repo-a")
    finally:
        client.close()


def test_save_file_briefs_persists_ignored_file(tmp_path: Path):
    repo = tmp_path / "repo-a"
    repo.mkdir()
    (repo / "thin.py").write_text("VALUE = 1\n", encoding="utf-8")

    started = host_agent_ingestion.start_host_agent_ingestion(str(repo))

    saved = host_agent_ingestion.save_file_briefs(
        started["job_id"],
        [{"file_path": "thin.py", "summary": "IGNORE"}],
        db_path=started["db_path"],
    )

    assert saved["files_ignored"] == 1
    client = _read_project_db(started["db_path"])
    try:
        doc = client.find_mental_model_document(
            repo_name="repo-a",
            file_path="thin.py",
            document_types=[MENTAL_MODEL_TYPES["IGNORED"]],
        )
        assert doc["data"] == "IGNORE"
    finally:
        client.close()


def test_build_repo_context_requires_no_pending_files(tmp_path: Path):
    repo = tmp_path / "repo-a"
    repo.mkdir()
    (repo / "app.py").write_text("print('pending')\n", encoding="utf-8")

    started = host_agent_ingestion.start_host_agent_ingestion(str(repo))

    with pytest.raises(ValueError, match="still pending"):
        host_agent_ingestion.build_repo_context(started["job_id"], db_path=started["db_path"])


def test_get_subdir_briefs_returns_combined_context(tmp_path: Path):
    db_path = tmp_path / ".codesense" / "code_sense.sqlite3"
    client = db.create_sqlite_client(str(db_path))
    try:
        for file_path, data in [
            ("backend/app/main.py", "`backend/app/main.py` defines API routes."),
            ("backend/app/chat_service.py", "`backend/app/chat_service.py` handles chat streams."),
            ("frontend/src/App.jsx", "`frontend/src/App.jsx` renders the shell."),
        ]:
            client.upsert_mental_model_document(
                repo_name="repo-a",
                file_path=file_path,
                document_type=MENTAL_MODEL_TYPES["BRIEF"],
                data=data,
                sha1=file_path,
            )
    finally:
        client.close()

    result = host_agent_ingestion.get_subdir_briefs("repo-a", "@backend/app", db_path=str(db_path))

    assert result["repo_name"] == "repo-a"
    assert result["subdir_path"] == "backend/app"
    assert result["file_count"] == 2
    assert result["files"] == ["backend/app/chat_service.py", "backend/app/main.py"]
    assert "SUBDIRECTORY @backend/app FILE BRIEFS (2 files):" in result["context"]
