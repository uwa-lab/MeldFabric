"""Process-local catalog populated by trusted import-time decorators."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel

from meldfabric.errors import CodeRegistrationError

RegistrationKind = Literal[
    "tool",
    "plugin",
    "hook",
    "hook_group",
    "intervention",
    "intervention_group",
    "graph_condition",
]

REGISTRATION_KINDS: tuple[RegistrationKind, ...] = (
    "tool",
    "plugin",
    "hook",
    "hook_group",
    "intervention",
    "intervention_group",
    "graph_condition",
)

_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}")


@dataclass(frozen=True, slots=True)
class MemberDefinition:
    """One code-owned callback or tool nested inside a class registration."""

    kind: Literal["hook", "intervention", "tool"]
    attribute: str
    event_types: tuple[type[Any], ...] = field(default_factory=tuple, repr=False)
    priority: float = 0
    payload: Any = field(default=None, repr=False, compare=False)

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "attribute": self.attribute,
        }
        if self.event_types:
            result["events"] = [event.__name__ for event in self.event_types]
        if self.kind == "hook":
            result["priority"] = self.priority
        if self.kind == "tool" and self.payload is not None:
            result.update(
                {
                    "name": self.payload.name,
                    "description": self.payload.description,
                    "input_schema": self.payload.input_schema,
                    "return_schema": self.payload.return_schema,
                }
            )
        return result


@dataclass(frozen=True, slots=True)
class RegistrationDefinition:
    """Immutable definition produced by one top-level registration decorator."""

    id: str
    kind: RegistrationKind
    name: str
    description: str
    python_path: str
    module: str
    target: Any = field(repr=False, compare=False)
    config_model: type[BaseModel] | None = field(default=None, repr=False, compare=False)
    members: tuple[MemberDefinition, ...] = field(default_factory=tuple)
    default_on_error: Literal["throw", "proceed", "deny"] = "throw"
    payload: Any = field(default=None, repr=False, compare=False)

    @property
    def config_schema(self) -> dict[str, Any] | None:
        return self.config_model.model_json_schema() if self.config_model is not None else None

    def contract(self) -> dict[str, Any]:
        """Return the public runtime contract, excluding implementation location."""

        payload: dict[str, Any] | None = None
        if self.kind == "tool" and self.payload is not None:
            payload = {
                "name": self.payload.name,
                "description": self.payload.description,
                "input_schema": self.payload.input_schema,
                "return_schema": self.payload.return_schema,
                "requires_sandbox": self.payload.requires_sandbox,
            }
        members = []
        for member in self.members:
            summary = member.summary()
            summary.pop("attribute", None)
            members.append(summary)
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "config_schema": self.config_schema,
            "default_on_error": self.default_on_error,
            "members": members,
            "payload": payload,
        }

    @property
    def digest(self) -> str:
        serialized = json.dumps(
            self.contract(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(serialized.encode()).hexdigest()

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "python_path": self.python_path,
            "module": self.module,
            "registration_digest": self.digest,
            "config_schema": self.config_schema,
            "default_on_error": self.default_on_error,
            "members": [member.summary() for member in self.members],
        }


def stable_registration_id(kind: RegistrationKind, target: Any) -> str:
    module = "__main__" if target.__module__ == "__mp_main__" else target.__module__
    python_path = f"{module}:{target.__qualname__}"
    digest = hashlib.sha256(f"{kind}\x1f{python_path}".encode()).hexdigest()[:16]
    return f"{kind}_{digest}"


def validate_registration_name(name: str) -> str:
    value = name.strip()
    if not _NAME_PATTERN.fullmatch(value):
        raise CodeRegistrationError(
            "registration name must start with an ASCII letter and contain only letters, "
            "digits, '.', '_' or '-'",
            details={"name": name},
        )
    return value


class CodeRegistrationCatalog:
    """Thread-safe singleton catalog shared by all AgentKit instances in one process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[RegistrationKind, dict[str, RegistrationDefinition]] = {
            kind: {} for kind in REGISTRATION_KINDS
        }
        self._ids: dict[str, RegistrationDefinition] = {}
        self._generation = 0
        self._kind_generations: dict[RegistrationKind, int] = {kind: 0 for kind in self._items}

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def register(self, definition: RegistrationDefinition) -> None:
        name = validate_registration_name(definition.name)
        if definition.name != name:
            raise AssertionError("registration definitions must contain normalized names")
        with self._lock:
            existing_id = self._ids.get(definition.id)
            if existing_id is not None and (
                existing_id.kind != definition.kind
                or existing_id.python_path != definition.python_path
            ):
                raise CodeRegistrationError(
                    f"registration id {definition.id!r} is already owned by "
                    f"{existing_id.kind} registration {existing_id.python_path}",
                    details={
                        "registration_id": definition.id,
                        "existing_kind": existing_id.kind,
                        "incoming_kind": definition.kind,
                        "existing": existing_id.python_path,
                        "incoming": definition.python_path,
                    },
                )
            # Re-importing the same source replaces the callable without leaving stale entries.
            self._items[definition.kind][definition.id] = definition
            self._ids[definition.id] = definition
            self._generation += 1
            self._kind_generations[definition.kind] += 1

    def get(self, kind: RegistrationKind, registration_id: str) -> RegistrationDefinition | None:
        with self._lock:
            return self._items[kind].get(registration_id)

    def by_id(self, registration_id: str) -> RegistrationDefinition | None:
        with self._lock:
            return self._ids.get(registration_id)

    def snapshot(
        self,
        kind: RegistrationKind | None = None,
    ) -> tuple[int, tuple[RegistrationDefinition, ...]]:
        with self._lock:
            if kind is not None:
                values = tuple(self._items[kind].values())
                generation = self._kind_generations[kind]
            else:
                values = tuple(
                    item for collection in self._items.values() for item in collection.values()
                )
                generation = self._generation
            return generation, tuple(
                sorted(values, key=lambda item: (item.kind, item.name, item.id))
            )

    def items(self, kind: RegistrationKind) -> MappingProxyType[str, RegistrationDefinition]:
        with self._lock:
            return MappingProxyType(dict(self._items[kind]))

    def extension_names(self) -> dict[str, list[str]]:
        with self._lock:
            return {
                "plugin": sorted(self._items["plugin"]),
                "hook": sorted([*self._items["hook"], *self._items["hook_group"]]),
                "intervention": sorted(
                    [
                        *self._items["intervention"],
                        *self._items["intervention_group"],
                    ]
                ),
                "graph_condition": sorted(self._items["graph_condition"]),
            }

    def catalog(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            result: dict[str, list[dict[str, Any]]] = {
                "tool": [],
                "plugin": [],
                "hook": [],
                "intervention": [],
                "graph_condition": [],
            }
            for definition in self._items["tool"].values():
                result["tool"].append(definition.summary())
            for definition in self._items["plugin"].values():
                result["plugin"].append(definition.summary())
            hook_definitions = (
                *self._items["hook"].values(),
                *self._items["hook_group"].values(),
            )
            for definition in hook_definitions:
                result["hook"].append(definition.summary())
            intervention_definitions = (
                *self._items["intervention"].values(),
                *self._items["intervention_group"].values(),
            )
            for definition in intervention_definitions:
                result["intervention"].append(definition.summary())
            for definition in self._items["graph_condition"].values():
                result["graph_condition"].append(definition.summary())
            for items in result.values():
                items.sort(key=lambda item: (item["name"], item["id"]))
            return result

    def modules(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(
                    {
                        item.module
                        for collection in self._items.values()
                        for item in collection.values()
                    }
                )
            )

    def digest(self) -> str:
        with self._lock:
            payload = [
                {
                    "id": item.id,
                    "registration_digest": item.digest,
                }
                for collection in self._items.values()
                for item in collection.values()
            ]
        serialized = json.dumps(
            sorted(payload, key=lambda item: item["id"]),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(serialized.encode()).hexdigest()


catalog = CodeRegistrationCatalog()
