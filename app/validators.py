"""
Input validation utilities for API endpoints.

Provides validation functions for user input to prevent security vulnerabilities
and ensure data integrity.
"""

import re
from fastapi import HTTPException, status


def validate_repo_name(repo_name: str) -> str:
    """
    Validate repo_name to prevent path traversal attacks.

    Allowed format: alphanumeric, hyphens, underscores, forward slashes
    Examples: "owner/repo", "my-repo", "org/sub/repo"

    Args:
        repo_name: Repository name to validate

    Returns:
        str: Validated repo_name

    Raises:
        HTTPException: 400 if repo_name is invalid
    """
    if not repo_name or not isinstance(repo_name, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="repo_name must be non-empty string"
        )

    # Prevent path traversal
    if ".." in repo_name or repo_name.startswith("/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="repo_name contains invalid path sequences"
        )

    # Allow only safe characters: alphanumeric, hyphen, underscore, forward slash
    if not re.match(r'^[a-zA-Z0-9_/-]+$', repo_name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="repo_name contains invalid characters (allowed: a-z, A-Z, 0-9, -, _, /)"
        )

    # Limit length
    if len(repo_name) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="repo_name too long (max 255 characters)"
        )

    return repo_name


def validate_conversation_id(conversation_id: str) -> str:
    """
    Validate MongoDB ObjectId format.

    Args:
        conversation_id: Conversation ID to validate

    Returns:
        str: Validated conversation_id

    Raises:
        HTTPException: 400 if not valid ObjectId format
    """
    if not conversation_id or not isinstance(conversation_id, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="conversation_id must be non-empty string"
        )

    if not re.match(r'^[a-f0-9]{24}$', conversation_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid conversation_id format (must be 24-char hex)"
        )

    return conversation_id


def validate_job_id(job_id: str) -> str:
    """
    Validate job_id format (UUID).

    Args:
        job_id: Job ID to validate

    Returns:
        str: Validated job_id

    Raises:
        HTTPException: 400 if not valid UUID format
    """
    if not job_id or not isinstance(job_id, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="job_id must be non-empty string"
        )

    # UUID format: 8-4-4-4-12 hex characters
    if not re.match(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', job_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid job_id format (must be UUID)"
        )

    return job_id
