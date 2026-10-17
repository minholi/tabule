from __future__ import annotations

import json
from typing import Any

import httpx

WEBHOOK_EVENT = "conversation_status_changed"
MESSAGE_PAGE_SIZE = 100
MAX_MESSAGE_PAGES = 200


class ChatwootError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"Chatwoot respondeu {status_code}: {detail}")
        self.status_code = status_code


class RemoteConfigError(Exception):
    pass


def parse_list_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                return parse_list_values(json.loads(text))
            except json.JSONDecodeError:
                pass
        return [line.strip() for line in text.splitlines() if line.strip()]
    return []


def attribute_options(definitions: list[dict[str, Any]], key: str) -> list[str]:
    for definition in definitions:
        if definition.get("attribute_key") != key:
            continue
        if definition.get("attribute_display_type") != "list":
            raise RemoteConfigError(f"o atributo '{key}' não é do tipo lista")
        values = parse_list_values(definition.get("attribute_values"))
        if not values:
            raise RemoteConfigError(f"o atributo '{key}' não tem opções cadastradas")
        return values
    raise RemoteConfigError(f"o atributo '{key}' não existe nesta conta")


class ChatwootClient:
    def __init__(
        self,
        base_url: str,
        api_token: str,
        account_id: int,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.account_id = account_id
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"api_access_token": api_token, "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _path(self, suffix: str) -> str:
        return f"/api/v1/accounts/{self.account_id}{suffix}"

    def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise ChatwootError(response.status_code, response.text[:300])
        return response.json() if response.content else None

    def profile(self) -> dict[str, Any]:
        return self._send("GET", "/api/v1/profile")

    def account_membership(self) -> dict[str, Any] | None:
        for account in self.profile().get("accounts", []):
            if account.get("id") == self.account_id:
                return account
        return None

    def attribute_definitions(self) -> list[dict[str, Any]]:
        return self._send("GET", self._path("/custom_attribute_definitions"), params={"attribute_model": 0})

    def get_conversation(self, display_id: int) -> dict[str, Any]:
        return self._send("GET", self._path(f"/conversations/{display_id}"))

    def list_resolved_conversations(self, page: int) -> list[dict[str, Any]]:
        data = self._send("GET", self._path("/conversations"), params={"status": "resolved", "page": page})
        container = data.get("data", data) if isinstance(data, dict) else {}
        return container.get("payload", []) if isinstance(container, dict) else []

    def _messages_page(self, display_id: int, after_id: int) -> list[dict[str, Any]]:
        data = self._send("GET", self._path(f"/conversations/{display_id}/messages"), params={"after": after_id})
        return data.get("payload", []) if isinstance(data, dict) else []

    def iter_messages(self, display_id: int) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        after_id = 0
        for _ in range(MAX_MESSAGE_PAGES):
            page = self._messages_page(display_id, after_id)
            if not page:
                break
            messages.extend(page)
            after_id = page[-1]["id"]
        return messages

    def update_custom_attributes(self, display_id: int, attributes: dict[str, Any]) -> None:
        self._send(
            "POST",
            self._path(f"/conversations/{display_id}/custom_attributes"),
            json={"custom_attributes": attributes, "merge": True},
        )

    def destroy_custom_attributes(self, display_id: int, keys: list[str]) -> None:
        self._send(
            "POST",
            self._path(f"/conversations/{display_id}/destroy_custom_attributes"),
            json={"custom_attributes": keys},
        )

    def get_conversation_labels(self, display_id: int) -> list[str]:
        data = self._send("GET", self._path(f"/conversations/{display_id}/labels"))
        return list(data.get("payload", [])) if isinstance(data, dict) else []

    def set_conversation_labels(self, display_id: int, labels: list[str]) -> None:
        self._send("POST", self._path(f"/conversations/{display_id}/labels"), json={"labels": labels})

    def account_labels(self) -> list[dict[str, Any]]:
        data = self._send("GET", self._path("/labels"))
        return data.get("payload", []) if isinstance(data, dict) else []

    def webhooks(self) -> list[dict[str, Any]]:
        data = self._send("GET", self._path("/webhooks"))
        return data if isinstance(data, list) else []
