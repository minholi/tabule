import json

import httpx
import pytest

from tabule.describer import DescriberError, DescriptionWriter

OPTIONS = ["RECUSA FINANCEIRO", "PAROU DE RESPONDER PRODUTO"]


def writer(payload, status=200):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(status, json=payload)

    return DescriptionWriter("key", "modelo", transport=httpx.MockTransport(handler)), seen


def completion(content, refusal=None):
    return {"choices": [{"message": {"content": content, "refusal": refusal}}]}


def test_describe_returns_context_and_descriptions_and_sends_strict_schema():
    answer = {"context": "Pós-graduação.", "options": {OPTIONS[0]: "Recusou por dinheiro.", OPTIONS[1]: "Sumiu."}}
    client, seen = writer(completion(json.dumps(answer)))
    result = client.describe(about="FDC, WhatsApp", options=OPTIONS)
    assert result.context == "Pós-graduação."
    assert result.options == answer["options"]
    body = seen[0]
    assert body["model"] == "modelo"
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["options"]["required"] == OPTIONS
    assert "FDC, WhatsApp" in body["messages"][1]["content"]


@pytest.mark.parametrize(
    "options",
    [
        {OPTIONS[0]: "x"},
        {OPTIONS[0]: "x", OPTIONS[1]: "y", "INVENTADA": "z"},
        {OPTIONS[0]: "x", OPTIONS[1]: "  "},
    ],
)
def test_describe_rejects_mismatched_or_empty_options(options):
    client, _ = writer(completion(json.dumps({"context": "c", "options": options})))
    with pytest.raises(DescriberError):
        client.describe(about="a", options=OPTIONS)


def test_describe_rejects_http_error_refusal_and_bad_json():
    with pytest.raises(DescriberError):
        writer({"error": "x"}, status=500)[0].describe(about="a", options=OPTIONS)
    with pytest.raises(DescriberError):
        writer(completion(None, refusal="não posso"))[0].describe(about="a", options=OPTIONS)
    with pytest.raises(DescriberError):
        writer(completion("não é json"))[0].describe(about="a", options=OPTIONS)
