from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Mapping

from .chatwoot import ChatwootError
from .config import AccountConfig, Settings
from .pipeline import Pipeline
from .store import Job, Store

logger = logging.getLogger("tabule")
RECONCILE_WINDOW_SECONDS = 86400


class Runner:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        pipeline: Pipeline,
        accounts: Mapping[int, AccountConfig],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._store = store
        self._pipeline = pipeline
        self._accounts = accounts
        self._clock = clock
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        recovered = self._store.recover_processing()
        if recovered:
            logger.warning("%s jobs interrompidos voltaram para a fila", recovered)
        for target, name in ((self._work_loop, "tabule-worker"), (self._reconcile_loop, "tabule-reconcile")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=10)
        self._threads.clear()

    def run_once(self) -> bool:
        job = self._store.claim_next()
        if job is None:
            return False
        try:
            self._pipeline.process(job)
        except ChatwootError as exc:
            if exc.status_code == 404:
                self._store.mark(job, "failed", str(exc)[:300])
                logger.warning("conta=%s conversa=%s não existe mais no Chatwoot", job.account_id, job.display_id)
            else:
                self._retry(job, exc)
        except Exception as exc:
            self._retry(job, exc)
        return True

    def reconcile_once(self) -> None:
        now = int(self._clock())
        for account in self._accounts.values():
            try:
                scanned, queued = self._pipeline.scan_resolved(
                    account,
                    source="reconcile",
                    priority=0,
                    pages=self._settings.reconcile_pages,
                    min_last_activity=now - RECONCILE_WINDOW_SECONDS,
                )
            except Exception as exc:
                logger.warning("conta=%s falha na reconciliação: %s", account.id, exc)
                continue
            if queued:
                logger.info("conta=%s reconciliação: %s varridas, %s enfileiradas", account.id, scanned, queued)

    def _retry(self, job: Job, exc: Exception) -> None:
        status = self._store.retry_later(
            job,
            f"{type(exc).__name__}: {exc}",
            max_attempts=self._settings.max_attempts,
        )
        logger.warning(
            "conta=%s conversa=%s tentativa=%s falhou (%s): %s -> %s",
            job.account_id,
            job.display_id,
            job.attempts,
            type(exc).__name__,
            exc,
            status,
        )

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            worked = self.run_once()
            self._stop.wait(self._settings.job_interval_seconds if worked else 1.0)

    def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            self.reconcile_once()
            self._stop.wait(self._settings.reconcile_interval_seconds)
