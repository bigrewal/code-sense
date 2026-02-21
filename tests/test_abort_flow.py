from app.db import INGESTION_JOBS_COLLECTION, MyMongoClient, _serialize_job


VALID_JOB_ID = "123e4567-e89b-42d3-a456-426614174000"


class _FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or {}

    def find_one(self, query, projection=None):
        job_id = query.get("job_id")
        doc = self.docs.get(job_id)
        if not doc:
            return None
        return dict(doc)

    def update_one(self, query, update):
        job_id = query.get("job_id")
        doc = self.docs.get(job_id)
        if not doc:
            return type("Result", (), {"matched_count": 0})()

        if doc.get("abort_requested") is True:
            return type("Result", (), {"matched_count": 0})()
        if doc.get("status") in {"completed", "failed", "aborted"}:
            return type("Result", (), {"matched_count": 0})()

        for k, v in update.get("$set", {}).items():
            doc[k] = v
        self.docs[job_id] = doc
        return type("Result", (), {"matched_count": 1})()


def _client_with_docs(docs):
    client = MyMongoClient.__new__(MyMongoClient)
    client._db = {INGESTION_JOBS_COLLECTION: _FakeCollection(docs)}
    return client


def test_request_abort_not_found():
    client = _client_with_docs({})
    result = client.request_abort(VALID_JOB_ID)
    assert result["reason"] == "not_found"
    assert result["abort_requested"] is False


def test_request_abort_marks_aborting():
    client = _client_with_docs(
        {
            VALID_JOB_ID: {
                "job_id": VALID_JOB_ID,
                "status": "running",
                "abort_requested": False,
            }
        }
    )
    result = client.request_abort(VALID_JOB_ID)
    assert result["reason"] == "requested"
    assert result["abort_requested"] is True
    assert result["status"] == "aborting"


def test_request_abort_already_terminal():
    client = _client_with_docs(
        {
            VALID_JOB_ID: {
                "job_id": VALID_JOB_ID,
                "status": "completed",
                "abort_requested": False,
            }
        }
    )
    result = client.request_abort(VALID_JOB_ID)
    assert result["reason"] == "already_terminal"
    assert result["already_terminal"] is True


def test_serialize_job_includes_abort_metadata():
    job = _serialize_job(
        {
            "job_id": VALID_JOB_ID,
            "repo_name": "repo",
            "status": "aborted",
            "current_stage": "resolve_refs",
            "stages": {},
            "abort_requested": True,
            "abort_requested_at": "t1",
            "aborted_at": "t2",
            "aborted_after_stage": "resolve_refs",
        }
    )
    assert job["abort_requested"] is True
    assert job["abort_requested_at"] == "t1"
    assert job["aborted_at"] == "t2"
    assert job["aborted_after_stage"] == "resolve_refs"
