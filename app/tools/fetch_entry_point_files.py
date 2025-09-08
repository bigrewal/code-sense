from typing import List
from ..db import get_mongo_client

def fetch_entry_point_files(repo_id: str) -> str:
    print(f"TOOL INVOKED: Fetching entry point files for repo_id: {repo_id}")

    mongo_client = get_mongo_client()
    mental_model_collection = mongo_client["mental_model"]

    # Get list of all file_paths that have BRIEF_FILE_OVERVIEW
    entry_points_cursor = mental_model_collection.find({
        "repo_id": repo_id,
        "document_type": "POTENTIAL_ENTRY_POINTS"
    })
    entry_points = list(entry_points_cursor)
    entry_point_files = {ep['file_path'] for ep in entry_points}

    # Format for LLM
    formatted_files = '\n'.join(f'- {file_path}' for file_path in entry_point_files)
    return f"Here's the list of potential entry point files:\n{formatted_files}"


def fetch_entry_point_files_as_list(repo_id: str) -> List[str]:
    mongo_client = get_mongo_client()
    mental_model_collection = mongo_client["mental_model"]

    # Get list of all file_paths that have BRIEF_FILE_OVERVIEW
    entry_points_cursor = mental_model_collection.find({
        "repo_id": repo_id,
        "document_type": "POTENTIAL_ENTRY_POINTS"
    })
    entry_points = list(entry_points_cursor)
    entry_point_files = {ep['file_path'] for ep in entry_points}

    print(f"Fetched entry point files for repo_id: {repo_id}")
    return list(entry_point_files)