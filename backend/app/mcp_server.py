from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .host_agent_ingestion import (
    build_repo_context as build_repo_context_service,
    get_file_brief as get_file_brief_service,
    get_next_file_batch as get_next_file_batch_service,
    get_repo_context as get_repo_context_service,
    get_subdir_briefs as get_subdir_briefs_service,
    save_file_briefs as save_file_briefs_service,
    start_host_agent_ingestion as start_host_agent_ingestion_service,
)


mcp = FastMCP("code-sense")


@mcp.tool()
def start_host_agent_ingestion(repo_path: str, repo_name: str | None = None) -> dict[str, Any]:
    """Start a Code-Sense ingestion job that the host coding agent will summarize."""
    return start_host_agent_ingestion_service(repo_path=repo_path, repo_name=repo_name)


@mcp.tool()
def get_next_file_batch(
    job_id: str,
    repo_path: str | None = None,
    db_path: str | None = None,
    limit: int = 8,
    include_content: bool = False,
    max_content_bytes: int = 40000,
) -> dict[str, Any]:
    """Return the next pending files for a host agent to read, classify, and summarize."""
    return get_next_file_batch_service(
        job_id=job_id,
        repo_path=repo_path,
        db_path=db_path,
        limit=limit,
        include_content=include_content,
        max_content_bytes=max_content_bytes,
    )


@mcp.tool()
def save_file_briefs(
    job_id: str,
    file_results: list[dict[str, Any]],
    repo_path: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Persist host-agent file classifications and Code-Sense file briefs."""
    return save_file_briefs_service(
        job_id=job_id,
        file_results=file_results,
        repo_path=repo_path,
        db_path=db_path,
    )


@mcp.tool()
def build_repo_context(
    job_id: str,
    repo_path: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Build the final repo-wide Code-Sense context once all file briefs are saved."""
    return build_repo_context_service(job_id=job_id, repo_path=repo_path, db_path=db_path)


@mcp.tool()
def get_repo_context(
    repo_name: str,
    repo_path: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Return the compressed repo-wide mental model for the host agent to use."""
    return get_repo_context_service(repo_name=repo_name, repo_path=repo_path, db_path=db_path)


@mcp.tool()
def get_file_brief(
    repo_name: str,
    file_path: str,
    repo_path: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Return the stored Code-Sense brief for one critical file."""
    return get_file_brief_service(repo_name=repo_name, file_path=file_path, repo_path=repo_path, db_path=db_path)


@mcp.tool()
def get_subdir_briefs(
    repo_name: str,
    subdir_path: str,
    repo_path: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Return stored Code-Sense briefs for every critical file under a repo subdir."""
    return get_subdir_briefs_service(
        repo_name=repo_name,
        subdir_path=subdir_path,
        repo_path=repo_path,
        db_path=db_path,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
