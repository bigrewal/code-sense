## Service Layer

# This layer will contain the business logic and service definitions

# Write method fetch_job_status
from typing import Any, Dict
from .config import Config

from app.db import get_mongo_client


def fetch_job_status(job_id: str) -> Dict[str, Any]:
    mongo_client = get_mongo_client()
    job_status_collection = mongo_client[Config.JOB_STATUS_COLLECTION]
    job_stages = job_status_collection.find({"job_id": job_id})
    
    # If no job stages found, raise an exception
    if not job_stages:
        raise ValueError(f"No job stages found for job_id: {job_id}")

    stages = []
    for stage in job_stages:
        stages.append({
            "stage_name": stage["stage_name"],
            "status": stage["status"],
            "start_time": stage.get("start_time"),
            "end_time": stage.get("end_time")
        })

    return {"job_id": job_id, "stages": stages}