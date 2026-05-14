"""
job_store.py — Store con cursor de bytes y buffer de escritura GCS.
"""
import threading
from datetime import datetime, timezone
from typing import Optional


class JobStatus:
    PENDING    = "pending"
    UPLOADING  = "uploading"
    COMPLETING = "completing"
    COMPLETED  = "completed"
    FAILED     = "failed"


class JobStore:
    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, job_id: str, filename: str, backend_url: str, headers: Optional[list[str]] = None) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        job = {
            "job_id":          job_id,
            "filename":        filename,
            "backend_url":     backend_url,
            "headers":         headers,
            "status":          JobStatus.PENDING,
            "session_ready":   False,
            "upload_location": None,
            "object_name":     None,
            "resumable_url":   None,
            "byte_offset":     0,
            "pending_buffer":  b"",
            "chunks_received": 0,
            "total_rows":      0,
            "total_bytes":     0,
            "error":           None,
            "created_at":      now,
            "updated_at":      now,
        }
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock:
            return self._jobs.get(job_id)

    def set_session(self, job_id: str, upload_location: str,
                    object_name: str, resumable_url: str):
        with self._lock:
            job = self._jobs[job_id]
            job["upload_location"] = upload_location
            job["object_name"]     = object_name
            job["resumable_url"]   = resumable_url
            job["session_ready"]   = True
            job["status"]          = JobStatus.UPLOADING
            job["updated_at"]      = datetime.now(timezone.utc).isoformat()

    def set_pending_buffer(self, job_id: str, data: bytes):
        with self._lock:
            self._jobs[job_id]["pending_buffer"] = data
            self._jobs[job_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

    def append_to_buffer(self, job_id: str, data: bytes):
        with self._lock:
            self._jobs[job_id]["pending_buffer"] += data
            self._jobs[job_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

    def flush_buffer(self, job_id: str, new_offset: int, remaining_buffer: bytes,
                     rows_added: int, bytes_flushed: int):
        with self._lock:
            job = self._jobs[job_id]
            job["byte_offset"]     = new_offset
            job["pending_buffer"]  = remaining_buffer
            job["chunks_received"] += 1
            job["total_rows"]      += rows_added
            job["total_bytes"]     += bytes_flushed
            job["updated_at"]      = datetime.now(timezone.utc).isoformat()

    def set_completed(self, job_id: str, total_bytes: int, total_rows: int):
        with self._lock:
            job = self._jobs[job_id]
            job["status"]         = JobStatus.COMPLETED
            job["total_bytes"]    = total_bytes
            job["total_rows"]     = total_rows
            job["pending_buffer"] = b""
            job["updated_at"]     = datetime.now(timezone.utc).isoformat()

    def set_status(self, job_id: str, status: str, error: str = None):
        with self._lock:
            job = self._jobs[job_id]
            job["status"]     = status
            job["updated_at"] = datetime.now(timezone.utc).isoformat()
            if error:
                job["error"] = error

    def set_headers(self, job_id: str, headers: list[str]):
        with self._lock:
            self._jobs[job_id]["headers"] = headers

    def delete(self, job_id: str):
        with self._lock:
            self._jobs.pop(job_id, None)
