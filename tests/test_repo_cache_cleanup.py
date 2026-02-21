from pathlib import Path

from app.main import _delete_repo_lsp_cache_files


def test_delete_repo_lsp_cache_files_removes_sqlite_artifacts(tmp_path: Path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    sqlite_file = repo_path / ".codesense_ref_index.sqlite"
    wal_file = repo_path / ".codesense_ref_index.sqlite-wal"
    shm_file = repo_path / ".codesense_ref_index.sqlite-shm"
    for f in (sqlite_file, wal_file, shm_file):
        f.write_text("x", encoding="utf-8")

    _delete_repo_lsp_cache_files(repo_path)

    assert not sqlite_file.exists()
    assert not wal_file.exists()
    assert not shm_file.exists()


def test_delete_repo_lsp_cache_files_is_noop_when_missing(tmp_path: Path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    _delete_repo_lsp_cache_files(repo_path)

    assert not (repo_path / ".codesense_ref_index.sqlite").exists()
