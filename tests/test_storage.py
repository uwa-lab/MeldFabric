from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from meldfabric.config.database import ControlDatabaseSpec
from meldfabric.config.schema import ControlSecretVersion
from meldfabric.errors import ConfigurationError, RunAlreadyActiveError, ThreadBusyError
from meldfabric.security import SecretCipher
from meldfabric.storage import (
    AsyncDatabase,
    PlaygroundRepository,
    SecretRepository,
    SystemRepository,
)
from meldfabric.storage.migrations import SYSTEM_SCHEMA_VERSION
from meldfabric.storage.schema import AgentRunRecord, SchemaMigrationRecord, SecretRecord


def create_database(tmp_path: Path) -> AsyncDatabase:
    spec = ControlDatabaseSpec.resolve(None, state_dir=tmp_path)
    return AsyncDatabase(spec)


@pytest.mark.asyncio
async def test_system_schema_migrations_are_versioned_and_verified(tmp_path: Path) -> None:
    database = create_database(tmp_path)
    repository = SystemRepository(database)
    await repository.initialize()
    assert repository.schema_version == SYSTEM_SCHEMA_VERSION

    await repository.initialize()
    assert repository.schema_version == SYSTEM_SCHEMA_VERSION

    async with database.session() as session, session.begin():
        await session.execute(
            update(SchemaMigrationRecord)
            .where(SchemaMigrationRecord.version == SYSTEM_SCHEMA_VERSION)
            .values(checksum="tampered")
        )
    with pytest.raises(ConfigurationError, match="does not match packaged history"):
        await repository.initialize()
    await database.close()


@pytest.mark.asyncio
async def test_playground_history_is_independent_and_hard_deleted(tmp_path: Path) -> None:
    database = create_database(tmp_path)
    system = SystemRepository(database)
    await system.initialize()
    repository = PlaygroundRepository(database)

    conversation = await repository.create_conversation(
        workspace_id="workspace-one",
        endpoint_id="endpoint-one",
        endpoint_name="Assistant",
        user_id="playground:workspace-one",
        generation=7,
    )
    assert await repository.append_messages(
        workspace_id="workspace-one",
        conversation_id=conversation.id,
        messages=[
            {
                "id": "message-user",
                "run_id": "run-one",
                "kind": "message",
                "role": "user",
                "content": "Hello from the console",
            },
            {
                "id": "message-assistant",
                "run_id": "run-one",
                "kind": "message",
                "role": "assistant",
                "content": "Hello",
            },
        ],
    )

    loaded = await repository.get_conversation("workspace-one", conversation.id)
    messages = await repository.list_messages("workspace-one", conversation.id)
    assert loaded is not None
    assert loaded.title == "Hello from the console"
    assert loaded.message_count == 2
    assert [item.id for item in messages] == ["message-user", "message-assistant"]

    assert await repository.delete_conversation("workspace-one", conversation.id) is True
    assert await repository.get_conversation("workspace-one", conversation.id) is None
    assert await repository.list_messages("workspace-one", conversation.id) == []
    await database.close()


@pytest.mark.asyncio
async def test_system_schema_migration_is_serialized_across_workers(tmp_path: Path) -> None:
    first_database = create_database(tmp_path)
    second_database = create_database(tmp_path)
    first = SystemRepository(first_database)
    second = SystemRepository(second_database)

    await asyncio.gather(first.initialize(), second.initialize())

    assert first.schema_version == SYSTEM_SCHEMA_VERSION
    assert second.schema_version == SYSTEM_SCHEMA_VERSION
    await first_database.close()
    await second_database.close()


@pytest.mark.asyncio
async def test_runtime_suspension_cannot_be_overwritten_by_an_inflight_heartbeat(
    tmp_path: Path,
) -> None:
    database = create_database(tmp_path)
    repository = SystemRepository(database)
    await repository.initialize()
    await repository.register_runtime_instance(
        instance_id="instance-race",
        workspace_id="workspace-race",
        hostname="host-a",
        process_id=101,
        config_generation=1,
        catalog_digest="a" * 64,
    )

    await repository.suspend_runtime_instances("workspace-race")
    await repository.heartbeat_runtime_instance(
        instance_id="instance-race",
        config_generation=1,
        catalog_digest="a" * 64,
        status="ready",
    )

    assert await repository.runtime_instance_is_suspended("instance-race") is True
    await database.close()


@pytest.mark.asyncio
async def test_secret_repository_encrypts_and_recovers_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MELDFABRIC_MASTER_KEY", "a-high-entropy-test-master-key")
    database = create_database(tmp_path)
    repository = SecretRepository(
        database,
        SecretCipher.from_environment("MELDFABRIC_MASTER_KEY"),
    )
    await repository.initialize()

    await repository.put_secret("secret_model", kind="model_api_key", value="secret-value")

    assert await repository.get_secret("secret_model") == "secret-value"
    assert await repository.delete_secret("secret_model") is True
    assert await repository.get_secret("secret_model") is None
    await database.close()


@pytest.mark.asyncio
async def test_secret_updates_append_immutable_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MELDFABRIC_MASTER_KEY", "versioned-secret-test-key")
    database = create_database(tmp_path)
    repository = SecretRepository(
        database,
        SecretCipher.from_environment("MELDFABRIC_MASTER_KEY"),
    )
    await repository.initialize()

    await repository.put_secret("secret_model", kind="model_api_key", value="first")
    await repository.put_secret("secret_model", kind="model_api_key", value="second")

    async with database.session() as session:
        versions = await session.scalar(
            select(func.count())
            .select_from(ControlSecretVersion)
            .where(ControlSecretVersion.secret_id == "secret_model")
        )
    assert versions == 2
    assert await repository.get_secret("secret_model") == "second"
    await database.close()


@pytest.mark.asyncio
async def test_inbound_key_is_only_returned_once_and_can_be_revoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MELDFABRIC_MASTER_KEY", "another-high-entropy-test-key")
    database = create_database(tmp_path)
    repository = SecretRepository(
        database,
        SecretCipher.from_environment("MELDFABRIC_MASTER_KEY"),
    )
    await repository.initialize()

    created = await repository.create_inbound_key(
        kind="remote_access",
        agent_id="agent_main",
        scopes=["discover", "invoke", "topology:read"],
    )

    verified = await repository.verify_key(created.value, required_scope="invoke")
    assert verified is not None
    assert verified.id == created.id
    assert not hasattr(verified, "key_hash")
    assert await repository.verify_key(created.value, required_scope="unknown") is None

    async with database.session() as session:
        stored = await session.get(SecretRecord, created.id)
        assert stored is not None
        assert stored.last_used_at is None
    assert await repository.flush_key_usage() == 1
    async with database.session() as session:
        stored = await session.get(SecretRecord, created.id)
        assert stored is not None
        assert stored.last_used_at is not None

    assert await repository.set_key_status(created.id, "disabled") is True
    assert await repository.verify_key(created.value) is None
    assert await repository.set_key_status(created.id, "active") is True
    assert await repository.verify_key(created.value) is not None

    assert await repository.revoke_key(created.id) is True
    assert await repository.verify_key(created.value) is None
    assert await repository.set_key_status(created.id, "active") is False
    await database.close()


@pytest.mark.asyncio
async def test_verified_inbound_key_uses_memory_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MELDFABRIC_MASTER_KEY", "cache-test-key")
    database = create_database(tmp_path)
    repository = SecretRepository(
        database,
        SecretCipher.from_environment("MELDFABRIC_MASTER_KEY"),
    )
    await repository.initialize()
    created = await repository.create_inbound_key(
        kind="agent_api",
        agent_id="agent_main",
        scopes=["invoke"],
    )

    assert await repository.verify_key(created.value, required_scope="invoke") is not None
    monkeypatch.setattr(
        "meldfabric.storage.secrets.verify_inbound_key",
        lambda *_args: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    assert await repository.verify_key(created.value, required_scope="invoke") is not None
    await database.close()


@pytest.mark.asyncio
async def test_system_repository_persists_remote_mapping_and_residual(
    tmp_path: Path,
) -> None:
    database = create_database(tmp_path)
    repository = SystemRepository(database)
    await repository.initialize()

    saved = await repository.put_remote_mapping(
        mapping_key="agent_a:thread_a:remote_b",
        local_agent_id="agent_a",
        remote_agent_id="remote_b",
        remote_thread_id="thread_remote",
        policy="parent_thread",
        parent_thread_id="thread_a",
    )
    loaded = await repository.get_remote_mapping("agent_a:thread_a:remote_b")

    assert loaded == saved

    residual_id = await repository.record_deleted_agent(
        agent_id="agent_deleted",
        database_refs=["db_default"],
        namespaces={"checkpoint": "ws.agent_deleted.checkpoint"},
        cleanup_types=["checkpoint", "memory"],
    )
    residuals = await repository.list_residuals()
    assert residuals[0].id == residual_id
    assert residuals[0].agent_id == "agent_deleted"
    await database.close()


@pytest.mark.asyncio
async def test_remote_mapping_concurrent_insert_returns_one_winner(tmp_path: Path) -> None:
    database = create_database(tmp_path)
    repository = SystemRepository(database)
    await repository.initialize()

    async def create(candidate: str):
        return await repository.put_remote_mapping(
            mapping_key="shared-user-mapping",
            local_agent_id="agent_a",
            remote_agent_id="remote_b",
            remote_thread_id=candidate,
            policy="per_user",
            user_id="user_a",
        )

    first, second = await asyncio.gather(create("remote_one"), create("remote_two"))

    assert first.remote_thread_id == second.remote_thread_id
    stored = await repository.get_remote_mapping("shared-user-mapping")
    assert stored is not None
    assert stored.remote_thread_id == first.remote_thread_id
    await database.close()


@pytest.mark.asyncio
async def test_system_repository_coordinates_runs_across_workers(tmp_path: Path) -> None:
    database = create_database(tmp_path)
    repository = SystemRepository(database)
    await repository.initialize()

    run = await repository.start_agent_run(
        workspace_id="ws_test",
        agent_id="agent_main",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="internal-thread-1",
        run_id="run-1",
        config_generation=7,
    )
    assert run is not None
    duplicate = await repository.start_agent_run(
        workspace_id="ws_test",
        agent_id="agent_main",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="internal-thread-1",
        run_id="run-1",
        config_generation=7,
    )
    assert duplicate is None

    await repository.finish_agent_run(
        run.id,
        status="interrupted",
        interrupt_ids=["interrupt-1"],
    )
    paused = await repository.get_paused_agent_run(
        workspace_id="ws_test",
        agent_id="agent_main",
        user_id="user-1",
        external_thread_id="thread-1",
    )
    assert paused is not None
    assert paused.config_generation == 7
    assert paused.interrupt_ids == ("interrupt-1",)

    second = await repository.start_agent_run(
        workspace_id="ws_test",
        agent_id="agent_main",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="internal-thread-1",
        run_id="run-2",
        config_generation=7,
        parent_run_id="run-1",
    )
    assert second is not None
    assert (
        await repository.request_run_cancellation(
            workspace_id="ws_test",
            agent_id="agent_main",
            run_id="run-2",
            external_thread_id="thread-1",
        )
        == 1
    )
    assert await repository.is_run_cancellation_requested(second.id) is True
    await database.close()


@pytest.mark.asyncio
async def test_thread_lease_rejects_later_run_across_database_connections(
    tmp_path: Path,
) -> None:
    first_database = create_database(tmp_path)
    second_database = create_database(tmp_path)
    first = SystemRepository(first_database)
    second = SystemRepository(second_database)
    await asyncio.gather(first.initialize(), second.initialize())

    lease = await first.acquire_thread_lease(
        workspace_id="ws_test",
        agent_id="agent_main",
        user_id="user-1",
        external_thread_id="thread-1",
        owner_instance_id="instance-a",
        run_id="run-a",
        ttl_seconds=60,
    )
    with pytest.raises(ThreadBusyError):
        await second.acquire_thread_lease(
            workspace_id="ws_test",
            agent_id="agent_main",
            user_id="user-1",
            external_thread_id="thread-1",
            owner_instance_id="instance-b",
            run_id="run-b",
            ttl_seconds=60,
        )
    with pytest.raises(RunAlreadyActiveError):
        await second.acquire_thread_lease(
            workspace_id="ws_test",
            agent_id="agent_main",
            user_id="user-1",
            external_thread_id="thread-1",
            owner_instance_id="instance-b",
            run_id="run-a",
            ttl_seconds=60,
        )

    assert await first.release_thread_lease(lease) is True
    await first_database.close()
    await second_database.close()


@pytest.mark.asyncio
async def test_expired_thread_lease_fences_old_owner_and_abandons_run(
    tmp_path: Path,
) -> None:
    first_database = create_database(tmp_path)
    second_database = create_database(tmp_path)
    first = SystemRepository(first_database)
    second = SystemRepository(second_database)
    await asyncio.gather(first.initialize(), second.initialize())

    old_lease = await first.acquire_thread_lease(
        workspace_id="ws_test",
        agent_id="agent_main",
        user_id="user-1",
        external_thread_id="thread-1",
        owner_instance_id="instance-a",
        run_id="run-a",
        ttl_seconds=0,
    )
    old_run = await first.start_agent_run(
        workspace_id="ws_test",
        agent_id="agent_main",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="internal-thread-1",
        run_id="run-a",
        config_generation=7,
        owner_instance_id="instance-a",
        fencing_token=old_lease.fencing_token,
    )
    assert old_run is not None

    new_lease = await second.acquire_thread_lease(
        workspace_id="ws_test",
        agent_id="agent_main",
        user_id="user-1",
        external_thread_id="thread-1",
        owner_instance_id="instance-b",
        run_id="run-b",
        ttl_seconds=60,
    )
    assert new_lease.fencing_token > old_lease.fencing_token
    assert await first.release_thread_lease(old_lease) is False

    async with second_database.session() as session:
        stored = await session.get(AgentRunRecord, old_run.id)
        assert stored is not None
        assert stored.status == "abandoned"
        assert stored.terminal_reason == "THREAD_LEASE_EXPIRED"

    assert await second.release_thread_lease(new_lease) is True
    await first_database.close()
    await second_database.close()
