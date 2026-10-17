from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Mapping

from .chatwoot import ChatwootClient, attribute_options
from .classifier import Outcome, build_choices, build_instructions, decide
from .config import AccountConfig, Settings
from .decisions import DecisionsClient
from .store import Job, Store
from .transcript import build_transcript

logger = logging.getLogger("tabule")
OPTIONS_TTL_SECONDS = 600


def _attribute(conversation: Mapping[str, Any], key: str) -> Any:
    return (conversation.get("custom_attributes") or {}).get(key)


def _filled(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _fmt(confidence: float | None) -> str:
    return "-" if confidence is None else f"{confidence:.2f}"


class Pipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        accounts: Mapping[int, AccountConfig],
        store: Store,
        client_factory: Callable[[AccountConfig], ChatwootClient],
        decisions: DecisionsClient,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._accounts = accounts
        self._store = store
        self._client_factory = client_factory
        self._decisions = decisions
        self._clock = clock
        self._lock = threading.RLock()
        self._clients: dict[int, ChatwootClient] = {}
        self._options: dict[int, tuple[float, list[str]]] = {}

    def client(self, account: AccountConfig) -> ChatwootClient:
        if account.id not in self._clients:
            self._clients[account.id] = self._client_factory(account)
        return self._clients[account.id]

    def options(self, account: AccountConfig) -> list[str]:
        now = self._clock()
        cached = self._options.get(account.id)
        if cached is not None and now - cached[0] < OPTIONS_TTL_SECONDS:
            return cached[1]
        values = attribute_options(self.client(account).attribute_definitions(), account.attribute_key)
        self._options[account.id] = (now, values)
        return values

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()
            self._decisions.close()

    def process(self, job: Job) -> str:
        with self._lock:
            return self._process(job)

    def scan_resolved(
        self,
        account: AccountConfig,
        *,
        source: str,
        priority: int,
        pages: int,
        delay: int = 0,
        min_last_activity: int | None = None,
        reset: bool = False,
    ) -> tuple[int, int]:
        with self._lock:
            return self._scan(
                account,
                source=source,
                priority=priority,
                pages=pages,
                delay=delay,
                min_last_activity=min_last_activity,
                reset=reset,
            )

    def _process(self, job: Job) -> str:
        account = self._accounts.get(job.account_id)
        if account is None:
            self._store.mark(job, "failed", "conta fora da allowlist ou desabilitada")
            return "failed"
        client = self.client(account)
        conversation = client.get_conversation(job.display_id)
        if conversation.get("status") != "resolved":
            self._store.mark(job, "not_resolved", f"status atual: {conversation.get('status')}")
            return "not_resolved"
        if conversation.get("inbox_id") not in account.inbox_ids:
            self._store.mark(job, "done", "inbox fora da lista")
            return "skipped"
        if _filled(_attribute(conversation, account.attribute_key)):
            self._store.mark(job, "done", "já tabulada")
            return "skipped"
        values = self.options(account)
        choices = build_choices(values, account.options)
        transcript = build_transcript(
            client.iter_messages(job.display_id),
            max_chars=self._settings.max_transcript_chars,
            timezone=self._settings.timezone,
        )
        if transcript:
            answer = self._decisions.choose(
                transcript=transcript,
                instructions=build_instructions(account.context),
                choices=choices,
            )
            outcome = decide(answer, valid_values=values, threshold=account.confidence_threshold)
        else:
            outcome = Outcome("review", None, None, [], "conversa sem mensagens")
        self._apply(client, account, job, outcome)
        self._store.mark(job, "done")
        return outcome.action

    def _scan(
        self,
        account: AccountConfig,
        *,
        source: str,
        priority: int,
        pages: int,
        delay: int,
        min_last_activity: int | None,
        reset: bool,
    ) -> tuple[int, int]:
        client = self.client(account)
        scanned = 0
        queued = 0
        for page in range(1, pages + 1):
            items = client.list_resolved_conversations(page)
            if not items:
                break
            reached_limit = False
            for conversation in items:
                last_activity = conversation.get("last_activity_at")
                if min_last_activity is not None and last_activity is not None and last_activity < min_last_activity:
                    reached_limit = True
                    break
                scanned += 1
                if conversation.get("inbox_id") not in account.inbox_ids:
                    continue
                if _filled(_attribute(conversation, account.attribute_key)):
                    continue
                if self._store.enqueue_bulk(
                    account.id,
                    conversation["id"],
                    source=source,
                    priority=priority,
                    delay=delay,
                    reset=reset,
                ):
                    queued += 1
            if reached_limit:
                break
        return scanned, queued

    def _apply(self, client: ChatwootClient, account: AccountConfig, job: Job, outcome: Outcome) -> None:
        live = account.mode == "live"
        if outcome.action == "accept":
            if not live:
                action = "would_write"
            else:
                current = client.get_conversation(job.display_id)
                if current.get("status") != "resolved" or _filled(_attribute(current, account.attribute_key)):
                    self._record(account, job, "skipped_changed", outcome)
                    return
                client.update_custom_attributes(job.display_id, {account.attribute_key: outcome.choice})
                action = "written"
        elif not live:
            action = "would_review"
        else:
            labels = client.get_conversation_labels(job.display_id)
            if account.review_label not in labels:
                client.set_conversation_labels(job.display_id, [*labels, account.review_label])
            action = "review_labeled"
        self._record(account, job, action, outcome)

    def _record(self, account: AccountConfig, job: Job, action: str, outcome: Outcome) -> None:
        self._store.record_decision(
            account.id,
            job.display_id,
            mode=account.mode,
            action=action,
            choice=outcome.choice,
            confidence=outcome.confidence,
            probabilities=outcome.probabilities,
            reason=outcome.reason,
            model=self._decisions.model,
        )
        logger.info(
            "conta=%s conversa=%s acao=%s escolha=%s confianca=%s",
            account.id,
            job.display_id,
            action,
            outcome.choice,
            _fmt(outcome.confidence),
        )
