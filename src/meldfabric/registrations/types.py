"""Stable public context and facade types for decorated code capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from meldfabric.identity import UserIdentity

ConfigT = TypeVar("ConfigT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class RuntimeCodeContext(Generic[ConfigT]):
    """Read-only invocation identity supplied to trusted registered code."""

    config: ConfigT
    workspace_id: str
    agent_id: str
    resource_id: str
    resource_name: str
    user_id: str
    thread_id: str
    identity: UserIdentity
    endpoint_id: str | None = None
    run_id: str | None = None
    trace_attributes: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class PluginContext(RuntimeCodeContext[ConfigT]):
    """Materialization context for one configured plugin instance."""


@dataclass(frozen=True, slots=True)
class HookContext(RuntimeCodeContext[ConfigT]):
    """Materialization context for one configured hook instance."""


@dataclass(frozen=True, slots=True)
class InterventionContext(RuntimeCodeContext[ConfigT]):
    """Materialization context for one configured intervention instance."""


@dataclass(frozen=True, slots=True)
class GraphConditionContext(Generic[ConfigT]):
    """Static Graph edge context bound when an orchestration is compiled."""

    config: ConfigT
    workspace_id: str
    orchestration_id: str
    edge_id: str
    source_node_id: str
    target_node_id: str


class PluginAgent:
    """Restricted Agent view available during custom plugin initialization.

    Tools and hooks cannot be registered through this facade. They must be declared with
    :func:`plugin_tool` and :func:`plugin_hook` so the control plane can inspect them.
    """

    __slots__ = ("__agent",)

    def __init__(self, agent: Any) -> None:
        self.__agent = agent

    @property
    def id(self) -> str:
        return str(getattr(self.__agent, "agent_id", ""))

    @property
    def name(self) -> str:
        return str(getattr(self.__agent, "name", ""))

    @property
    def system_prompt(self) -> Any:
        return self.__agent.system_prompt

    @system_prompt.setter
    def system_prompt(self, value: Any) -> None:
        self.__agent.system_prompt = value

    @property
    def state(self) -> Any:
        return self.__agent.state

    @property
    def tool_names(self) -> tuple[str, ...]:
        names = getattr(self.__agent, "tool_names", ())
        return tuple(str(item) for item in names)

    @property
    def model_name(self) -> str:
        model = getattr(self.__agent, "model", None)
        return type(model).__name__ if model is not None else ""


class EmptyConfig(BaseModel):
    """Validated empty configuration used when a registration declares no model."""

    model_config = {"extra": "forbid"}
