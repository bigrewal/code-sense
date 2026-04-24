from pathlib import Path

import pytest

from app.repo_ingestion_pipeline.file_state import build_repo_file_changes
from app.repo_ingestion_pipeline.pre_ingestion_analysis import PreIngestionAnalysisStage


class DummyLLM:
    def count_tokens(self, text: str) -> int:
        return len(text.split())


class FakeDB:
    def get_repo_file_states(self, _repo_name):
        return {}

    def upsert_repo_file_states(self, _repo_name, _rows):
        return None

    def delete_repo_file_states(self, _repo_name, _file_paths):
        return 0


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_scan_retokenizes_when_file_becomes_supported(monkeypatch, tmp_path: Path):
    _write(tmp_path / "MAIN.agc", "TC INTPRET\nCAF OCTAL\n")

    previous_state = {
        "MAIN.agc": {
            "sha1": build_repo_file_changes(tmp_path, {}).current_files["MAIN.agc"].sha1,
            "language": None,
            "supported": False,
            "token_count": 0,
        }
    }

    monkeypatch.setattr(
        "app.repo_ingestion_pipeline.pre_ingestion_analysis.get_db_client",
        lambda: FakeDB(),
    )

    stage = PreIngestionAnalysisStage(llm_grok=DummyLLM(), repo_name="Apollo-11")
    file_changes = build_repo_file_changes(tmp_path, previous_state)

    metrics, scan_stats, state_rows = await stage.scan(
        repo_path=tmp_path,
        file_changes=file_changes,
        previous_state=previous_state,
    )

    assert file_changes.unchanged_files == {"MAIN.agc"}
    assert scan_stats["total_files_tokenized"] == 1
    assert metrics[0].supported is True
    assert metrics[0].tokens > 0
    assert state_rows[0]["language"] == "assembly"
    assert state_rows[0]["supported"] is True
    assert state_rows[0]["token_count"] > 0


@pytest.mark.asyncio
async def test_scan_retokenizes_when_supported_file_has_zero_cached_tokens(monkeypatch, tmp_path: Path):
    _write(tmp_path / "MAIN.agc", "TC INTPRET\nCAF OCTAL\n")

    previous_state = {
        "MAIN.agc": {
            "sha1": build_repo_file_changes(tmp_path, {}).current_files["MAIN.agc"].sha1,
            "language": "assembly",
            "supported": True,
            "token_count": 0,
        }
    }

    monkeypatch.setattr(
        "app.repo_ingestion_pipeline.pre_ingestion_analysis.get_db_client",
        lambda: FakeDB(),
    )

    stage = PreIngestionAnalysisStage(llm_grok=DummyLLM(), repo_name="Apollo-11")
    file_changes = build_repo_file_changes(tmp_path, previous_state)

    metrics, scan_stats, state_rows = await stage.scan(
        repo_path=tmp_path,
        file_changes=file_changes,
        previous_state=previous_state,
    )

    assert file_changes.unchanged_files == {"MAIN.agc"}
    assert scan_stats["total_files_tokenized"] == 1
    assert metrics[0].tokens > 0
    assert state_rows[0]["token_count"] > 0


@pytest.mark.asyncio
async def test_scan_does_not_persist_unsupported_files(monkeypatch, tmp_path: Path):
    _write(tmp_path / "MAIN.agc", "TC INTPRET\nCAF OCTAL\n")
    _write(tmp_path / "README.xyz", "not supported\n")

    monkeypatch.setattr(
        "app.repo_ingestion_pipeline.pre_ingestion_analysis.get_db_client",
        lambda: FakeDB(),
    )

    stage = PreIngestionAnalysisStage(llm_grok=DummyLLM(), repo_name="Apollo-11")
    file_changes = build_repo_file_changes(tmp_path, previous_state={})

    metrics, _scan_stats, state_rows = await stage.scan(
        repo_path=tmp_path,
        file_changes=file_changes,
        previous_state={},
    )

    assert any(m.file_path == "README.xyz" and m.supported is False for m in metrics)
    assert all(row["file_path"] != "README.xyz" for row in state_rows)
    assert any(row["file_path"] == "MAIN.agc" for row in state_rows)
