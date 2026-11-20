"""Canonical names for model-facing identifiers, separate from labels and content."""

from __future__ import annotations

import hashlib
import re
import unicodedata


def model_name(value: str) -> str:
    """Return an idempotent, provider-compatible function/Agent name (max 64 chars).

    Whitespace/punctuation becomes underscores. Non-ASCII names retain a stable suffix
    so unrelated human labels do not collapse to the same empty ASCII identifier.
    Callers keep the original text in display_name; namespace collisions remain errors.
    """
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    original = unicodedata.normalize("NFKC", value).strip()
    if not original:
        raise ValueError("name must not be blank")
    result = re.sub(r"[^A-Za-z0-9_-]+", "_", original)
    suffix = ""
    if not any(char.isascii() and char.isalnum() for char in original):
        result = "name"
    if not original.isascii() or len(result) > 64:
        suffix = "_" + hashlib.sha256(original.encode()).hexdigest()[:10]
    return result[: 64 - len(suffix)] + suffix


def version_name(value: str) -> str:
    """Prompt versions are local labels; they need not be Python identifiers."""
    if not isinstance(value, str):
        raise ValueError("version must be a string")
    result = re.sub(r"\s+", "_", value.strip())
    if not result or len(result) > 128 or any(unicodedata.category(c) == "Cc" for c in result):
        raise ValueError("version must contain 1-128 characters without control characters")
    return result


def http_header_name(value: str) -> str:
    result = value.strip()
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", result):
        raise ValueError("HTTP header name must be a valid token without whitespace")
    return result


def http_headers(value: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in value.items():
        key = http_header_name(key)
        if "\r" in item or "\n" in item:
            raise ValueError("HTTP header values must not contain CR or LF")
        if key.lower() in {header.lower() for header in result}:
            raise ValueError("duplicate HTTP header names after normalization")
        result[key] = item
    return result


def management_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("management.path must be a string")
    result = re.sub(r"\s+", "_", value.strip()).rstrip("/")
    if not result.startswith("/") or result == "":
        raise ValueError("management.path must be an absolute non-root URL path")
    if any(part in {"", ".", ".."} for part in result[1:].split("/")):
        raise ValueError("management.path must not contain empty, '.' or '..' segments")
    if not re.fullmatch(r"/[A-Za-z0-9_./~-]+", result):
        raise ValueError(
            "management.path requires URL-safe ASCII segments without query or fragment"
        )
    return result
