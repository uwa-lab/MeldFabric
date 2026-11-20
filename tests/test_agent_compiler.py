from __future__ import annotations

import pytest

from meldfabric import AgentKit
from meldfabric.agents.native_compiler import NativeExecutionResult
from tests.support.strands_model import ScriptedStrandsModel
from tests.support.workspace import configure_strands_workspace


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint_id",
    [
        "endpoint_agent",
        "endpoint_agent_as_tool",
        "endpoint_swarm",
        "endpoint_graph",
        "endpoint_workflow",
    ],
)
async def test_all_native_executable_kinds_compile_and_invoke(tmp_path, endpoint_id: str) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    await configure_strands_workspace(kit, ScriptedStrandsModel())
    await kit.storage.initialize(kit.runtime_config)
    await kit.persistence.initialize(kit.runtime_config)
    try:
        compiled = await kit.agent_runtime.compiled_endpoint(
            kit.runtime_config.config_generation,
            endpoint_id,
        )
        events = [
            event
            async for event in compiled.stream(
                {"messages": [{"role": "user", "content": "hello"}]},
                user_id="user-one",
                thread_id=f"thread-{endpoint_id}",
            )
        ]
    finally:
        await kit.persistence.close()
        await kit.storage.close()
    result = next(event for event in events if isinstance(event, NativeExecutionResult))
    assert result.output
    assert result.executable_id


@pytest.mark.asyncio
async def test_metrics_are_structured_in_execution_result(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    await configure_strands_workspace(kit, ScriptedStrandsModel(), include_orchestrations=False)
    await kit.storage.initialize(kit.runtime_config)
    await kit.persistence.initialize(kit.runtime_config)
    try:
        result, _revision, _run_id = await kit.agent_runtime.invoke(
            "endpoint_agent",
            [{"role": "user", "content": "hello"}],
            user_id="user-one",
            thread_id="thread-one",
        )
    finally:
        await kit.persistence.close()
        await kit.storage.close()
    assert isinstance(result["metrics"], dict)
