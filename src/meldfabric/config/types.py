"""Shared value objects for configuration persistence and coordination."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meldfabric.config.database import ControlDatabaseSpec
from meldfabric.config.models import WorkspaceConfig


@dataclass(frozen=True, slots=True)
class LoadedWorkspace:
    config: WorkspaceConfig
    state_dir: Path
    database: ControlDatabaseSpec
    # Plaintext is intentionally available only in the process that initialized it.
    revealed_admin_key: str | None = None
    admin_key_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    id: str
    version: int
    source_generation: int
    label: str | None
    note: str | None
    fingerprint: str
    created_at: Any


@dataclass(frozen=True, slots=True)
class GenerationState:
    workspace_id: str
    latest_generation: int
    active_generation: int


@dataclass(frozen=True, slots=True)
class EncryptedSecretWrite:
    """One encrypted credential mutation committed with a configuration generation."""

    secret_id: str
    kind: str
    encrypted_value: bytes


__all__ = [
    "ConfigSnapshot",
    "EncryptedSecretWrite",
    "GenerationState",
    "LoadedWorkspace",
]
