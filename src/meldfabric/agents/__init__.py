"""Public Strands Agent runtime exports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meldfabric.agents.native_compiler import (
        AgentBlueprint,
        NativeStrandsCompiler,
        StrandsCompiledExecutable,
    )
    from meldfabric.agents.runtime import AgentRuntimeManager
    from meldfabric.agents.types import NativeExecutionResult

__all__ = [
    "AgentBlueprint",
    "AgentRuntimeManager",
    "NativeExecutionResult",
    "NativeStrandsCompiler",
    "StrandsCompiledExecutable",
]


def __getattr__(name: str) -> Any:
    if name == "AgentRuntimeManager":
        from meldfabric.agents.runtime import AgentRuntimeManager

        return AgentRuntimeManager
    if name == "NativeExecutionResult":
        from meldfabric.agents.types import NativeExecutionResult

        return NativeExecutionResult
    if name in {
        "AgentBlueprint",
        "NativeStrandsCompiler",
        "StrandsCompiledExecutable",
    }:
        from meldfabric.agents.native_compiler import (
            AgentBlueprint,
            NativeStrandsCompiler,
            StrandsCompiledExecutable,
        )

        return {
            "AgentBlueprint": AgentBlueprint,
            "NativeStrandsCompiler": NativeStrandsCompiler,
            "StrandsCompiledExecutable": StrandsCompiledExecutable,
        }[name]
    raise AttributeError(name)
