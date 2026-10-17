from __future__ import annotations

import json
from typing import Any

import httpx


def json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def choice_answer(value: str, confidence: float) -> dict[str, Any]:
    return {
        "type": "choice",
        "name": "tabulacao",
        "choice": value,
        "probabilities": [{"value": value, "probability": confidence}],
        "confidence": confidence,
    }


class FakeChatwoot:
    def __init__(
        self,
        *,
        conversation: dict[str, Any],
        messages: list[dict[str, Any]],
        definitions: list[dict[str, Any]],
        labels: list[str] | None = None,
    ) -> None:
        self.conversation = conversation
        self.messages = messages
        self.definitions = definitions
        self.labels = list(labels or [])
        self.attribute_writes: list[dict[str, Any]] = []
        self.label_writes: list[list[str]] = []
        self.destroyed: list[list[str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/custom_attribute_definitions"):
            return json_response(self.definitions)
        if "/conversations/" in path and path.endswith("/messages"):
            after = int(request.url.params.get("after", "0"))
            page = [m for m in self.messages if m["id"] > after][:100]
            return json_response({"meta": {}, "payload": page})
        if "/conversations/" in path and path.endswith("/labels"):
            if request.method == "POST":
                labels = json.loads(request.content)["labels"]
                self.label_writes.append(labels)
                self.labels = list(labels)
            return json_response({"payload": list(self.labels)})
        if request.method == "POST" and path.endswith("/custom_attributes"):
            body = json.loads(request.content)
            self.attribute_writes.append(body)
            self.conversation["custom_attributes"].update(body["custom_attributes"])
            return json_response({"custom_attributes": dict(self.conversation["custom_attributes"])})
        if request.method == "POST" and path.endswith("/destroy_custom_attributes"):
            keys = json.loads(request.content)["custom_attributes"]
            self.destroyed.append(keys)
            for key in keys:
                self.conversation["custom_attributes"].pop(key, None)
            return json_response({"custom_attributes": dict(self.conversation["custom_attributes"])})
        if request.method == "GET" and "/conversations/" in path:
            snapshot = dict(self.conversation)
            snapshot["custom_attributes"] = dict(self.conversation["custom_attributes"])
            return json_response(snapshot)
        return json_response({"error": "rota não mapeada no fake"}, status=404)


class FakeDecisions:
    def __init__(self, answer: dict[str, Any] | None) -> None:
        self.answer = answer
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return json_response({"answers": [self.answer]})
