"""Default configuration factory."""

from __future__ import annotations

import uuid

from meldfabric.config.models import (
    DatabaseConnection,
    ResourcesConfig,
    WorkspaceConfig,
    WorkspaceInfo,
)
from meldfabric.domain import DatabaseEngine


def create_default_config() -> WorkspaceConfig:
    """Create a minimal, management-ready workspace with no inference model."""

    return WorkspaceConfig(
        workspace=WorkspaceInfo(
            id=f"ws_{uuid.uuid4().hex}",
            name="MeldFabric",
        ),
        resources=ResourcesConfig(
            databases=[
                DatabaseConnection(
                    id="db_default",
                    name="default-agent-sqlite",
                    display_name="默认 Agent SQLite",
                    engine=DatabaseEngine.SQLITE,
                    url="sqlite+aiosqlite:///resources/default.db",
                )
            ]
        ),
    )
