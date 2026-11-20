"""MeldFabric Remote Protocol value objects."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from meldfabric.errors import (
    RemoteAgentCycleError,
    RemoteDelegationDepthError,
    RemoteDelegationLimitError,
)


class RemoteModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RemoteAgentDescriptor(RemoteModel):
    schema_version: str = "1"
    service_id: str
    agent_id: str
    name: str
    display_name: str | None = None
    description: str = ""
    status: Literal["active", "disabled"]
    config_generation: int
    protocols: list[str] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @property
    def node_id(self) -> str:
        return f"{self.service_id}/{self.agent_id}"


class RemoteCallContext(RemoteModel):
    call_chain_id: str = Field(default_factory=lambda: f"chain_{uuid.uuid4().hex}")
    delegation_id: str = Field(default_factory=lambda: f"delegation_{uuid.uuid4().hex}")
    visited: list[str] = Field(default_factory=list)
    depth: int = Field(default=0, ge=0)
    delegation_count: int = Field(default=0, ge=0)
    max_depth: int = Field(default=10, ge=1, le=100)
    max_delegations: int = Field(default=32, ge=1, le=1000)

    @classmethod
    def root(
        cls,
        *,
        service_id: str,
        agent_id: str,
        max_depth: int = 10,
        max_delegations: int = 32,
    ) -> RemoteCallContext:
        return cls(
            visited=[f"{service_id}/{agent_id}"],
            max_depth=max_depth,
            max_delegations=max_delegations,
        )

    def enter(self, node_id: str) -> RemoteCallContext:
        if node_id in self.visited:
            raise RemoteAgentCycleError(
                f"remote delegation would revisit {node_id!r}",
                details={"node_id": node_id, "visited": self.visited},
            )
        if self.depth > self.max_depth:
            raise RemoteDelegationDepthError(
                "remote delegation depth exceeded",
                details={"depth": self.depth, "max_depth": self.max_depth},
            )
        if self.delegation_count > self.max_delegations:
            raise RemoteDelegationLimitError(
                "remote delegation count exceeded",
                details={
                    "delegation_count": self.delegation_count,
                    "max_delegations": self.max_delegations,
                },
            )
        return self.model_copy(update={"visited": [*self.visited, node_id]})

    def delegate_to(self, node_id: str) -> RemoteCallContext:
        if node_id in self.visited:
            raise RemoteAgentCycleError(
                f"remote delegation would revisit {node_id!r}",
                details={"node_id": node_id, "visited": self.visited},
            )
        next_depth = self.depth + 1
        next_count = self.delegation_count + 1
        if next_depth > self.max_depth:
            raise RemoteDelegationDepthError(
                "remote delegation depth exceeded",
                details={"depth": next_depth, "max_depth": self.max_depth},
            )
        if next_count > self.max_delegations:
            raise RemoteDelegationLimitError(
                "remote delegation count exceeded",
                details={
                    "delegation_count": next_count,
                    "max_delegations": self.max_delegations,
                },
            )
        return self.model_copy(
            update={
                "delegation_id": f"delegation_{uuid.uuid4().hex}",
                "depth": next_depth,
                "delegation_count": next_count,
            }
        )


class RemoteInvokeRequest(RemoteModel):
    messages: list[dict[str, Any]] = Field(min_length=1)
    user_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    remote_context: RemoteCallContext


class RemoteResumeEntry(RemoteModel):
    interrupt_id: str = Field(min_length=1)
    status: Literal["resolved", "cancelled"] = "resolved"
    payload: Any | None = None
    metadata: dict[str, Any] | None = None


class RemoteResumeRequest(RemoteModel):
    run_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    remote_context: RemoteCallContext
    resume: list[RemoteResumeEntry] = Field(min_length=1)


class RemoteCancelRequest(RemoteModel):
    run_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)


class RemoteRunResult(RemoteModel):
    run_id: str
    agent_id: str
    user_id: str
    thread_id: str
    config_generation: int
    status: Literal["completed", "interrupted"] = "completed"
    result: dict[str, Any]


class TopologyNode(RemoteModel):
    service_id: str
    agent_id: str
    name: str
    status: str
    config_generation: int | None = None

    @property
    def id(self) -> str:
        return f"{self.service_id}/{self.agent_id}"


class TopologyEdge(RemoteModel):
    source: str
    target: str
    binding_id: str
    enabled: bool = True


class TopologyUnreachable(RemoteModel):
    source: str
    remote_agent_ref: str
    error_code: str
    message: str


class RemoteTopology(RemoteModel):
    nodes: list[TopologyNode] = Field(default_factory=list)
    edges: list[TopologyEdge] = Field(default_factory=list)
    cycles: list[list[str]] = Field(default_factory=list)
    unreachable: list[TopologyUnreachable] = Field(default_factory=list)
    truncated: bool = False
