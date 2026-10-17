from __future__ import annotations

import hashlib
import hmac

SIGNATURE_TOLERANCE_SECONDS = 300


def sign(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(
    secret: str,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    *,
    now: int,
    tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(now - sent_at) > tolerance:
        return False
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))
