from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import delete, inspect, select
from sqlalchemy.orm import Session

from meldfabric import AgentKit
from meldfabric.config import ConfigRepository
from meldfabric.config.models import ModelProvider
from meldfabric.config.schema import (
    ControlAdminCredential,
    ControlConfigGeneration,
    ControlGenerationSecretRef,
    ControlSecretVersion,
)
from meldfabric.errors import (
    ConfigurationConflictError,
    ConfigurationError,
    ControlDatabaseCorruptError,
)
from meldfabric.storage.schema import SecretRecord


@pytest.mark.asyncio
async def test_default_control_database_is_created_and_admin_key_is_hashed(
    tmp_path: Path,
) -> None:
    kit = AgentKit(state_dir=tmp_path)
    control_path = tmp_path / "control.db"

    assert control_path.exists()
    assert kit.admin_key is not None
    assert kit.admin_key.startswith("meldfabric_admin_")
    assert await kit.verify_admin_key(kit.admin_key)
    assert kit.admin_key.encode() not in control_path.read_bytes()
    assert [item.id for item in kit.config.resources.databases] == ["db_default"]
    assert kit.config.resources.databases[0].url == ("sqlite+aiosqlite:///resources/default.db")

    with Session(kit.repository.engine) as session:
        credential = session.scalar(select(ControlAdminCredential))
        assert credential is not None
        assert credential.key_hash.startswith("scrypt-v1:")
        assert credential.key_prefix == kit.admin_key[:24]


@pytest.mark.asyncio
async def test_first_start_admin_precedence_and_existing_database_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUSTOM_ADMIN_KEY", "from-environment")
    first = AgentKit(
        state_dir=tmp_path,
        admin_key="from-constructor",
        admin_key_env="CUSTOM_ADMIN_KEY",
    )

    assert first.admin_key == "from-constructor"
    assert await first.verify_admin_key("from-constructor")
    assert not await first.verify_admin_key("from-environment")

    restarted = AgentKit(
        state_dir=tmp_path,
        admin_key="ignored-on-restart",
        admin_key_env="CUSTOM_ADMIN_KEY",
    )
    assert restarted.admin_key is None
    assert await restarted.verify_admin_key("from-constructor")
    assert not await restarted.verify_admin_key("ignored-on-restart")


@pytest.mark.asyncio
async def test_first_start_uses_environment_when_constructor_key_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUSTOM_ADMIN_KEY", "from-environment")

    kit = AgentKit(state_dir=tmp_path, admin_key_env="CUSTOM_ADMIN_KEY")

    assert kit.admin_key == "from-environment"
    assert await kit.verify_admin_key("from-environment")


def test_existing_workspace_without_admin_record_is_corrupt(tmp_path: Path) -> None:
    repository = ConfigRepository(state_dir=tmp_path, admin_key="known-admin")
    repository.initialize()
    with repository.engine.begin() as connection:
        connection.execute(delete(ControlAdminCredential))
    repository.dispose()

    with pytest.raises(ControlDatabaseCorruptError, match="credential is missing"):
        AgentKit(state_dir=tmp_path)


@pytest.mark.asyncio
async def test_patch_uses_latest_generation_and_preserves_history(tmp_path: Path) -> None:
    kit = AgentKit(state_dir=tmp_path)
    original_generation = kit.config.config_generation

    loaded = await kit.apatch_config(
        {"workspace": {"name": "Updated"}},
        expected_generation=original_generation,
    )

    assert loaded.config.workspace.name == "Updated"
    assert loaded.config.config_generation == original_generation + 1
    assert kit.snapshots.current.revision == original_generation
    assert kit.runtime_config.workspace.name == "MeldFabric"
    assert kit.repository.load().config.config_generation == original_generation + 1
    assert kit.repository.load_active().config.config_generation == original_generation
    state = kit.repository.generation_state()
    assert state.latest_generation == original_generation + 1
    assert state.active_generation == original_generation

    await kit.aapply_latest_config(expected_generation=loaded.config.config_generation)

    assert kit.snapshots.current.revision == original_generation + 1
    assert kit.runtime_config.workspace.name == "Updated"
    assert kit.repository.generation_state().active_generation == original_generation + 1
    historical = kit.repository.load_generation(kit.config.workspace.id, original_generation)
    assert historical.workspace.name == "MeldFabric"

    with pytest.raises(ConfigurationConflictError):
        await kit.apatch_config(
            {"workspace": {"name": "Stale"}},
            expected_generation=original_generation,
        )


@pytest.mark.asyncio
async def test_restart_uses_active_generation_while_management_keeps_latest(
    tmp_path: Path,
) -> None:
    first = AgentKit(state_dir=tmp_path)
    await first.apatch_config({"workspace": {"name": "Saved but not applied"}})

    restarted = AgentKit(state_dir=tmp_path)

    assert restarted.config.workspace.name == "Saved but not applied"
    assert restarted.config.config_generation == 2
    assert restarted.runtime_config.workspace.name == "MeldFabric"
    assert restarted.runtime_config.config_generation == 1


@pytest.mark.asyncio
async def test_runtime_credentials_remain_pinned_until_generation_is_applied(
    tmp_path: Path,
) -> None:
    kit = AgentKit(state_dir=tmp_path)
    repository = await kit.storage.require_secrets(kit.config)
    await repository.put_secret("secret_provider", kind="model_api_key", value="old-key")
    provider = ModelProvider(
        id="provider_pinned",
        name="pinned",
        base_url="https://example.invalid/v1",
        api_key_secret_ref="secret_provider",
    )
    saved = await kit.apatch_config(
        {"resources": {"model_providers": [provider.model_dump(mode="json")]}}
    )
    await kit.aapply_latest_config(expected_generation=saved.config.config_generation)
    active_revision = saved.config.config_generation

    await repository.put_secret("secret_provider", kind="model_api_key", value="new-key")
    pending = await kit.atouch_config(reason="test-secret-rotation")
    await kit.storage.initialize_revision(pending.config, make_current=False)

    assert await kit.storage.secrets_for(active_revision).get_secret("secret_provider") == "old-key"
    assert (
        await kit.storage.secrets_for(pending.config.config_generation).get_secret(
            "secret_provider"
        )
        == "new-key"
    )
    assert kit.runtime_config.config_generation == active_revision

    await kit.aapply_latest_config(expected_generation=pending.config.config_generation)
    assert kit.runtime_config.config_generation == pending.config.config_generation
    await kit.persistence.close()
    await kit.storage.close()


@pytest.mark.asyncio
async def test_secret_write_and_generation_are_one_transaction(tmp_path: Path) -> None:
    kit = AgentKit(state_dir=tmp_path)
    secrets = await kit.storage.require_secrets(kit.config)
    provider = ModelProvider(
        id="provider_atomic",
        name="atomic",
        base_url="https://example.invalid/v1",
        api_key_secret_ref="secret_atomic",
    )
    write = secrets.prepare_secret_write(
        "secret_atomic",
        kind="model_provider_api_key",
        value="first-value",
    )
    saved = await kit.apatch_config(
        {"resources": {"model_providers": [provider.model_dump(mode="json")]}},
        secret_writes=(write,),
    )

    with Session(kit.repository.engine) as session:
        version = session.get(ControlSecretVersion, ("secret_atomic", 1))
        reference = session.get(
            ControlGenerationSecretRef,
            (kit.config.workspace.id, saved.config.config_generation, "secret_atomic"),
        )
        current = session.get(SecretRecord, "secret_atomic")
        assert version is not None
        assert reference is not None and reference.secret_version == 1
        assert current is not None and current.encrypted_value == version.encrypted_value

    stale_generation = saved.config.config_generation
    await kit.apatch_config({"workspace": {"name": "newer"}})
    rejected = secrets.prepare_secret_write(
        "secret_atomic",
        kind="model_provider_api_key",
        value="must-not-commit",
    )
    with pytest.raises(ConfigurationConflictError):
        await kit.apatch_config(
            {},
            expected_generation=stale_generation,
            secret_writes=(rejected,),
        )
    with Session(kit.repository.engine) as session:
        versions = list(
            session.scalars(
                select(ControlSecretVersion).where(
                    ControlSecretVersion.secret_id == "secret_atomic"
                )
            )
        )
    assert len(versions) == 1
    await kit.storage.close()


@pytest.mark.asyncio
async def test_explicit_snapshots_have_independent_versions_and_restore_as_new_generation(
    tmp_path: Path,
) -> None:
    kit = AgentKit(state_dir=tmp_path)
    first_generation = kit.config.config_generation
    snapshot = kit.repository.create_snapshot(label="baseline", note="before change")

    changed = await kit.apatch_config({"workspace": {"name": "Changed"}})
    restored = kit.repository.restore_snapshot(
        snapshot.version,
        expected_generation=changed.config.config_generation,
    )

    assert snapshot.version == 1
    assert snapshot.source_generation == first_generation
    assert restored.config.workspace.name == "MeldFabric"
    assert restored.config.config_generation == changed.config.config_generation + 1
    assert [item.version for item in kit.repository.list_snapshots()] == [1]
    with Session(kit.repository.engine) as session:
        reasons = list(
            session.scalars(
                select(ControlConfigGeneration.reason).order_by(ControlConfigGeneration.generation)
            )
        )
    assert reasons == ["initialize", "patch", "restore-snapshot:1"]


def test_snapshot_deletion_is_permanent_and_versions_remain_monotonic(tmp_path: Path) -> None:
    kit = AgentKit(state_dir=tmp_path)
    first = kit.repository.create_snapshot(label="first")

    kit.repository.delete_snapshot(first.version)
    second = kit.repository.create_snapshot(label="second")

    assert [item.version for item in kit.repository.list_snapshots()] == [second.version]
    assert second.version == first.version + 1
    with pytest.raises(ConfigurationError, match="does not exist"):
        kit.repository.delete_snapshot(first.version)


def test_control_schema_has_no_redundant_config_sections(tmp_path: Path) -> None:
    kit = AgentKit(state_dir=tmp_path)

    assert (
        "meldfabric_control_config_sections" not in inspect(kit.repository.engine).get_table_names()
    )


@pytest.mark.asyncio
async def test_snapshot_generation_check_is_enforced_inside_repository(tmp_path: Path) -> None:
    kit = AgentKit(state_dir=tmp_path)
    stale = kit.config.config_generation
    await kit.apatch_config({"workspace": {"name": "new generation"}})

    with pytest.raises(ConfigurationConflictError):
        kit.repository.create_snapshot(expected_generation=stale)
    assert kit.repository.list_snapshots() == []


@pytest.mark.asyncio
async def test_workspace_identity_cannot_be_changed_by_raw_patch(tmp_path: Path) -> None:
    kit = AgentKit(state_dir=tmp_path)

    with pytest.raises(ConfigurationError, match="workspace identity is immutable"):
        await kit.apatch_config({"workspace": {"id": "ws_replacement"}})

    assert kit.config.workspace.id != "ws_replacement"
