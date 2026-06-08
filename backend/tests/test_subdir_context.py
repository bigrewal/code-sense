from app.subdir_context import extract_subdir_mentions, format_subdir_briefs, merge_subdir_paths, normalize_subdir_path


def test_extract_subdir_mentions_normalizes_and_deduplicates_paths():
    message = "Explain @backend/app, compare @./frontend/src and ignore user@example.com plus @backend/app."

    assert extract_subdir_mentions(message) == ["backend/app", "frontend/src"]


def test_normalize_subdir_path_rejects_unsafe_paths():
    for value in ["", "/backend/app", "~/repo", "../backend", "backend/../app"]:
        try:
            normalize_subdir_path(value)
        except ValueError:
            continue
        raise AssertionError(f"expected {value!r} to be rejected")


def test_merge_subdir_paths_combines_message_and_explicit_paths():
    assert merge_subdir_paths("Ask about @backend/app", ["frontend/src", "@backend/app"]) == [
        "backend/app",
        "frontend/src",
    ]


def test_format_subdir_briefs_prefixes_unqualified_briefs():
    context = format_subdir_briefs(
        "backend/app",
        [
            {"file_path": "backend/app/main.py", "data": "defines the API"},
            {"file_path": "backend/app/db.py", "data": "`backend/app/db.py` owns persistence."},
        ],
    )

    assert "SUBDIRECTORY @backend/app FILE BRIEFS (2 files):" in context
    assert "`backend/app/main.py` defines the API" in context
    assert "`backend/app/db.py` owns persistence." in context
