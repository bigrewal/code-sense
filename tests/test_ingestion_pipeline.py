import asyncio
from pathlib import Path

import pytest

from app.models.data_model import IngestionStage
from app.repo_ingestion_pipeline.file_state import FileEntry, RepoFileChanges
import app.repo_ingestion_pipeline as rp


class FakeMongo:
    def __init__(self):
        self.upserts = []
        self.ingested = []
        self.cancel_reason = None

    def upsert_ingestion_job(self, job, extra_fields=None, error=None):
        self.upserts.append({"job": job, "extra_fields": extra_fields, "error": error})

    def get_repo_file_states(self, _repo_name):
        return {}

    def add_ingested_repo(self, repo_name, job_id):
        self.ingested.append((repo_name, job_id))

    def cancel_active_ingestion_jobs(self, reason):
        self.cancel_reason = reason
        return 1

class DummyPrecheckStage:
    def __init__(self, **_kwargs):
        pass

    async def run(self, **_kwargs):
        return {"supported_file_count": 1}


class FailingPrecheckStage:
    def __init__(self, **_kwargs):
        pass

    async def run(self, **_kwargs):
        raise rp.PreIngestionAnalysisError("precheck failed")


class DummyMentalModel:
    def __init__(self, **_kwargs):
        pass

    async def run(self, **_kwargs):
        return (2, 1, 42)


class FailingMentalModel:
    def __init__(self, **_kwargs):
        pass

    async def run(self, **_kwargs):
        raise RuntimeError("mental failed")


class CancelledMentalModel:
    def __init__(self, **_kwargs):
        pass

    async def run(self, **_kwargs):
        raise asyncio.CancelledError


def _empty_changes() -> RepoFileChanges:
    return RepoFileChanges(
        current_files={"a.py": FileEntry(sha1="abc", language="python", supported=True)},
        new_files=set(),
        changed_files=set(),
        deleted_files=set(),
        unchanged_files={"a.py"},
    )


def test_initial_stage_is_precheck():
    fake_mongo = FakeMongo()
    assert fake_mongo is not None
    assert IngestionStage.PRECHECK.value == "precheck"


@pytest.mark.asyncio
async def test_start_ingestion_pipeline_happy_path(monkeypatch, tmp_path: Path):
    fake_mongo = FakeMongo()
    monkeypatch.setattr(rp, "get_mongo_client", lambda: fake_mongo)
    monkeypatch.setattr(rp, "GrokLLM", lambda: object())
    monkeypatch.setattr(rp, "build_repo_file_changes", lambda *_args, **_kwargs: _empty_changes())
    monkeypatch.setattr(rp, "PreIngestionAnalysisStage", DummyPrecheckStage)
    monkeypatch.setattr(rp, "MentalModelStage", DummyMentalModel)

    result = await rp.start_ingestion_pipeline(
        local_repo_path=tmp_path,
        repo_name="repo-a",
        job_id="job-1",
    )

    assert result == {"status": "completed", "job_id": "job-1"}
    assert fake_mongo.ingested == [("repo-a", "job-1")]


@pytest.mark.asyncio
async def test_start_ingestion_pipeline_precheck_failure(monkeypatch, tmp_path: Path):
    fake_mongo = FakeMongo()
    monkeypatch.setattr(rp, "get_mongo_client", lambda: fake_mongo)
    monkeypatch.setattr(rp, "GrokLLM", lambda: object())
    monkeypatch.setattr(rp, "build_repo_file_changes", lambda *_args, **_kwargs: _empty_changes())
    monkeypatch.setattr(rp, "PreIngestionAnalysisStage", FailingPrecheckStage)

    result = await rp.start_ingestion_pipeline(
        local_repo_path=tmp_path,
        repo_name="repo-a",
        job_id="job-2",
    )
    assert result is None
    assert any(u["job"].current_stage == IngestionStage.PRECHECK for u in fake_mongo.upserts)
    assert any(u["job"].status == "failed" for u in fake_mongo.upserts)


@pytest.mark.asyncio
async def test_start_ingestion_pipeline_mental_model_failure(monkeypatch, tmp_path: Path):
    fake_mongo = FakeMongo()
    monkeypatch.setattr(rp, "get_mongo_client", lambda: fake_mongo)
    monkeypatch.setattr(rp, "GrokLLM", lambda: object())
    monkeypatch.setattr(rp, "build_repo_file_changes", lambda *_args, **_kwargs: _empty_changes())
    monkeypatch.setattr(rp, "PreIngestionAnalysisStage", DummyPrecheckStage)
    monkeypatch.setattr(rp, "MentalModelStage", FailingMentalModel)

    result = await rp.start_ingestion_pipeline(
        local_repo_path=tmp_path,
        repo_name="repo-a",
        job_id="job-4",
    )
    assert result is None
    assert any(u["job"].current_stage == IngestionStage.MENTAL_MODEL for u in fake_mongo.upserts)


@pytest.mark.asyncio
async def test_start_ingestion_pipeline_marks_cancelled_on_task_cancellation(monkeypatch, tmp_path: Path):
    fake_mongo = FakeMongo()
    monkeypatch.setattr(rp, "get_mongo_client", lambda: fake_mongo)
    monkeypatch.setattr(rp, "GrokLLM", lambda: object())
    monkeypatch.setattr(rp, "build_repo_file_changes", lambda *_args, **_kwargs: _empty_changes())
    monkeypatch.setattr(rp, "PreIngestionAnalysisStage", DummyPrecheckStage)
    monkeypatch.setattr(rp, "MentalModelStage", CancelledMentalModel)

    with pytest.raises(asyncio.CancelledError):
        await rp.start_ingestion_pipeline(
            local_repo_path=tmp_path,
            repo_name="repo-a",
            job_id="job-5",
        )

    assert "job-5" in fake_mongo.cancel_reason
