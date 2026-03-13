"""A8 contract test: async_jobs failed-job retry block.

M-023: HTTP 409 "Task rejected: failed" when retrying a skill call after a
transient failure. Stale 'failed' rows in async_jobs persist with their input_hash.
get_async_job_by_hash only filters on status IN ('accepted', 'running', 'queued') —
so 'failed' rows are invisible to dedup. But the UNIQUE constraint on input_hash
(if it exists) causes INSERT to fail, returning 409 on retry.

FIX LOCATION: storage.py
Add a method to purge stale failed/expired jobs:
    def purge_stale_failed_jobs(self, grace_seconds: int = 300) -> int:
        cutoff = time.time() - grace_seconds
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM async_jobs WHERE status IN ('failed', 'expired') AND updated_at < ?",
            (cutoff,)
        )
        conn.commit()
        return cursor.rowcount

Call this at job submission time (before insert) or via periodic sweep.

CONTRACT:
1. purge_stale_failed_jobs(grace_seconds) method exists on Storage.
2. Failed jobs older than grace_seconds are deleted by purge_stale_failed_jobs.
3. Failed jobs newer than grace_seconds are NOT deleted (still within grace period).
4. After purge, a new job with the same input_hash can be inserted without error.
5. get_async_job_by_hash still returns None for a failed (non-active) job.
"""
import time
import uuid
import pytest
from knarr.dht.storage import Storage


SKILL = "test-skill"
CONSUMER = "aa" * 32
INPUT_HASH = "deadbeef" * 8


def _insert_job(storage, status, updated_at_offset=-400):
    job_id = str(uuid.uuid4())
    now = time.time()
    storage.insert_async_job(
        job_id=job_id,
        skill=SKILL,
        consumer_id=CONSUMER,
        input_hash=INPUT_HASH,
        position=0,
        expires_at=now + 3600,
    )
    # Manually set status and updated_at
    conn = storage._get_conn()
    conn.execute(
        "UPDATE async_jobs SET status=?, updated_at=? WHERE job_id=?",
        (status, now + updated_at_offset, job_id),
    )
    conn.commit()
    return job_id


def test_purge_stale_failed_jobs_method_exists():
    """Storage must have a purge_stale_failed_jobs method."""
    storage = Storage(":memory:")
    assert hasattr(storage, "purge_stale_failed_jobs"), (
        "Storage is missing purge_stale_failed_jobs method. "
        "Fix: add purge_stale_failed_jobs(grace_seconds=300) to storage.py."
    )


def test_purge_removes_old_failed_jobs():
    """Failed jobs older than grace_seconds must be deleted."""
    storage = Storage(":memory:")
    _insert_job(storage, "failed", updated_at_offset=-400)  # 400s old

    deleted = storage.purge_stale_failed_jobs(grace_seconds=300)
    assert deleted >= 1, (
        f"Expected at least 1 deletion, got {deleted}. "
        "purge_stale_failed_jobs must delete failed rows older than grace_seconds."
    )

    conn = storage._get_conn()
    rows = conn.execute(
        "SELECT COUNT(*) FROM async_jobs WHERE input_hash=? AND status='failed'",
        (INPUT_HASH,)
    ).fetchone()
    assert rows[0] == 0, "Old failed job still exists after purge."


def test_purge_keeps_recent_failed_jobs():
    """Failed jobs newer than grace_seconds must be retained (still in grace period)."""
    storage = Storage(":memory:")
    _insert_job(storage, "failed", updated_at_offset=-60)  # 60s old, within 300s grace

    deleted = storage.purge_stale_failed_jobs(grace_seconds=300)
    assert deleted == 0, (
        f"Purged {deleted} recent failed jobs within grace period. "
        "Jobs within grace_seconds must not be deleted."
    )


def test_retry_succeeds_after_purge():
    """After purging old failed job, a new job with same input_hash must insert cleanly."""
    storage = Storage(":memory:")
    _insert_job(storage, "failed", updated_at_offset=-400)

    storage.purge_stale_failed_jobs(grace_seconds=300)

    # Should now be able to insert a new job with the same input_hash
    new_job_id = str(uuid.uuid4())
    try:
        storage.insert_async_job(
            job_id=new_job_id,
            skill=SKILL,
            consumer_id=CONSUMER,
            input_hash=INPUT_HASH,
            position=0,
            expires_at=time.time() + 3600,
        )
    except Exception as exc:
        pytest.fail(
            f"Failed to insert retry job after purging old failed job: {exc}. "
            "Fix: purge_stale_failed_jobs must remove input_hash conflict."
        )

    row = storage.get_async_job(new_job_id)
    assert row is not None, "Retry job was not inserted."
    assert row["status"] == "queued"


def test_get_async_job_by_hash_ignores_failed():
    """get_async_job_by_hash must NOT return failed jobs (contract unchanged)."""
    storage = Storage(":memory:")
    _insert_job(storage, "failed", updated_at_offset=-10)

    result = storage.get_async_job_by_hash(INPUT_HASH)
    assert result is None, (
        "get_async_job_by_hash returned a failed job. "
        "Only active jobs (accepted/running/queued) should be returned."
    )


def test_purge_also_removes_expired_jobs():
    """Expired status (not just failed) should also be purged."""
    storage = Storage(":memory:")
    _insert_job(storage, "expired", updated_at_offset=-400)

    deleted = storage.purge_stale_failed_jobs(grace_seconds=300)
    assert deleted >= 1, (
        "purge_stale_failed_jobs should also delete expired rows older than grace_seconds."
    )
