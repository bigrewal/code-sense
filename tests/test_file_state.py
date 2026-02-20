import hashlib
from pathlib import Path

from app.repo_ingestion_pipeline.file_state import build_repo_file_changes


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_build_repo_file_changes_tracks_file_deltas(tmp_path: Path):
    _write(tmp_path / "src/new_file.py", "print('new')\n")
    _write(tmp_path / "src/changed.py", "print('changed v2')\n")
    _write(tmp_path / "src/unchanged.py", "print('same')\n")

    previous_state = {
        "src/changed.py": {"sha1": "stale-sha"},
        "src/unchanged.py": {
            "sha1": hashlib.sha1((tmp_path / "src/unchanged.py").read_bytes()).hexdigest(),
        },
        "src/deleted.py": {"sha1": "deleted-sha"},
    }

    changes = build_repo_file_changes(tmp_path, previous_state)

    assert "src/new_file.py" in changes.new_files
    assert "src/changed.py" in changes.changed_files
    assert "src/deleted.py" in changes.deleted_files
    assert "src/unchanged.py" in changes.unchanged_files
    assert changes.current_files["src/new_file.py"].supported is True


def test_build_repo_file_changes_excludes_ignored_hidden_and_sqlite(tmp_path: Path):
    _write(tmp_path / "src/kept.py", "print('ok')\n")
    _write(tmp_path / "tests/ignored.py", "print('ignore')\n")
    _write(tmp_path / ".hidden/ignored.py", "print('ignore')\n")
    _write(tmp_path / "db.sqlite", "x")

    changes = build_repo_file_changes(tmp_path, previous_state={})

    assert "src/kept.py" in changes.current_files
    assert "tests/ignored.py" not in changes.current_files
    assert ".hidden/ignored.py" not in changes.current_files
    assert "db.sqlite" not in changes.current_files
