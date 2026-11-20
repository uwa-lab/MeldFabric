"""Unambiguous encodings for caller-controlled composite identities."""

from __future__ import annotations

import hashlib
import json


def identity_digest(*parts: str) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
