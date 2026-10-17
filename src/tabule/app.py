from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping

from fastapi import FastAPI, HTTPException, Request

from . import signature
from .chatwoot import WEBHOOK_EVENT, ChatwootClient
from .config import AccountConfig, Settings, load_accounts
from .decisions import DecisionsClient
from .pipeline import Pipeline
from .service import Runner
from .store import Store

logger = logging.getLogger("tabule")


def build_pipeline(settings: Settings, accounts: Mapping[int, AccountConfig], store: Store) -> Pipeline:
    def client_factory(account: AccountConfig) -> ChatwootClient:
        return ChatwootClient(
            settings.chatwoot_base_url,
            account.api_token,
            account.id,
            timeout=settings.http_timeout_seconds,
        )

    decisions = DecisionsClient(
        settings.openai_api_key or "",
        settings.openai_model,
        timeout=settings.http_timeout_seconds,
    )
    return Pipeline(
        settings=settings,
        accounts=accounts,
        store=store,
        client_factory=client_factory,
        decisions=decisions,
    )


def create_app(
    *,
    settings: Settings | None = None,
    accounts: Mapping[int, AccountConfig] | None = None,
    account_errors: Mapping[int, str] | None = None,
    store: Store | None = None,
    pipeline: Pipeline | None = None,
    start_workers: bool = True,
) -> FastAPI:
    if settings is None:
        settings = Settings.from_env()
    if accounts is None:
        accounts, account_errors = load_accounts(settings)
    disabled = dict(account_errors or {})
    store = store if store is not None else Store(settings.db_path)
    pipeline = pipeline if pipeline is not None else build_pipeline(settings, accounts, store)
    runner = Runner(settings=settings, store=store, pipeline=pipeline, accounts=accounts)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if start_workers:
            runner.start()
        yield
        runner.stop()

    app = FastAPI(title="tabule", lifespan=lifespan)

    @app.post("/tabule/webhook/{account_id}")
    async def receive_webhook(account_id: int, request: Request) -> dict[str, Any]:
        account = accounts.get(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="conta não habilitada")
        body = await request.body()
        if len(body) > settings.webhook_max_body_bytes:
            raise HTTPException(status_code=413, detail="payload grande demais")
        headers = request.headers
        if not signature.verify(
            account.webhook_secret,
            body,
            headers.get("x-chatwoot-timestamp"),
            headers.get("x-chatwoot-signature"),
            now=int(time.time()),
        ):
            raise HTTPException(status_code=401, detail="assinatura inválida")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="JSON inválido") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="payload inesperado")
        account_info = payload.get("account")
        payload_account_id = account_info.get("id") if isinstance(account_info, dict) else None
        if payload_account_id != account_id:
            raise HTTPException(status_code=403, detail="conta do payload não confere com a URL")
        if payload.get("event") != WEBHOOK_EVENT:
            return {"status": "ignored", "reason": "evento"}
        if payload.get("status") != "resolved":
            return {"status": "ignored", "reason": "status"}
        inbox_id = payload.get("inbox_id")
        if not isinstance(inbox_id, int) or inbox_id not in account.inbox_ids:
            return {"status": "ignored", "reason": "inbox"}
        display_id = payload.get("id")
        if not isinstance(display_id, int):
            raise HTTPException(status_code=400, detail="id da conversa ausente")
        result = store.enqueue_webhook(
            account_id,
            display_id,
            delivery_id=headers.get("x-chatwoot-delivery"),
            delay=settings.resolution_delay_seconds,
        )
        logger.info("webhook conta=%s conversa=%s resultado=%s", account_id, display_id, result)
        return {"status": result}

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "contas": {str(i): {"nome": a.name, "modo": a.mode} for i, a in accounts.items()},
            "desabilitadas": {str(i): message for i, message in disabled.items()},
            "fila": store.queue_summary(),
        }

    return app
