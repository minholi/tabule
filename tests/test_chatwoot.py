import json

import httpx
import pytest

from tabule.chatwoot import ChatwootClient, RemoteConfigError, attribute_options, parse_list_values


def make_client(handler):
    return ChatwootClient("https://omni.example", "tok", 9, transport=httpx.MockTransport(handler))


def test_parse_list_values_accepts_lists_json_and_lines():
    assert parse_list_values(["A", "B"]) == ["A", "B"]
    assert parse_list_values('["A", "B"]') == ["A", "B"]
    assert parse_list_values("A\nB\n\n") == ["A", "B"]
    assert parse_list_values(None) == []


def test_attribute_options_requires_list_type_and_existing_key():
    definitions = [{"attribute_key": "tabulacao", "attribute_display_type": "text", "attribute_values": ""}]
    with pytest.raises(RemoteConfigError, match="não é do tipo lista"):
        attribute_options(definitions, "tabulacao")
    with pytest.raises(RemoteConfigError, match="não existe"):
        attribute_options([], "tabulacao")


def test_iter_messages_follows_after_cursor_until_empty_page():
    all_messages = [{"id": i, "content": f"m{i}"} for i in range(1, 251)]
    calls = []

    def handler(request):
        after = int(request.url.params["after"])
        calls.append(after)
        page = [m for m in all_messages if m["id"] > after][:100]
        return httpx.Response(200, json={"meta": {}, "payload": page})

    messages = make_client(handler).iter_messages(101)
    assert [m["id"] for m in messages] == list(range(1, 251))
    assert calls == [0, 100, 200, 250]


def test_update_custom_attributes_sends_merge_and_token():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["token"] = request.headers["api_access_token"]
        return httpx.Response(200, json={"custom_attributes": {}})

    make_client(handler).update_custom_attributes(101, {"tabulacao": "A"})
    assert seen["path"] == "/api/v1/accounts/9/conversations/101/custom_attributes"
    assert seen["body"] == {"custom_attributes": {"tabulacao": "A"}, "merge": True}
    assert seen["token"] == "tok"


def test_list_resolved_conversations_reads_data_payload():
    def handler(request):
        assert request.url.params["status"] == "resolved"
        return httpx.Response(200, json={"data": {"meta": {}, "payload": [{"id": 7}]}})

    assert make_client(handler).list_resolved_conversations(1) == [{"id": 7}]


def test_parse_webhooks_accepts_known_response_shapes():
    from tabule.chatwoot import parse_webhooks

    hook = {"id": 1, "url": "https://omni.example/tabule/webhook/9"}
    assert parse_webhooks([hook]) == [hook]
    assert parse_webhooks({"payload": [hook]}) == [hook]
    assert parse_webhooks({"payload": {"webhooks": [hook]}}) == [hook]
    assert parse_webhooks({"webhooks": [hook]}) == [hook]
    assert parse_webhooks({"payload": {}}) == []
    assert parse_webhooks(None) == []
