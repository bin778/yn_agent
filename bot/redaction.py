"""Small, dependency-free redaction helpers for persisted previews and logs."""

from __future__ import annotations

import re
from typing import Any


REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"password|passwd|secret|credential|cookie|client|case|matter|name|"
    r"address|birth|resident|rrn|phone|email|contact)",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:01[016789])[- ]?\d{3,4}[- ]?\d{4}(?!\d)")
_RRN = re.compile(r"(?<!\d)\d{6}[- ]?[1-4]\d{6}(?!\d)")
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def redact_text(value: str) -> str:
    """Redact common credentials and Korean personal identifiers in text."""

    value = _BEARER.sub(f"Bearer {REDACTED}", value)
    value = _EMAIL.sub(REDACTED, value)
    value = _PHONE.sub(REDACTED, value)
    return _RRN.sub(REDACTED, value)


def redact_data(value: Any) -> Any:
    """Return a recursively redacted, JSON-compatible preview."""

    if isinstance(value, dict):
        return {
            str(key): REDACTED if _SENSITIVE_KEY.search(str(key)) else redact_data(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def summarize_for_log(value: Any) -> Any:
    """Minimize free text while preserving structure and numeric controls.

    Pattern redaction alone cannot reliably identify names or legal narratives.
    Persisted execution logs therefore record free-text length, not its contents.
    """

    if isinstance(value, dict):
        return {
            str(key): REDACTED if _SENSITIVE_KEY.search(str(key)) else summarize_for_log(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [summarize_for_log(item) for item in value]
    if isinstance(value, str):
        redacted = redact_text(value)
        if redacted != value:
            return redacted
        return {"type": "text", "length": len(value)}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return {"type": type(value).__name__, "length": len(text)}
