"""Unified trusted code registration API with cycle-safe lazy exports."""

from __future__ import annotations

from typing import Any

from meldfabric.registrations.catalog import (
    CodeRegistrationCatalog,
    MemberDefinition,
    RegistrationDefinition,
    RegistrationKind,
    catalog,
)
from meldfabric.registrations.decorators import (
    callback_accepts_context,
    hook_callback,
    intervention_callback,
    plugin_hook,
    plugin_tool,
    register,
)
from meldfabric.registrations.types import (
    EmptyConfig,
    GraphConditionContext,
    HookContext,
    InterventionContext,
    PluginAgent,
    PluginContext,
    RuntimeCodeContext,
)

_RUNTIME_EXPORTS = {"RegistrationRuntime", "extension_close_callback"}


def __getattr__(name: str) -> Any:
    if name in _RUNTIME_EXPORTS:
        from meldfabric.registrations import runtime

        return getattr(runtime, name)
    raise AttributeError(name)


__all__ = [
    "CodeRegistrationCatalog",
    "EmptyConfig",
    "GraphConditionContext",
    "HookContext",
    "InterventionContext",
    "MemberDefinition",
    "PluginAgent",
    "PluginContext",
    "RegistrationDefinition",
    "RegistrationKind",
    "RegistrationRuntime",
    "RuntimeCodeContext",
    "callback_accepts_context",
    "catalog",
    "extension_close_callback",
    "hook_callback",
    "intervention_callback",
    "plugin_hook",
    "plugin_tool",
    "register",
]
