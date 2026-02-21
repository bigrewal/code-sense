from types import SimpleNamespace

import pytest
from bson import ObjectId

from app import db
from app.db_exceptions import InvalidParameterError, QueryError
from app.models.data_model import IngestionJobStatus, IngestionStage, IngestionStageStatus


class FakeCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, *_args, **_kwargs):
        return self

    def skip(self, n):
        self.docs = self.docs[n:]
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    def __iter__(self):
        return iter(self.docs)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.last_update = None
        self.raise_on_insert = False

    def find_one(self, query=None, projection=None):
        query = query or {}
        for d in self.docs:
            ok = True
            for k, v in query.items():
                if d.get(k) != v:
                    ok = False
                    break
            if ok:
                return d
        return None

    def find(self, query=None, projection=None):
        query = query or {}
        out = []
        for d in self.docs:
            ok = True
            for k, v in query.items():
                if isinstance(v, dict) and "$in" in v:
                    if d.get(k) not in v["$in"]:
                        ok = False
                        break
                elif d.get(k) != v:
                    ok = False
                    break
            if ok:
                if projection and projection != {"_id": 0}:
                    row = {}
                    for key, keep in projection.items():
                        if keep and key in d:
                            row[key] = d[key]
                    if "_id" in d and projection.get("_id", 1):
                        row["_id"] = d["_id"]
                    out.append(row)
                elif projection == {"_id": 0}:
                    row = {k: v for k, v in d.items() if k != "_id"}
                    out.append(row)
                else:
                    out.append(d)
        return FakeCursor(out)

    def insert_one(self, doc):
        if self.raise_on_insert:
            raise RuntimeError("insert failed")
        inserted = dict(doc)
        inserted.setdefault("_id", "507f1f77bcf86cd799439011")
        self.docs.append(inserted)
        return SimpleNamespace(inserted_id=inserted["_id"])

    def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in query.items()):
                self.docs.pop(i)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    def delete_many(self, query):
        before = len(self.docs)
        kept = []
        for d in self.docs:
            match = True
            for k, v in query.items():
                if isinstance(v, dict) and "$in" in v:
                    if d.get(k) not in v["$in"]:
                        match = False
                        break
                elif d.get(k) != v:
                    match = False
                    break
            if not match:
                kept.append(d)
        self.docs = kept
        return SimpleNamespace(deleted_count=before - len(self.docs))

    def update_one(self, filt, update, upsert=False):
        self.last_update = {"filter": filt, "update": update, "upsert": upsert}
        return SimpleNamespace(matched_count=1, modified_count=1)

    def count_documents(self, query):
        return len(list(self.find(query)))


class FakeDB(dict):
    def command(self, cmd):
        if cmd != "ping":
            raise RuntimeError("unsupported")
        return {"ok": 1}

    @property
    def name(self):
        return "testdb"

    def list_collection_names(self):
        return list(self.keys())


def _client_with_fake_db(fake_db):
    client = db.MyMongoClient.__new__(db.MyMongoClient)
    client._db = fake_db
    client._client = object()
    client._initialized = True
    return client


def test_validate_repo_name_rejects_bad_values():
    with pytest.raises(InvalidParameterError):
        db._validate_repo_name("")
    with pytest.raises(InvalidParameterError):
        db._validate_repo_name("bad${name}")


def test_delete_repo_data_and_file_states():
    fake_db = FakeDB(
        {
            db.CONVERSATIONS_COLLECTION: FakeCollection([{"repo_name": "r"}]),
            db.MESSAGES_COLLECTION: FakeCollection([{"repo_name": "r"}]),
            db.MENTAL_MODEL_COLLECTION: FakeCollection([{"repo_name": "r"}]),
            db.INGESTED_REPOS_COLLECTION: FakeCollection([{"repo_name": "r"}]),
            db.INGESTION_FILE_STATE_COLLECTION: FakeCollection(
                [{"repo_name": "r", "file_path": "a.py"}, {"repo_name": "r", "file_path": "b.py"}]
            ),
            db.INGESTION_JOBS_COLLECTION: FakeCollection([{"repo_name": "r"}]),
        }
    )
    client = _client_with_fake_db(fake_db)

    assert client.delete_repo_file_states("r", []) == 0
    assert client.delete_repo_file_states("r", ["a.py"]) == 1

    result = client.delete_repo_data("r")
    assert result["repo_name"] == "r"
    assert result["collections_processed"] == 6
    assert result["total_deleted"] >= 1


def test_delete_repo_data_wraps_errors():
    class BrokenDB(FakeDB):
        def __getitem__(self, item):
            raise RuntimeError("db down")

    client = _client_with_fake_db(BrokenDB())
    with pytest.raises(QueryError):
        client.delete_repo_data("r")


def test_create_list_and_delete_conversation_flows():
    oid = ObjectId("507f1f77bcf86cd799439011")
    conv_coll = FakeCollection([{"_id": oid, "repo_name": "repo-a"}])
    msg_coll = FakeCollection([{"conversation_id": "507f1f77bcf86cd799439011"}])
    fake_db = FakeDB(
        {
            db.CONVERSATIONS_COLLECTION: conv_coll,
            db.MESSAGES_COLLECTION: msg_coll,
            db.INGESTION_JOBS_COLLECTION: FakeCollection([]),
            db.INGESTED_REPOS_COLLECTION: FakeCollection([]),
        }
    )
    client = _client_with_fake_db(fake_db)

    created = client.create_conversation("repo-a")
    assert created["repo_name"] == "repo-a"

    rows = client.list_conversations(repo_name="repo-a", limit=500, offset=-1)
    assert isinstance(rows, list)

    conversation_id = "507f1f77bcf86cd799439011"
    assert client.conversation_exists(conversation_id) is True
    assert len(client.list_conversation_messages(conversation_id=conversation_id, limit=1000)) == 1
    client.delete_conversation(conversation_id)

    with pytest.raises(ValueError):
        client.conversation_exists("bad-id")
    with pytest.raises(ValueError):
        client.delete_conversation("bad-id")
    with pytest.raises(KeyError):
        client.delete_conversation("507f1f77bcf86cd799439012")


def test_create_conversation_wraps_errors():
    conv_coll = FakeCollection([])
    conv_coll.raise_on_insert = True
    fake_db = FakeDB({db.CONVERSATIONS_COLLECTION: conv_coll})
    client = _client_with_fake_db(fake_db)
    with pytest.raises(QueryError):
        client.create_conversation("repo-a")


def test_ingestion_job_and_repo_helpers():
    jobs = FakeCollection(
        [
            {
                "job_id": "j1",
                "repo_name": "repo-a",
                "status": "running",
                "current_stage": "precheck",
                "stages": {
                    "precheck": {
                        "status": "completed",
                        "metrics": {"supported_file_count": 1, "secret_metric": 99},
                    }
                },
                "abort_requested": True,
            }
        ]
    )
    repos = FakeCollection([{"repo_name": "repo-a"}])
    states = FakeCollection(
        [{"repo_name": "repo-a", "file_path": "a.py", "sha1": "abc", "supported": True}]
    )
    fake_db = FakeDB(
        {
            db.INGESTION_JOBS_COLLECTION: jobs,
            db.INGESTED_REPOS_COLLECTION: repos,
            db.INGESTION_FILE_STATE_COLLECTION: states,
        }
    )
    client = _client_with_fake_db(fake_db)

    job = IngestionJobStatus(
        job_id="j2",
        repo_name="repo-a",
        status="running",
        current_stage=IngestionStage.PRECHECK,
        stage_status={IngestionStage.PRECHECK: IngestionStageStatus.RUNNING},
    )
    client.upsert_ingestion_job(job, extra_fields={"operation": "x"})
    assert jobs.last_update is not None

    status = client.get_job_status("j1")
    assert status["stages"]["precheck"]["metrics"] == {"supported_file_count": 1}
    assert client.get_job_status("missing") is None

    listed, total = client.list_jobs(include_total=True)
    assert len(listed) == 1
    assert total == 1
    assert client.is_abort_requested("j1") is True
    assert client.is_abort_requested("missing") is False

    assert client.list_ingested_repos() == ["repo-a"]
    assert client.is_repo_ingested("repo-a") is True

    assert client.get_repo_file_states("repo-a") == {
        "a.py": {"file_path": "a.py", "sha1": "abc", "supported": True}
    }


def test_job_delete_health_and_close_paths(monkeypatch):
    jobs = FakeCollection([{"job_id": "j1"}])
    fake_db = FakeDB({db.INGESTION_JOBS_COLLECTION: jobs})
    client = _client_with_fake_db(fake_db)

    assert client.get_job("j1") == {"job_id": "j1"}
    assert client.delete_job("j1") is True
    assert client.delete_job("missing") is False

    healthy = client.health_check()
    assert healthy["status"] == "healthy"
    assert healthy["collection_count"] == 1

    client._db = None
    unhealthy = client.health_check()
    assert unhealthy["status"] == "unhealthy"

    class BadDB(FakeDB):
        def command(self, cmd):
            raise RuntimeError("ping failed")

    client._db = BadDB({})
    client._client = object()
    ping_fail = client.health_check()
    assert ping_fail["status"] == "unhealthy"

    class FakeMongoDriver:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    driver = FakeMongoDriver()
    client._client = driver
    client._db = FakeDB({})
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)
    client.close()
    assert driver.closed is True
