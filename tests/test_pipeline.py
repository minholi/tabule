import httpx
import pytest

from fakes import FakeChatwoot, FakeDecisions, choice_answer
from tabule.chatwoot import ChatwootClient
from tabule.classifier import OptionsMismatchError
from tabule.config import AccountConfig, Settings
from tabule.decisions import DecisionsClient
from tabule.pipeline import Pipeline
from tabule.store import Store

DEFINITIONS = [
    {
        "attribute_key": "tabulacao",
        "attribute_display_type": "list",
        "attribute_values": ["RECUSA FINANCEIRO", "PAROU DE RESPONDER PRODUTO"],
    }
]
DESCRIPTIONS = {
    "RECUSA FINANCEIRO": "Recusou por motivo financeiro.",
    "PAROU DE RESPONDER PRODUTO": "Parou de responder sobre o produto.",
}
MESSAGES = [
    {"id": 1, "content": "Oi, quero saber o valor", "message_type": 0, "private": False, "sender_type": "Contact", "created_at": 1760000000},
    {"id": 2, "content": "Segue a tabela de valores", "message_type": 1, "private": False, "sender_type": "User", "created_at": 1760000060},
    {"id": 3, "content": "Podemos oferecer desconto", "message_type": 1, "private": True, "sender_type": "User", "created_at": 1760000120},
]


def make_conversation(**overrides):
    conversation = {"id": 101, "status": "resolved", "inbox_id": 16, "custom_attributes": {}}
    conversation.update(overrides)
    return conversation


def make_account(mode="live", threshold=0.8, descriptions=None):
    return AccountConfig(
        id=9,
        name="FDC",
        mode=mode,
        inbox_ids=frozenset({16}),
        attribute_key="tabulacao",
        review_label="tabulacao-revisar",
        confidence_threshold=threshold,
        context="Pós-graduação via WhatsApp.",
        options=dict(DESCRIPTIONS if descriptions is None else descriptions),
        api_token="tok-9",
        webhook_secret="sec-9",
    )


def make_pipeline(tmp_path, *, conversation, answer=None, account=None, labels=None):
    settings = Settings(
        allowed_account_ids=(9,),
        chatwoot_base_url="https://omni.example",
        openai_api_key="sk-test",
    )
    account = account or make_account()
    chatwoot = FakeChatwoot(conversation=conversation, messages=MESSAGES, definitions=DEFINITIONS, labels=labels)
    decisions = FakeDecisions(answer)
    store = Store(tmp_path / "tabule.db")
    pipeline = Pipeline(
        settings=settings,
        accounts={9: account},
        store=store,
        client_factory=lambda acc: ChatwootClient(
            "https://omni.example", acc.api_token, acc.id, transport=httpx.MockTransport(chatwoot.handler)
        ),
        decisions=DecisionsClient("sk-test", "gpt-6-luna", transport=httpx.MockTransport(decisions.handler)),
    )
    return pipeline, store, chatwoot, decisions


def run_job(store, pipeline, display_id=101):
    store.enqueue_webhook(9, display_id, delivery_id="d-1", delay=0)
    return pipeline.process(store.claim_next())


def test_live_mode_writes_choice_with_merge(tmp_path):
    pipeline, store, chatwoot, decisions = make_pipeline(
        tmp_path, conversation=make_conversation(), answer=choice_answer("RECUSA FINANCEIRO", 0.93)
    )
    assert run_job(store, pipeline) == "accept"
    assert chatwoot.attribute_writes == [{"custom_attributes": {"tabulacao": "RECUSA FINANCEIRO"}, "merge": True}]
    assert store.get_job(9, 101).status == "done"
    assert len(store.written_decisions(9, since=0)) == 1

    request = decisions.requests[0]
    assert request["model"] == "gpt-6-luna"
    assert request["questions"][0]["choices"][0] == {
        "value": "RECUSA FINANCEIRO",
        "description": "Recusou por motivo financeiro.",
    }
    assert "Cliente: Oi, quero saber o valor" in request["input"]
    assert "Nota interna: Podemos oferecer desconto" in request["input"]


def test_shadow_mode_never_writes(tmp_path):
    pipeline, store, chatwoot, _ = make_pipeline(
        tmp_path,
        conversation=make_conversation(),
        answer=choice_answer("RECUSA FINANCEIRO", 0.93),
        account=make_account(mode="shadow"),
    )
    assert run_job(store, pipeline) == "accept"
    assert chatwoot.attribute_writes == []
    assert chatwoot.label_writes == []
    assert [row["action"] for row in store.list_decisions(9)] == ["would_write"]


def test_low_confidence_in_live_mode_adds_review_label_only(tmp_path):
    pipeline, store, chatwoot, _ = make_pipeline(
        tmp_path, conversation=make_conversation(), answer=choice_answer("PAROU DE RESPONDER PRODUTO", 0.4)
    )
    assert run_job(store, pipeline) == "review"
    assert chatwoot.attribute_writes == []
    assert chatwoot.label_writes == [["tabulacao-revisar"]]
    assert [row["action"] for row in store.list_decisions(9)] == ["review_labeled"]


def test_existing_label_is_kept_when_adding_review_label(tmp_path):
    pipeline, store, chatwoot, _ = make_pipeline(
        tmp_path,
        conversation=make_conversation(),
        answer=choice_answer("PAROU DE RESPONDER PRODUTO", 0.4),
        labels=["lead-quente"],
    )
    run_job(store, pipeline)
    assert chatwoot.label_writes == [["lead-quente", "tabulacao-revisar"]]


def test_already_tabulated_conversation_skips_the_model(tmp_path):
    pipeline, store, _, decisions = make_pipeline(
        tmp_path,
        conversation=make_conversation(custom_attributes={"tabulacao": "RECUSA FINANCEIRO"}),
        answer=choice_answer("RECUSA FINANCEIRO", 0.99),
    )
    assert run_job(store, pipeline) == "skipped"
    assert decisions.requests == []
    assert store.get_job(9, 101).note == "já tabulada"


def test_reopened_conversation_is_not_classified(tmp_path):
    pipeline, store, _, decisions = make_pipeline(
        tmp_path, conversation=make_conversation(status="open"), answer=choice_answer("RECUSA FINANCEIRO", 0.99)
    )
    assert run_job(store, pipeline) == "not_resolved"
    assert decisions.requests == []
    assert store.get_job(9, 101).status == "not_resolved"


def test_option_without_description_blocks_classification(tmp_path):
    pipeline, store, _, decisions = make_pipeline(
        tmp_path,
        conversation=make_conversation(),
        answer=choice_answer("RECUSA FINANCEIRO", 0.99),
        account=make_account(descriptions={"RECUSA FINANCEIRO": "ok", "PAROU DE RESPONDER PRODUTO": ""}),
    )
    store.enqueue_webhook(9, 101, delivery_id=None, delay=0)
    with pytest.raises(OptionsMismatchError, match="PAROU DE RESPONDER PRODUTO"):
        pipeline.process(store.claim_next())
    assert decisions.requests == []
