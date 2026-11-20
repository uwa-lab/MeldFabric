from __future__ import annotations

import asyncio

import pytest

from meldfabric import AgentKit
from meldfabric.agents.concurrency import KeyedAsyncLock
from meldfabric.errors import ThreadBusyError
from tests.support.strands_model import ScriptedModelAdapter, ScriptedStrandsModel
from tests.support.workspace import configure_strands_workspace


class BlockingModel(ScriptedStrandsModel):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, *args, **kwargs):
        self.started.set()
        await self.release.wait()
        async for event in super().stream(*args, **kwargs):
            yield event


@pytest.mark.asyncio
async def test_keyed_lock_serializes_one_key_without_blocking_other_keys() -> None:
    locks: KeyedAsyncLock[str] = KeyedAsyncLock()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    same_key_entered = asyncio.Event()
    other_key_entered = asyncio.Event()

    async def first() -> None:
        async with locks.hold("thread-a"):
            first_entered.set()
            await release_first.wait()

    async def same_key() -> None:
        async with locks.hold("thread-a"):
            same_key_entered.set()

    async def other_key() -> None:
        async with locks.hold("thread-b"):
            other_key_entered.set()

    first_task = asyncio.create_task(first())
    await first_entered.wait()
    same_task = asyncio.create_task(same_key())
    other_task = asyncio.create_task(other_key())

    await asyncio.wait_for(other_key_entered.wait(), timeout=1)
    assert not same_key_entered.is_set()

    release_first.set()
    await asyncio.gather(first_task, same_task, other_task)
    assert same_key_entered.is_set()


@pytest.mark.asyncio
async def test_runtime_state_lookup_for_one_thread_does_not_block_another(
    tmp_path, monkeypatch
) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    first_lookup_started = asyncio.Event()
    release_first_lookup = asyncio.Event()

    class PausedRunRepository:
        async def get_paused_agent_run(self, *, external_thread_id: str, **_kwargs):
            if external_thread_id == "thread-a":
                first_lookup_started.set()
                await release_first_lookup.wait()
            return None

    repository = PausedRunRepository()
    monkeypatch.setattr(
        kit.storage,
        "all_system_repositories",
        lambda: (repository,),
    )

    first = asyncio.create_task(
        kit.agent_runtime.revision_for_stream(
            endpoint_id="endpoint-agent",
            user_id="user-one",
            thread_id="thread-a",
            resume=False,
        )
    )
    await asyncio.wait_for(first_lookup_started.wait(), timeout=1)

    second_revision = await asyncio.wait_for(
        kit.agent_runtime.revision_for_stream(
            endpoint_id="endpoint-agent",
            user_id="user-one",
            thread_id="thread-b",
            resume=False,
        ),
        timeout=1,
    )
    assert second_revision == kit.snapshots.current.revision

    release_first_lookup.set()
    assert await first == kit.snapshots.current.revision


@pytest.mark.asyncio
async def test_two_pods_reject_the_later_run_for_the_same_thread(tmp_path) -> None:
    first_model = BlockingModel()
    first = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    await configure_strands_workspace(first, first_model, include_orchestrations=False)
    second = AgentKit(state_dir=tmp_path)
    second.model_adapters.register(ScriptedModelAdapter(ScriptedStrandsModel()))

    await first.storage.initialize(first.runtime_config)
    await first.persistence.initialize(first.runtime_config)
    await second.storage.initialize(second.runtime_config)
    await second.persistence.initialize(second.runtime_config)

    async def consume_first() -> None:
        async for _event in first.agent_runtime.stream(
            "endpoint_agent",
            {"messages": [{"role": "user", "content": "hold"}]},
            user_id="same-user",
            thread_id="same-thread",
            run_id="run-first",
        ):
            pass

    task = asyncio.create_task(consume_first())
    await asyncio.wait_for(first_model.started.wait(), timeout=2)
    try:
        with pytest.raises(ThreadBusyError):
            async for _event in second.agent_runtime.stream(
                "endpoint_agent",
                {"messages": [{"role": "user", "content": "later"}]},
                user_id="same-user",
                thread_id="same-thread",
                run_id="run-second",
            ):
                pass
    finally:
        first_model.release.set()
        await asyncio.wait_for(task, timeout=2)
        await first.persistence.close()
        await first.storage.close()
        await second.persistence.close()
        await second.storage.close()


@pytest.mark.asyncio
async def test_expired_thread_lease_can_be_recovered_after_a_crashed_pod(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    await configure_strands_workspace(kit, ScriptedStrandsModel(), include_orchestrations=False)
    await kit.storage.initialize(kit.runtime_config)
    repository = await kit.storage.control_repository(kit.runtime_config)
    lease = await repository.acquire_thread_lease(
        workspace_id=kit.runtime_config.workspace.id,
        agent_id="endpoint_agent",
        user_id="user-one",
        external_thread_id="crashed-thread",
        owner_instance_id="dead-pod",
        run_id="dead-run",
        ttl_seconds=1,
    )
    assert lease.owner_instance_id == "dead-pod"
    await asyncio.sleep(1.1)
    recovered = await repository.acquire_thread_lease(
        workspace_id=kit.runtime_config.workspace.id,
        agent_id="endpoint_agent",
        user_id="user-one",
        external_thread_id="crashed-thread",
        owner_instance_id="new-pod",
        run_id="new-run",
        ttl_seconds=5,
    )
    assert recovered.owner_instance_id == "new-pod"
    await repository.release_thread_lease(recovered)
    await kit.storage.close()
