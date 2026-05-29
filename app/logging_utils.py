from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SENSITIVE_KEYS = {
    "authorization",
    "client_secret",
    "cpf",
    "cnpj",
    "email",
    "phone_number",
    "x-hasura-admin-secret",
}


def sanitize_for_log(value: Any, *, max_string_length: int = 500) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in SENSITIVE_KEYS:
                sanitized[key_text] = "***"
            else:
                sanitized[key_text] = sanitize_for_log(
                    item, max_string_length=max_string_length
                )
        return sanitized

    if isinstance(value, list):
        return [
            sanitize_for_log(item, max_string_length=max_string_length)
            for item in value
        ]

    if isinstance(value, str) and len(value) > max_string_length:
        return f"{value[:max_string_length]}...<truncated>"

    return value
