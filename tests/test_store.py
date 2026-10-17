import pytest

from tabule.store import Store


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock):
    db = Store(tmp_path / "tabule.db", clock=clock)
    yield db
    db.close()


def test_webhook_is_queued_once_then_duplicate(store):
    assert store.enqueue_webhook(9, 101, delivery_id="d1", delay=60) == "queued"
    assert store.enqueue_webhook(9, 101, delivery_id="d2", delay=60) == "duplicate"
    job = store.get_job(9, 101)
    assert job.status == "pending"
    assert job.run_after == 1_000_060


def test_claim_respects_run_after(store, clock):
    store.enqueue_webhook(9, 101, delivery_id=None, delay=60)
    assert store.claim_next() is None
    clock.now += 60
    job = store.claim_next()
    assert job is not None
    assert job.status == "processing"
    assert job.attempts == 1


def test_webhook_jobs_run_before_backfill_jobs(store):
    store.enqueue_bulk(9, 1, source="backfill", priority=1)
    store.enqueue_webhook(9, 2, delivery_id=None, delay=0)
    assert store.claim_next().display_id == 2
    assert store.claim_next().display_id == 1


def test_bulk_never_duplicates_unless_reset(store):
    assert store.enqueue_bulk(9, 5, source="reconcile", priority=0) is True
    assert store.enqueue_bulk(9, 5, source="reconcile", priority=0) is False
    assert store.enqueue_bulk(9, 5, source="backfill", priority=1, reset=True) is True
    job = store.get_job(9, 5)
    assert job.status == "pending"
    assert job.priority == 1


def test_not_resolved_job_is_requeued_by_new_resolution(store):
    store.enqueue_webhook(9, 7, delivery_id=None, delay=0)
    job = store.claim_next()
    store.mark(job, "not_resolved", "status: open")
    assert store.enqueue_webhook(9, 7, delivery_id=None, delay=0) == "requeued"
    assert store.get_job(9, 7).status == "pending"


def test_retries_until_failed(store, clock):
    store.enqueue_webhook(9, 8, delivery_id=None, delay=0)
    job = store.claim_next()
    assert store.retry_later(job, "erro", max_attempts=2) == "pending"
    clock.now += 3600
    job = store.claim_next()
    assert job.attempts == 2
    assert store.retry_later(job, "erro", max_attempts=2) == "failed"


def test_interrupted_jobs_return_to_queue_on_recovery(store):
    store.enqueue_webhook(9, 9, delivery_id=None, delay=0)
    store.claim_next()
    assert store.recover_processing() == 1
    assert store.get_job(9, 9).status == "pending"


def test_written_decisions_can_be_listed_and_reverted(store):
    decision_id = store.record_decision(
        9,
        101,
        mode="live",
        action="written",
        choice="A",
        confidence=0.9,
        probabilities=[],
        reason="aceita",
        model="gpt-6-luna",
    )
    assert [row["id"] for row in store.written_decisions(9, since=0)] == [decision_id]
    store.mark_reverted(decision_id)
    assert store.written_decisions(9, since=0) == []
