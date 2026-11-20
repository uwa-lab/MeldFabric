from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from meldfabric import AgentKit
from meldfabric.config.backup import ControlBackupService
from meldfabric.config.models import ModelProvider
from meldfabric.config.schema import ControlBackupManifest, ControlSchemaMigration
from meldfabric.errors import ConfigurationError
from meldfabric.security.crypto import SecretDecryptionError


def test_control_schema_records_packaged_module_migration(tmp_path: Path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="known-admin")

    with Session(kit.repository.engine) as session:
        migration = session.get(ControlSchemaMigration, ("core", 1))

    assert migration is not None
    assert migration.name == "initial-control-schema"
    assert len(migration.checksum) == 64


@pytest.mark.asyncio
async def test_full_control_backup_is_verified_and_bootable(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source = AgentKit(state_dir=source_dir, admin_key="known-admin")
    await source.apatch_config({"workspace": {"name": "Backed up"}})
    source.repository.create_snapshot(label="release")
    with TestClient(source.create_app()) as client:
        assert client.get("/health/live").status_code == 200
    target_url = f"sqlite:///{target_dir / 'control.db'}"

    result = ControlBackupService(source.repository.engine, source.repository.database).backup(
        target_url
    )

    assert result.active_generation == source.runtime_config.config_generation
    assert result.latest_generation == source.config.config_generation
    assert result.last_snapshot_version == 1
    assert result.table_counts["meldfabric_control_config_generations"] == 2
    assert result.table_counts["meldfabric_runtime_instances"] == 0
    assert result.table_counts["meldfabric_thread_leases"] == 0
    assert len(result.content_hash) == 64

    restored = AgentKit(target_url, state_dir=target_dir)
    assert restored.config.workspace.name == "Backed up"
    assert restored.runtime_config.workspace.name == "MeldFabric"
    assert await restored.verify_admin_key("known-admin")
    assert restored.repository.list_snapshots()[0].label == "release"
    with Session(restored.repository.engine) as session:
        manifest = session.scalar(select(ControlBackupManifest))
    assert manifest is not None
    assert manifest.status == "complete"


def test_backup_requires_explicit_overwrite_and_rejects_other_workspace(
    tmp_path: Path,
) -> None:
    source = AgentKit(state_dir=tmp_path / "source", admin_key="source-admin")
    service = ControlBackupService(source.repository.engine, source.repository.database)
    target_url = f"sqlite:///{tmp_path / 'backup.db'}"
    service.backup(target_url)

    with pytest.raises(ConfigurationError, match="explicit overwrite"):
        service.backup(target_url)
    service.backup(target_url, overwrite=True)

    other_url = f"sqlite:///{tmp_path / 'other.db'}"
    AgentKit(other_url, state_dir=tmp_path / "other-state", admin_key="other-admin")
    with pytest.raises(ConfigurationError, match=r"another.*workspace"):
        service.backup(other_url, overwrite=True)


@pytest.mark.asyncio
async def test_backup_restores_encrypted_credentials_only_with_original_key_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MELDFABRIC_MASTER_KEY", raising=False)
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source = AgentKit(state_dir=source_dir, admin_key="known-admin")
    provider = ModelProvider(name="provider", base_url="https://example.test/v1")
    await source.apatch_config({"resources": {"model_providers": [provider.model_dump()]}})
    with TestClient(source.create_app()) as client:
        saved = client.put(
            f"/admin/resources/model-providers/{provider.id}/secret",
            headers={"X-Admin-Key": "known-admin"},
            json={
                "value": "encrypted-provider-secret",
                "expected_catalog_digest": source.catalog_digest,
            },
        )
        assert saved.status_code == 200
    target_url = f"sqlite:///{target_dir / 'control.db'}"
    ControlBackupService(source.repository.engine, source.repository.database).backup(target_url)

    wrong_key = AgentKit(target_url, state_dir=target_dir)
    with pytest.raises(SecretDecryptionError), TestClient(wrong_key.create_app()):
        pass

    workspace_id = source.config.workspace.id
    target_key = target_dir / "keys" / f"{workspace_id}.key"
    shutil.copy2(source_dir / "keys" / f"{workspace_id}.key", target_key)
    restored = AgentKit(target_url, state_dir=target_dir)
    with TestClient(restored.create_app()) as client:
        secret_ref = restored.config.resources.model_providers[0].api_key_secret_ref
        assert secret_ref is not None
        assert (
            client.portal.call(restored.storage.secrets.get_secret, secret_ref)
            == "encrypted-provider-secret"
        )


def test_backup_api_requires_step_up_admin_key_and_does_not_add_a_resource(
    tmp_path: Path,
) -> None:
    kit = AgentKit(state_dir=tmp_path / "source", admin_key="known-admin")
    before = [item.id for item in kit.config.resources.databases]
    target_url = f"sqlite:///{tmp_path / 'transient-target.db'}"

    with TestClient(kit.create_app()) as client:
        denied = client.post(
            "/admin/system/backups",
            headers={"X-Admin-Key": "known-admin"},
            json={
                "engine": "sqlite",
                "url": target_url,
                "admin_key": "wrong-admin",
            },
        )
        created = client.post(
            "/admin/system/backups",
            headers={"X-Admin-Key": "known-admin"},
            json={
                "engine": "sqlite",
                "url": target_url,
                "admin_key": "known-admin",
            },
        )

    assert denied.status_code == 401
    assert created.status_code == 200
    assert [item.id for item in kit.config.resources.databases] == before
    assert target_url.encode() not in (tmp_path / "source" / "control.db").read_bytes()
