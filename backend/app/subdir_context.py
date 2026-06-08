from __future__ import annotations

import re
from typing import Any


_SUBDIR_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_./-])@(?P<path>(?:\./)?[A-Za-z0-9][A-Za-z0-9._/-]*)")
_TRAILING_MENTION_PUNCTUATION = ".,;:!?)]}\"'"


def normalize_subdir_path(path: str) -> str:
    if not path or not isinstance(path, str):
        raise ValueError("subdir_path must be a non-empty string")

    cleaned = path.strip().strip("`\"'")
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    cleaned = cleaned.rstrip(_TRAILING_MENTION_PUNCTUATION)
    cleaned = cleaned.replace("\\", "/")

    if cleaned.startswith("/") or cleaned.startswith("~"):
        raise ValueError("subdir_path must be repository-relative")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]

    parts = [part for part in cleaned.strip("/").split("/") if part and part != "."]
    if not parts:
        raise ValueError("subdir_path must be a non-empty repository-relative path")
    if any(part == ".." for part in parts):
        raise ValueError("subdir_path cannot contain parent directory traversal")

    return "/".join(parts)


def extract_subdir_mentions(message: str) -> list[str]:
    if not message or not isinstance(message, str):
        return []

    paths: list[str] = []
    seen: set[str] = set()
    for match in _SUBDIR_MENTION_RE.finditer(message):
        try:
            path = normalize_subdir_path(match.group("path"))
        except ValueError:
            continue
        if path not in seen:
            paths.append(path)
            seen.add(path)
    return paths


def merge_subdir_paths(message: str, explicit_paths: list[str] | None = None) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()

    for path in [*extract_subdir_mentions(message), *(explicit_paths or [])]:
        normalized = normalize_subdir_path(path)
        if normalized not in seen:
            paths.append(normalized)
            seen.add(normalized)

    return paths


def format_subdir_briefs(subdir_path: str, docs: list[dict[str, Any]]) -> str:
    normalized = normalize_subdir_path(subdir_path)
    lines = [f"SUBDIRECTORY @{normalized} FILE BRIEFS ({len(docs)} files):"]
    for doc in docs:
        file_path = str(doc.get("file_path") or "").strip()
        brief = str(doc.get("data") or "").strip()
        if not file_path or not brief:
            continue
        if brief.startswith(f"`{file_path}`"):
            lines.append(brief)
        else:
            lines.append(f"`{file_path}` {brief}")
    return "\n\n".join(lines)
