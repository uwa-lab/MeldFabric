"""Immutable values returned by runtime storage repositories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class RemoteSessionMapping:
    id: str
    remote_thread_id: str


@dataclass(frozen=True, slots=True)
class StoredRemoteInteraction:
    id: str
    remote_agent_id: str
    user_id: str
    parent_thread_id: str
    remote_thread_id: str
    remote_run_id: str
    remote_context: dict[str, Any]
    stages: list[dict[str, Any]]
    status: str


@dataclass(frozen=True, slots=True)
class StoredAgentRun:
    id: str
    run_id: str
    agent_id: str
    user_id: str
    external_thread_id: str
    internal_thread_id: str
    config_generation: int
    status: str
    cancel_requested: bool
    interrupt_ids: tuple[str, ...]
    owner_instance_id: str | None = None
    fencing_token: int | None = None
    terminal_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeLease:
    id: str
    workspace_id: str
    agent_id: str
    user_id: str
    thread_id: str
    owner_instance_id: str
    run_id: str
    fencing_token: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class StoredToolExecution:
    id: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    status: str


@dataclass(frozen=True, slots=True)
class StoredA2ATask:
    id: str
    workspace_id: str
    agent_id: str
    access_key_id: str
    user_id: str
    context_id: str
    thread_id: str
    run_id: str
    config_generation: int
    state: str
    task: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    owner_instance_id: str | None = None


__all__ = [
    "RemoteSessionMapping",
    "RuntimeLease",
    "StoredA2ATask",
    "StoredAgentRun",
    "StoredRemoteInteraction",
    "StoredToolExecution",
]
