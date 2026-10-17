from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    account_id INTEGER NOT NULL,
    display_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    run_after INTEGER NOT NULL,
    delivery_id TEXT,
    note TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (account_id, display_id)
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (status, priority, run_after);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    display_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    mode TEXT NOT NULL,
    action TEXT NOT NULL,
    choice TEXT,
    confidence REAL,
    probabilities TEXT,
    reason TEXT,
    model TEXT,
    reverted_at INTEGER
);
CREATE INDEX IF NOT EXISTS decisions_account ON decisions (account_id, action, created_at);
"""


@dataclass(frozen=True)
class Job:
    account_id: int
    display_id: int
    source: str
    priority: int
    status: str
    attempts: int
    run_after: int
    delivery_id: str | None
    note: str | None


def _job_from_row(row: sqlite3.Row, *, status: str | None = None, attempts: int | None = None) -> Job:
    return Job(
        account_id=row["account_id"],
        display_id=row["display_id"],
        source=row["source"],
        priority=row["priority"],
        status=status or row["status"],
        attempts=attempts if attempts is not None else row["attempts"],
        run_after=row["run_after"],
        delivery_id=row["delivery_id"],
        note=row["note"],
    )


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _now(self) -> int:
        return int(self._clock())

    def enqueue_webhook(self, account_id: int, display_id: int, *, delivery_id: str | None, delay: int) -> str:
        now = self._now()
        run_after = now + delay
        with self._lock:
            row = self._conn.execute(
                "SELECT status, priority FROM jobs WHERE account_id=? AND display_id=?", (account_id, display_id)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO jobs (account_id, display_id, source, priority, status, attempts, run_after, "
                    "delivery_id, created_at, updated_at) VALUES (?, ?, 'webhook', 0, 'pending', 0, ?, ?, ?, ?)",
                    (account_id, display_id, run_after, delivery_id, now, now),
                )
                return "queued"
            if row["status"] in ("not_resolved", "failed"):
                self._conn.execute(
                    "UPDATE jobs SET status='pending', source='webhook', priority=0, attempts=0, run_after=?, "
                    "delivery_id=?, note=NULL, updated_at=? WHERE account_id=? AND display_id=?",
                    (run_after, delivery_id, now, account_id, display_id),
                )
                return "requeued"
            if row["status"] == "pending" and row["priority"] > 0:
                self._conn.execute(
                    "UPDATE jobs SET priority=0, run_after=MIN(run_after, ?), updated_at=? "
                    "WHERE account_id=? AND display_id=?",
                    (run_after, now, account_id, display_id),
                )
            return "duplicate"

    def enqueue_bulk(
        self,
        account_id: int,
        display_id: int,
        *,
        source: str,
        priority: int,
        delay: int = 0,
        reset: bool = False,
    ) -> bool:
        now = self._now()
        run_after = now + delay
        with self._lock:
            inserted = self._conn.execute(
                "INSERT OR IGNORE INTO jobs (account_id, display_id, source, priority, status, attempts, run_after, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)",
                (account_id, display_id, source, priority, run_after, now, now),
            ).rowcount
            if inserted:
                return True
            if not reset:
                return False
            updated = self._conn.execute(
                "UPDATE jobs SET status='pending', source=?, priority=?, attempts=0, run_after=?, note=NULL, "
                "updated_at=? WHERE account_id=? AND display_id=? AND status != 'processing'",
                (source, priority, run_after, now, account_id, display_id),
            ).rowcount
            return bool(updated)

    def claim_next(self) -> Job | None:
        now = self._now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE status='pending' AND run_after<=? ORDER BY priority, run_after LIMIT 1",
                    (now,),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                self._conn.execute(
                    "UPDATE jobs SET status='processing', attempts=attempts+1, updated_at=? "
                    "WHERE account_id=? AND display_id=?",
                    (now, row["account_id"], row["display_id"]),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return _job_from_row(row, status="processing", attempts=row["attempts"] + 1)

    def mark(self, job: Job, status: str, note: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, note=?, updated_at=? WHERE account_id=? AND display_id=?",
                (status, note, self._now(), job.account_id, job.display_id),
            )

    def retry_later(self, job: Job, error: str, *, max_attempts: int, base_delay: int = 60) -> str:
        now = self._now()
        if job.attempts >= max_attempts:
            status, run_after = "failed", job.run_after
        else:
            status = "pending"
            run_after = now + min(3600, base_delay * 2 ** max(job.attempts - 1, 0))
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, run_after=?, note=?, updated_at=? WHERE account_id=? AND display_id=?",
                (status, run_after, error[:500], now, job.account_id, job.display_id),
            )
        return status

    def recover_processing(self) -> int:
        with self._lock:
            return self._conn.execute(
                "UPDATE jobs SET status='pending', updated_at=? WHERE status='processing'", (self._now(),)
            ).rowcount

    def get_job(self, account_id: int, display_id: int) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE account_id=? AND display_id=?", (account_id, display_id)
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def record_decision(
        self,
        account_id: int,
        display_id: int,
        *,
        mode: str,
        action: str,
        choice: str | None,
        confidence: float | None,
        probabilities: list[dict[str, Any]],
        reason: str,
        model: str,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO decisions (account_id, display_id, created_at, mode, action, choice, confidence, "
                "probabilities, reason, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account_id,
                    display_id,
                    self._now(),
                    mode,
                    action,
                    choice,
                    confidence,
                    json.dumps(probabilities, ensure_ascii=False),
                    reason,
                    model,
                ),
            )
            return int(cursor.lastrowid)

    def written_decisions(self, account_id: int, since: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, display_id, choice, created_at FROM decisions WHERE account_id=? AND action='written' "
                "AND reverted_at IS NULL AND created_at>=? ORDER BY id",
                (account_id, int(since)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_reverted(self, decision_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE decisions SET reverted_at=? WHERE id=?", (self._now(), decision_id))

    def list_decisions(self, account_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if account_id is None:
                rows = self._conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM decisions WHERE account_id=? ORDER BY id", (account_id,)
                ).fetchall()
        return [dict(row) for row in rows]

    def queue_summary(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    def decision_summary(self, since: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT account_id, action, COUNT(*) AS n FROM decisions WHERE created_at>=? "
                "GROUP BY account_id, action ORDER BY account_id, action",
                (int(since),),
            ).fetchall()
        return [dict(row) for row in rows]
