import httpx

from tabule.chatwoot import ChatwootClient
from tabule.cli import _sample_human_tabulated
from tabule.config import AccountConfig


def test_evaluation_sample_uses_only_human_values_and_skips_excluded_ids():
    conversations = [
        {"id": 1, "inbox_id": 16, "status": "resolved", "custom_attributes": {"tabulacao": "A"}, "last_activity_at": 5},
        {"id": 2, "inbox_id": 16, "status": "resolved", "custom_attributes": {"tabulacao": "A"}, "last_activity_at": 4},
        {"id": 3, "inbox_id": 16, "status": "resolved", "custom_attributes": {"tabulacao": "B"}, "last_activity_at": 3},
        {"id": 4, "inbox_id": 99, "status": "resolved", "custom_attributes": {"tabulacao": "A"}, "last_activity_at": 2},
        {"id": 5, "inbox_id": 16, "status": "resolved", "custom_attributes": {}, "last_activity_at": 1},
    ]

    def handler(request):
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={"data": {"meta": {}, "payload": conversations if page == 1 else []}})

    client = ChatwootClient("https://omni.example", "tok", 9, transport=httpx.MockTransport(handler))
    account = AccountConfig(
        id=9,
        name="FDC",
        mode="shadow",
        inbox_ids=frozenset({16}),
        attribute_key="tabulacao",
        review_label="tabulacao-revisar",
        confidence_threshold=0.8,
        context="",
        options={},
        api_token="tok",
        webhook_secret="sec",
    )
    sample = _sample_human_tabulated(client, account, ["A", "B"], limit=10, pages=3, exclude={2})
    assert [conversation["id"] for conversation in sample] == [1, 3]
