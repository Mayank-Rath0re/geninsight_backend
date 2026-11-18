"""In-process progress registry for uploads.

An upload still runs as one ordinary POST (same response contract), but the
client generates a job id and polls GET /upload_status while that request is
in flight, so it can show which phase the ingest is in and how far along it is.

State lives in this process only, which matches how UPLOAD_DIR already works:
the API is deployed as a single uvicorn process (see Dockerfile). Jobs are
pruned an hour after their last update.
"""

import threading
import time
from typing import Optional

JOB_TTL_SECONDS = 3600

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _key(user_id: int, job_id: str) -> str:
    return f"{user_id}:{job_id}"


def _prune(now: float) -> None:
    stale = [k for k, job in _jobs.items() if now - job["updated_at"] > JOB_TTL_SECONDS]
    for key in stale:
        _jobs.pop(key, None)


def new_job(user_id: int, job_id: str) -> str:
    now = time.time()
    key = _key(user_id, job_id)
    with _lock:
        _prune(now)
        _jobs[key] = {
            "status": "running",
            "phase": "cleaning",
            "pct": 20,
            "message": "Preparing the file",
            "result": None,
            "error": None,
            "updated_at": now,
        }
    return key


def update(user_id: int, job_id: str, phase=None, pct=None, message=None) -> None:
    with _lock:
        job = _jobs.get(_key(user_id, job_id))
        if job is None or job["status"] != "running":
            return
        if phase is not None:
            job["phase"] = phase
        if pct is not None:
            job["pct"] = max(0, min(100, int(pct)))
        if message is not None:
            job["message"] = message
        job["updated_at"] = time.time()


def finish(user_id: int, job_id: str, result: dict) -> None:
    with _lock:
        job = _jobs.get(_key(user_id, job_id))
        if job is None:
            return
        job.update(status="done", phase="preview", pct=100, message="Ready",
                   result=result, updated_at=time.time())


def fail(user_id: int, job_id: str, message: str, code: Optional[str] = None) -> None:
    with _lock:
        job = _jobs.get(_key(user_id, job_id))
        if job is None:
            return
        job.update(status="error", error={"code": code, "message": message},
                   updated_at=time.time())


def get(user_id: int, job_id: str) -> Optional[dict]:
    with _lock:
        job = _jobs.get(_key(user_id, job_id))
        return dict(job) if job else None
