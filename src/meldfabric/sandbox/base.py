"""Provider-neutral sandbox execution contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from meldfabric.config.models import SandboxProfile


@dataclass(frozen=True, slots=True)
class SandboxContext:
    """Isolation identity used to reuse a sandbox for one logical conversation."""

    workspace_id: str
    agent_id: str
    user_id: str
    thread_id: str
    config_generation: int


@dataclass(frozen=True, slots=True)
class SandboxToolRequest:
    context: SandboxContext
    profile: SandboxProfile
    python_path: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SandboxStatus:
    provider: str
    available: bool
    detail: str | None = None


@runtime_checkable
class SandboxProvider(Protocol):
    """A provider executes code without ever falling back to the host process."""

    name: ClassVar[str]
    capabilities: ClassVar[frozenset[str]]
    config_schema: ClassVar[dict[str, Any]]

    async def test(self, profile: SandboxProfile) -> SandboxStatus: ...

    async def execute_tool(self, request: SandboxToolRequest) -> Any: ...

    async def execute_command(
        self,
        *,
        context: SandboxContext,
        profile: SandboxProfile,
        command: str,
        timeout: int | None = None,
    ) -> tuple[str, int | None, bool]: ...

    async def upload_files(
        self,
        *,
        context: SandboxContext,
        profile: SandboxProfile,
        files: list[tuple[str, bytes]],
    ) -> list[tuple[str, str | None]]: ...

    async def download_files(
        self,
        *,
        context: SandboxContext,
        profile: SandboxProfile,
        paths: list[str],
    ) -> list[tuple[str, bytes | None, str | None]]: ...

    async def close(self) -> None: ...
