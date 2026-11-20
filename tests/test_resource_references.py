import pytest
from fastapi.testclient import TestClient

from meldfabric import AgentKit
from meldfabric.config.models import (
    AgentDefinition,
    MemoryManagerConfig,
    ModelProvider,
    ModelResource,
    SessionManagerConfig,
)


@pytest.mark.asyncio
async def test_reference_api_groups_named_owners_and_routes_without_writes(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    provider = ModelProvider(
        id="provider_one",
        name="Provider",
        base_url="https://model.invalid/v1",
    )
    model = ModelResource(
        id="model_one",
        provider_ref=provider.id,
        model_name="model-one",
    )
    agent = AgentDefinition(
        id="agent_support",
        name="support",
        display_name="Support Agent",
        model_ref=model.id,
        session_manager=SessionManagerConfig(database_ref="db_default"),
        memory_manager=MemoryManagerConfig(database_ref="db_default"),
    )
    await kit.apatch_config(
        {
            "resources": {
                "model_providers": [provider.model_dump(mode="json")],
                "models": [model.model_dump(mode="json")],
            },
            "agents": [agent.model_dump(mode="json")],
        }
    )
    before = kit.config.config_generation
    headers = {"Authorization": "Bearer test-admin-key"}
    with TestClient(kit.create_app()) as client:
        assert client.get(f"/admin/resource-references/{model.id}").status_code == 401
        model_refs = client.get(f"/admin/resource-references/{model.id}", headers=headers).json()
        database_refs = client.get("/admin/resource-references/db_default", headers=headers).json()

    assert model_refs["total"] == 1
    item = model_refs["groups"][0]["items"][0]
    assert item["owner_name"] == "Support Agent"
    assert item["usage"] == "推理模型"
    assert item["href"] == "/agents/agent_support"
    assert database_refs["total"] == 2
    assert {item["section"] for item in database_refs["groups"][0]["items"]} == {"persistence"}
    assert kit.config.config_generation == before
