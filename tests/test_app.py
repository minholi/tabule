import json
import time

import pytest
from fastapi.testclient import TestClient

from tabule import signature
from tabule.app import create_app
from tabule.config import AccountConfig, Settings
from tabule.store import Store

RESOLVED = {
    "event": "conversation_status_changed",
    "id": 101,
    "status": "resolved",
    "inbox_id": 16,
    "account": {"id": 9, "name": "FDC"},
}


@pytest.fixture
def app_client(tmp_path):
    settings = Settings(allowed_account_ids=(9,), chatwoot_base_url="https://omni.example", openai_api_key="sk-test")
    account = AccountConfig(
        id=9,
        name="FDC",
        mode="shadow",
        inbox_ids=frozenset({16}),
        attribute_key="tabulacao",
        review_label="tabulacao-revisar",
        confidence_threshold=0.8,
        context="teste",
        options={"A": "a", "B": "b"},
        api_token="tok-9",
        webhook_secret="sec-9",
    )
    store = Store(tmp_path / "app.db")
    app = create_app(settings=settings, accounts={9: account}, account_errors={}, store=store, start_workers=False)
    return TestClient(app), store


def post(client, account_id, payload, *, secret="sec-9", delivery="d-1", timestamp=None, corrupt=False):
    body = json.dumps(payload).encode()
    ts = str(int(time.time())) if timestamp is None else timestamp
    sig = signature.sign(secret, ts, body)
    if corrupt:
        sig = "sha256=" + "0" * 64
    return client.post(
        f"/tabule/webhook/{account_id}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Chatwoot-Timestamp": ts,
            "X-Chatwoot-Signature": sig,
            "X-Chatwoot-Delivery": delivery,
        },
    )


def test_unknown_account_is_not_found(app_client):
    client, _ = app_client
    assert post(client, 999, RESOLVED, secret="qualquer").status_code == 404


def test_bad_signature_is_unauthorized(app_client):
    client, store = app_client
    response = post(client, 9, RESOLVED, corrupt=True)
    assert response.status_code == 401
    assert store.get_job(9, 101) is None


def test_stale_signature_is_unauthorized(app_client):
    client, _ = app_client
    old = str(int(time.time()) - 3600)
    assert post(client, 9, RESOLVED, timestamp=old).status_code == 401


def test_payload_account_must_match_url(app_client):
    client, _ = app_client
    payload = {**RESOLVED, "account": {"id": 12, "name": "Outra"}}
    assert post(client, 9, payload).status_code == 403


def test_reopened_conversation_is_ignored(app_client):
    client, store = app_client
    response = post(client, 9, {**RESOLVED, "status": "open"})
    assert response.json() == {"status": "ignored", "reason": "status"}
    assert store.get_job(9, 101) is None


def test_inbox_outside_allowlist_is_ignored(app_client):
    client, _ = app_client
    response = post(client, 9, {**RESOLVED, "inbox_id": 99})
    assert response.json() == {"status": "ignored", "reason": "inbox"}


def test_resolved_conversation_is_queued_once(app_client):
    client, store = app_client
    assert post(client, 9, RESOLVED).json() == {"status": "queued"}
    assert post(client, 9, RESOLVED, delivery="d-2").json() == {"status": "duplicate"}
    job = store.get_job(9, 101)
    assert job.status == "pending"
    assert job.delivery_id == "d-1"


def test_health_lists_accounts_and_queue(app_client):
    client, _ = app_client
    body = client.get("/health").json()
    assert body["contas"] == {"9": {"nome": "FDC", "modo": "shadow"}}
    assert body["fila"] == {}
