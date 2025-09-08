from ..db import get_mongo_client

def fetch_brief_file_summary(file_path: str, repo_id: str) -> str:
    """Tool for LLM: Fetch brief file summary"""
    mongo_client = get_mongo_client()
    mental_model_collection = mongo_client["mental_model"]

    brief_overview = mental_model_collection.find_one({
        "repo_id": repo_id,
        "document_type": "BRIEF_FILE_OVERVIEW",
        "file_path": file_path
    })

    return brief_overview["data"] if brief_overview else f"Brief file overview doesn't exist for {file_path}"