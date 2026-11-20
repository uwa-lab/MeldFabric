"""Small, protocol-neutral helpers for turning runtime values into JSON data."""

from __future__ import annotations

import base64
from dataclasses import asdict, is_dataclass
from typing import Any


def jsonable(
    value: Any,
    *,
    include_object_id: bool = False,
) -> Any:
    """Recursively convert common SDK and application values into JSON-safe data.

    Protocol adapters may request ``include_object_id`` for SDK models whose serializer
    omits an otherwise public ``id`` attribute.  Keeping that exception explicit avoids
    subtly different serializers growing in every transport module.
    """

    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
        if include_object_id and isinstance(payload, dict):
            identifier = getattr(value, "id", None)
            if identifier is not None:
                payload.setdefault("id", str(identifier))
        return jsonable(payload, include_object_id=include_object_id)
    if hasattr(value, "as_dict"):
        return jsonable(value.as_dict(), include_object_id=include_object_id)
    if hasattr(value, "to_dict"):
        return jsonable(value.to_dict(), include_object_id=include_object_id)
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value), include_object_id=include_object_id)
    if isinstance(value, dict):
        return {
            str(key): jsonable(item, include_object_id=include_object_id)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item, include_object_id=include_object_id) for item in value]
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = ["jsonable"]
