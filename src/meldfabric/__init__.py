"""Public API for MeldFabric."""

from meldfabric._version import __version__
from meldfabric.app import AgentKit
from meldfabric.identity import IdentityResolver, UserIdentity
from meldfabric.orchestration import GraphState
from meldfabric.registrations import (
    GraphConditionContext,
    HookContext,
    InterventionContext,
    PluginAgent,
    PluginContext,
    hook_callback,
    intervention_callback,
    plugin_hook,
    plugin_tool,
    register,
)
from meldfabric.tools import ToolResult

__all__ = [
    "AgentKit",
    "GraphConditionContext",
    "GraphState",
    "HookContext",
    "IdentityResolver",
    "InterventionContext",
    "PluginAgent",
    "PluginContext",
    "ToolResult",
    "UserIdentity",
    "__version__",
    "hook_callback",
    "intervention_callback",
    "plugin_hook",
    "plugin_tool",
    "register",
]
