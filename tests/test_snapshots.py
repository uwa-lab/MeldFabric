from __future__ import annotations

from pathlib import Path

import pytest

from meldfabric import AgentKit
from meldfabric.config.defaults import create_default_config
from meldfabric.runtime import RuntimeSnapshot, SnapshotRegistry


def test_in_flight_run_keeps_old_snapshot() -> None:
    first_config = create_default_config()
    registry = SnapshotRegistry()
    registry.publish(RuntimeSnapshot.compile(first_config))

    with registry.pin_current() as pinned:
        second_config = first_config.model_copy(deep=True)
        second_config.config_generation = 2
        second_config.workspace.name = "New"
        registry.publish(RuntimeSnapshot.compile(second_config))

        assert pinned.revision == 1
        assert pinned.config.workspace.name == "MeldFabric"
        assert registry.current.revision == 2
        assert registry.active_revisions() == (1, 2)

    assert registry.active_revisions() == (2,)


@pytest.mark.asyncio
async def test_explicit_apply_keeps_revision_scoped_storage_until_run_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MELDFABRIC_MASTER_KEY", "snapshot-resource-master-key")
    kit = AgentKit(state_dir=tmp_path)
    await kit.storage.initialize(kit.config)
    await kit.persistence.initialize(kit.config)
    original_revision = kit.config.config_generation

    with kit.snapshots.pin_current():
        await kit.apatch_config({"workspace": {"name": "hot-updated"}})
        current_revision = kit.config.config_generation
        assert kit.snapshots.current.revision == original_revision
        assert kit.storage.status()["active_revisions"] == [original_revision]
        assert kit.persistence.status()["active_revisions"] == [original_revision]

        await kit.aapply_latest_config(expected_generation=current_revision)
        assert kit.storage.status()["active_revisions"] == [
            original_revision,
            current_revision,
        ]
        assert kit.persistence.status()["active_revisions"] == [
            original_revision,
            current_revision,
        ]

    await kit.agent_runtime.aprune()
    assert kit.storage.status()["active_revisions"] == [current_revision]
    assert kit.persistence.status()["active_revisions"] == [current_revision]
    await kit.persistence.close()
    await kit.storage.close()
