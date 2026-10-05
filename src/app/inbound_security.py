import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class InboundLimits:
    raw_mime_bytes: int = 25 * 1024 * 1024
    attachment_bytes: int = 10 * 1024 * 1024
    total_attachment_bytes: int = 25 * 1024 * 1024
    attachment_count: int = 20

    @classmethod
    def from_env(cls) -> "InboundLimits":
        return cls(
            raw_mime_bytes=_env_int("RESEND_MAX_RAW_MIME_BYTES", 25 * 1024 * 1024),
            attachment_bytes=_env_int("RESEND_MAX_ATTACHMENT_BYTES", 10 * 1024 * 1024),
            total_attachment_bytes=_env_int("RESEND_MAX_TOTAL_ATTACHMENT_BYTES", 25 * 1024 * 1024),
            attachment_count=_env_int("RESEND_MAX_ATTACHMENT_COUNT", 20),
        )


MAGIC_BYTES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "application/zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
}


def validate_magic_bytes(content_type: str, content: bytes) -> None:
    normalized = content_type.split(";", 1)[0].strip().casefold()
    signatures = MAGIC_BYTES.get(normalized)
    if signatures and not any(content.startswith(signature) for signature in signatures):
        raise ValueError(f"attachment content does not match declared type {normalized}")


def redact_error(message: str) -> str:
    sanitized = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", message)
    sanitized = re.sub(r"(?i)\b(?:re|whsec)_[A-Za-z0-9_+/-]+", "[credential]", sanitized)
    sanitized = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email]", sanitized)
    sanitized = re.sub(r"https?://\S+", "[url]", sanitized)
    sanitized = re.sub(r"[\x00-\x1f\x7f]", " ", sanitized)
    return sanitized[:1000]


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed
