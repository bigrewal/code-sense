import asyncio
import hashlib
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

mock_db_module = types.SimpleNamespace(get_mongo_client=lambda: Mock())
sys.modules.setdefault('app.db', mock_db_module)

from app.lsp_reference_resolver.core.base_analyser import BaseLSPAnalyzer


class DummyCache:
    def __init__(self, sha_by_path=None):
        self.sha_by_path = dict(sha_by_path or {})

    def get_all_cached_file_paths(self):
        return set()

    def get_file_sha(self, rel_path):
        return self.sha_by_path.get(rel_path)

    def invalidate_by_definition_file(self, _def_path):
        return set()

    def delete_file_completely(self, _path):
        return None

    def delete_mappings_for_file(self, _path):
        return None

    def update_file_sha(self, rel_path, sha):
        self.sha_by_path[rel_path] = sha

    def store_mapping(self, _mapping):
        return None

    def commit(self):
        return None


class DummyAnalyzer(BaseLSPAnalyzer):
    def get_server_command(self):
        return ["dummy-lsp"]

    def get_file_extensions(self):
        return [".py"]

    def get_language_id(self):
        return "python"

    def ref_pos_extractor(self, text: str, path: Path):
        return []

    def needs_did_open(self):
        return False

    def get_max_concurrency(self):
        return 1

    def get_timeout_seconds(self):
        return 0.01

    def get_total_server_instances(self):
        return 1


def test_skips_lsp_start_when_no_files_changed(tmp_path, monkeypatch):
    file_path = tmp_path / "a.py"
    file_path.write_text("print('stable')\n", encoding="utf-8")
    repo_name = "repo"
    rel_path = f"{repo_name}/a.py"
    sha = hashlib.sha1(file_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()

    analyzer = DummyAnalyzer(tmp_path, repo_name, show_progress=False)
    analyzer.cache = DummyCache({rel_path: sha})

    mock_mongo = Mock()
    monkeypatch.setattr("app.lsp_reference_resolver.core.base_analyser.get_mongo_client", lambda: mock_mongo)

    analyzer.start_server = AsyncMock()

    asyncio.run(analyzer.analyze())

    analyzer.start_server.assert_not_called()
    assert analyzer.changed_files == set()


def test_starts_lsp_when_files_changed(tmp_path, monkeypatch):
    file_path = tmp_path / "a.py"
    file_path.write_text("print('changed')\n", encoding="utf-8")

    analyzer = DummyAnalyzer(tmp_path, "repo", show_progress=False)
    analyzer.cache = DummyCache()

    mock_mongo = Mock()
    monkeypatch.setattr("app.lsp_reference_resolver.core.base_analyser.get_mongo_client", lambda: mock_mongo)

    dummy_client = Mock()
    dummy_client.send_notification = AsyncMock()
    analyzer.start_server = AsyncMock(side_effect=lambda: analyzer.server_list.append(dummy_client))
    analyzer.shutdown = AsyncMock()
    analyzer._query_def_streaming = AsyncMock(return_value=None)

    asyncio.run(analyzer.analyze())

    analyzer.start_server.assert_called_once()
    assert "repo/a.py" in analyzer.changed_files
