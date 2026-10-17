from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ACTIVITY = 2
INCOMING = 0
TEMPLATE = 3
BOT_SENDERS = frozenset({"AgentBot", "Captain::Assistant"})
MAX_LINE_CHARS = 2000


def _speaker(message: dict[str, Any]) -> str:
    if message.get("private"):
        return "Nota interna"
    if message.get("message_type") == INCOMING:
        return "Cliente"
    if message.get("message_type") == TEMPLATE:
        return "Modelo enviado"
    if message.get("sender_type") in BOT_SENDERS:
        return "Bot"
    return "Atendente"


def _content(message: dict[str, Any]) -> str | None:
    text = " / ".join(line.strip() for line in str(message.get("content") or "").splitlines() if line.strip())
    if text:
        return text if len(text) <= MAX_LINE_CHARS else text[: MAX_LINE_CHARS - 1] + "…"
    attachments = message.get("attachments") or ([message["attachment"]] if message.get("attachment") else [])
    if attachments:
        kinds = sorted({str(item.get("file_type") or "arquivo") for item in attachments if isinstance(item, dict)})
        return "[anexo: " + ", ".join(kinds) + "]"
    return None


def _stamp(value: Any, tz: ZoneInfo) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "sem data"


def build_transcript(messages: list[dict[str, Any]], *, max_chars: int, timezone: str) -> str:
    tz = ZoneInfo(timezone)
    lines: list[str] = []
    for message in messages:
        if message.get("message_type") == ACTIVITY:
            continue
        content = _content(message)
        if content is None:
            continue
        lines.append(f"[{_stamp(message.get('created_at'), tz)}] {_speaker(message)}: {content}")
    if sum(len(line) + 1 for line in lines) <= max_chars:
        return "\n".join(lines)
    head_budget = max_chars // 4
    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head):]):
        if used + len(line) + 1 > max_chars - head_budget:
            break
        tail.append(line)
        used += len(line) + 1
    tail.reverse()
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join([*head, f"[... {omitted} mensagens omitidas ...]", *tail])
