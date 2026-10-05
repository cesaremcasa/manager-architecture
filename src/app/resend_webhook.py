import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any


class WebhookVerificationError(ValueError):
    pass


def verify_resend_webhook(
    payload: bytes,
    *,
    svix_id: str | None,
    svix_timestamp: str | None,
    svix_signature: str | None,
    secret: str,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> Mapping[str, Any]:
    """Verify Resend's Svix signature against the untouched HTTP body."""
    if not svix_id or not svix_timestamp or not svix_signature or not secret:
        raise WebhookVerificationError("missing webhook verification data")
    try:
        timestamp = int(svix_timestamp)
    except ValueError as error:
        raise WebhookVerificationError("invalid webhook timestamp") from error
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        raise WebhookVerificationError("webhook timestamp outside tolerance")
    encoded_secret = secret.removeprefix("whsec_")
    try:
        signing_key = base64.b64decode(encoded_secret + "===")
    except (binascii.Error, ValueError) as error:
        raise WebhookVerificationError("invalid webhook secret") from error
    signed_content = b".".join((svix_id.encode(), svix_timestamp.encode(), payload))
    expected = base64.b64encode(hmac.new(signing_key, signed_content, hashlib.sha256).digest()).decode()
    candidates = [part.split(",", 1)[1] for part in svix_signature.split() if part.startswith("v1,")]
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise WebhookVerificationError("invalid webhook signature")
    try:
        event = json.loads(payload)
    except json.JSONDecodeError as error:
        raise WebhookVerificationError("webhook body is not valid JSON") from error
    if not isinstance(event, dict):
        raise WebhookVerificationError("webhook body must be an object")
    return event

