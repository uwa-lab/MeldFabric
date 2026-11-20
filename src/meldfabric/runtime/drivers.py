"""Embedded FastAPI deployment driver for the Strands runtime."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from meldfabric.errors import ConfigurationError, OptionalDependencyError
from meldfabric.identity import ClaimedIdentityResolver

if TYPE_CHECKING:
    from meldfabric.app import AgentKit


BOOTSTRAP_CONTROL_DATABASE_ENV = "MELDFABRIC_BOOTSTRAP_CONTROL_DATABASE"
BOOTSTRAP_STATE_DIR_ENV = "MELDFABRIC_BOOTSTRAP_STATE_DIR"
BOOTSTRAP_ADMIN_KEY_ENV_ENV = "MELDFABRIC_BOOTSTRAP_ADMIN_KEY_ENV"
BOOTSTRAP_IDENTITY_RESOLVER_ENV = "MELDFABRIC_BOOTSTRAP_IDENTITY_RESOLVER"
BOOTSTRAP_REGISTRATION_MODULES_ENV = "MELDFABRIC_BOOTSTRAP_REGISTRATION_MODULES"


@runtime_checkable
class RuntimeDriver(Protocol):
    name: str
    capabilities: frozenset[str]

    def create_app(self) -> Any: ...

    def run(self, *, host: str | None = None, port: int | None = None) -> None: ...


class EmbeddedFastAPIDriver:
    """Run the complete Strands control and data plane in FastAPI."""

    name = "embedded_fastapi"
    capabilities = frozenset(
        {
            "a2a",
            "ag_ui",
            "meldfabric_remote",
            "explicit_config_apply",
            "management_api",
            "multi_worker",
            "per_agent_persistence",
            "strands_plugins",
        }
    )

    def __init__(self, agent_kit: AgentKit) -> None:
        self.agent_kit = agent_kit

    def create_app(self) -> Any:
        return self.agent_kit.create_app()

    def run(self, *, host: str | None = None, port: int | None = None) -> None:
        try:
            import uvicorn
        except ImportError as exc:
            raise OptionalDependencyError(
                "Core dependencies are missing; reinstall meldfabric with dependencies"
            ) from exc

        runtime = self.agent_kit.runtime_config.runtime
        selected_host = host or runtime.host
        selected_port = port or runtime.port
        if runtime.workers == 1:
            uvicorn.run(self.create_app(), host=selected_host, port=selected_port)
            return

        os.environ.update(bootstrap_environment(self.agent_kit))
        uvicorn.run(
            "meldfabric.runtime.bootstrap:create_embedded_app",
            factory=True,
            host=selected_host,
            port=selected_port,
            workers=runtime.workers,
        )


def bootstrap_environment(agent_kit: AgentKit) -> dict[str, str]:
    """Return non-secret coordinates needed to reconstruct one worker."""

    environment = {
        BOOTSTRAP_CONTROL_DATABASE_ENV: agent_kit.repository.database.sync_url,
        BOOTSTRAP_STATE_DIR_ENV: str(agent_kit.repository.state_dir),
        BOOTSTRAP_ADMIN_KEY_ENV_ENV: agent_kit.repository.admin_key_env,
    }
    if agent_kit.identity_resolver_path:
        environment[BOOTSTRAP_IDENTITY_RESOLVER_ENV] = agent_kit.identity_resolver_path
    elif not isinstance(agent_kit.identity_resolver, ClaimedIdentityResolver):
        raise ConfigurationError(
            "multi-process deployments require custom identity resolvers to be configured "
            "with identity_resolver_path"
        )
    modules = agent_kit.registrations.modules()
    unavailable = [module for module in modules if module in {"__main__", "__mp_main__"}]
    if unavailable:
        raise ConfigurationError(
            "multi-process deployments require decorated capabilities to live in importable "
            "Python modules rather than __main__",
            details={"modules": unavailable},
        )
    environment[BOOTSTRAP_REGISTRATION_MODULES_ENV] = json.dumps(list(modules))
    return environment


def driver_for(agent_kit: AgentKit, name: str | None = None) -> RuntimeDriver:
    selected = name or agent_kit.runtime_config.runtime.driver
    drivers: Mapping[str, type[EmbeddedFastAPIDriver]] = {
        EmbeddedFastAPIDriver.name: EmbeddedFastAPIDriver,
    }
    try:
        return drivers[selected](agent_kit)
    except KeyError as exc:
        raise ConfigurationError(
            f"unknown runtime driver {selected!r}", details={"driver": selected}
        ) from exc
