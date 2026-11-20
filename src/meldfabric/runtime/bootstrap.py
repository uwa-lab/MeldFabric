"""Import-string compatible application factories for multi-process servers."""

from __future__ import annotations

import importlib
import json
import os

from meldfabric.app import AgentKit
from meldfabric.errors import ConfigurationError
from meldfabric.runtime.drivers import (
    BOOTSTRAP_ADMIN_KEY_ENV_ENV,
    BOOTSTRAP_CONTROL_DATABASE_ENV,
    BOOTSTRAP_IDENTITY_RESOLVER_ENV,
    BOOTSTRAP_REGISTRATION_MODULES_ENV,
    BOOTSTRAP_STATE_DIR_ENV,
)


def agent_kit_from_environment() -> AgentKit:
    database = os.getenv(BOOTSTRAP_CONTROL_DATABASE_ENV)
    if not database:
        raise ConfigurationError(
            f"{BOOTSTRAP_CONTROL_DATABASE_ENV} must identify the MeldFabric control database"
        )
    raw_modules = os.getenv(BOOTSTRAP_REGISTRATION_MODULES_ENV, "[]")
    try:
        modules = json.loads(raw_modules)
        if not isinstance(modules, list) or not all(isinstance(item, str) for item in modules):
            raise TypeError("registration module manifest must be a list of strings")
        for module in modules:
            importlib.import_module(module)
    except Exception as exc:
        raise ConfigurationError(
            "failed to import decorated capability modules in the worker",
            details={"reason": str(exc)},
        ) from exc
    return AgentKit(
        database,
        state_dir=os.getenv(BOOTSTRAP_STATE_DIR_ENV, ".meldfabric"),
        admin_key_env=os.getenv(BOOTSTRAP_ADMIN_KEY_ENV_ENV, "MELDFABRIC_ADMIN_KEY"),
        identity_resolver_path=os.getenv(BOOTSTRAP_IDENTITY_RESOLVER_ENV),
    )


def create_embedded_app():
    return agent_kit_from_environment().create_app()
