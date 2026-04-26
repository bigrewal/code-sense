"""Unit tests for input validators."""

import pytest
from fastapi import HTTPException
from app.validators import (
    derive_repo_name_from_path,
    validate_repo_name,
    validate_repo_path,
    validate_conversation_id,
    validate_job_id,
)


class TestValidateRepoName:
    def test_valid_repo_name(self):
        assert validate_repo_name("owner/repo") == "owner/repo"
        assert validate_repo_name("my-repo") == "my-repo"
        assert validate_repo_name("org_name/repo_name") == "org_name/repo_name"
        assert validate_repo_name("test123") == "test123"

    def test_path_traversal_blocked(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_repo_name("../../etc/passwd")
        assert exc_info.value.status_code == 400
        assert "invalid path sequences" in str(exc_info.value.detail).lower()

    def test_absolute_path_blocked(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_repo_name("/etc/passwd")
        assert exc_info.value.status_code == 400

    def test_invalid_characters_blocked(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_repo_name("test$repo")
        assert exc_info.value.status_code == 400
        assert "invalid characters" in str(exc_info.value.detail).lower()

    def test_empty_string_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_repo_name("")
        assert exc_info.value.status_code == 400

    def test_too_long_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_repo_name("a" * 256)
        assert exc_info.value.status_code == 400
        assert "too long" in str(exc_info.value.detail).lower()


class TestValidateRepoPath:
    def test_existing_directory_accepted(self, tmp_path):
        assert validate_repo_path(str(tmp_path)) == tmp_path.resolve()

    def test_missing_path_rejected(self, tmp_path):
        with pytest.raises(HTTPException) as exc_info:
            validate_repo_path(str(tmp_path / "missing"))
        assert exc_info.value.status_code == 404

    def test_file_path_rejected(self, tmp_path):
        file_path = tmp_path / "repo.py"
        file_path.write_text("print('x')", encoding="utf-8")
        with pytest.raises(HTTPException) as exc_info:
            validate_repo_path(str(file_path))
        assert exc_info.value.status_code == 400

    def test_derive_repo_name_from_path_sanitizes_folder_name(self, tmp_path):
        repo_path = tmp_path / "my repo!"
        repo_path.mkdir()
        assert derive_repo_name_from_path(repo_path) == "my-repo"


class TestValidateConversationId:
    def test_valid_object_id(self):
        valid_id = "507f1f77bcf86cd799439011"
        assert validate_conversation_id(valid_id) == valid_id

    def test_invalid_format(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_conversation_id("invalid-id")
        assert exc_info.value.status_code == 400
        assert "invalid" in str(exc_info.value.detail).lower()

    def test_wrong_length(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_conversation_id("507f1f77")
        assert exc_info.value.status_code == 400


class TestValidateJobId:
    def test_valid_uuid(self):
        valid_uuid = "123e4567-e89b-12d3-a456-426614174000"
        assert validate_job_id(valid_uuid) == valid_uuid

    def test_invalid_format(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_job_id("not-a-uuid")
        assert exc_info.value.status_code == 400
        assert "invalid" in str(exc_info.value.detail).lower()

    def test_uppercase_uuid_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_job_id("123E4567-E89B-12D3-A456-426614174000")
        assert exc_info.value.status_code == 400
