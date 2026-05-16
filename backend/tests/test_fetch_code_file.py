from pathlib import Path
import importlib

import pytest

fetch_code_file_module = importlib.import_module("app.tools.fetch_code_file")


class FakeDB:
    def __init__(self, repo_path: Path | None):
        self.repo_path = repo_path

    def get_repo_local_path(self, _repo_name):
        return str(self.repo_path) if self.repo_path else None


def test_fetch_code_file_uses_registered_repo_path(monkeypatch, tmp_path: Path):
    repo_dir = tmp_path / "external-repo"
    repo_dir.mkdir()
    code_file = repo_dir / "app.py"
    code_file.write_text("print('hello')", encoding="utf-8")

    monkeypatch.setattr(fetch_code_file_module, "get_db_client", lambda: FakeDB(repo_dir))

    assert fetch_code_file_module.fetch_code_file("repo-a", "app.py") == "print('hello')"


def test_fetch_code_file_blocks_repo_escape(monkeypatch, tmp_path: Path):
    repo_dir = tmp_path / "external-repo"
    repo_dir.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')", encoding="utf-8")

    monkeypatch.setattr(fetch_code_file_module, "get_db_client", lambda: FakeDB(repo_dir))

    with pytest.raises(ValueError):
        fetch_code_file_module.fetch_code_file("repo-a", "../outside.py")
