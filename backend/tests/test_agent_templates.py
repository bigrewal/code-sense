import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_codex_code_sense_agent_template_is_valid_toml():
    template_path = REPO_ROOT / ".codex" / "agents" / "code-sense.toml"
    data = tomllib.loads(template_path.read_text(encoding="utf-8"))

    assert data["name"] == "code_sense"
    assert data["description"]
    assert "start_host_agent_ingestion" in data["developer_instructions"]
    assert "db_path" in data["developer_instructions"]
    assert "build_repo_context" in data["developer_instructions"]
    assert "get_subdir_briefs" in data["developer_instructions"]
