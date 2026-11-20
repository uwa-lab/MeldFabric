"""Runtime values shared by the Agent compiler, executor, and protocols."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meldfabric.domain.serialization import jsonable

if TYPE_CHECKING:
    from meldfabric.storage import RuntimeLease, SystemRepository


@dataclass(frozen=True, slots=True)
class NativeExecutionResult:
    executable_id: str
    output: str
    messages: list[dict[str, Any]]
    state: dict[str, Any]
    stop_reason: str | None
    interrupts: list[dict[str, Any]]
    structured_output: Any = None
    metrics: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "executable_id": self.executable_id,
            "output": self.output,
            "messages": self.messages,
            "state": self.state,
            "stop_reason": self.stop_reason,
            "interrupts": self.interrupts,
            "structured_output": jsonable(self.structured_output),
            "metrics": jsonable(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class PausedRun:
    run_id: str
    revision: int
    record_id: str | None = None


@dataclass(slots=True)
class ActiveRun:
    run_id: str
    endpoint_id: str
    user_id: str
    thread_id: str
    cancel_requested: asyncio.Event
    record_id: str | None = None
    system_repository: SystemRepository | None = None
    lease: RuntimeLease | None = None
    abort_error: Exception | None = None


__all__ = ["ActiveRun", "NativeExecutionResult", "PausedRun"]
