from __future__ import annotations

import json
import time
from functools import partial
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from meldfabric import AgentKit, register
from tests.support.strands_model import ScriptedModelAdapter, ScriptedStrandsModel


def auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-admin-key"}


def generation(client: TestClient) -> int:
    return client.get("/admin/config", headers=auth()).json()["config_generation"]


def catalog_digest(client: TestClient) -> str:
    return client.get("/admin/runtime/capabilities", headers=auth()).json()["code_catalog_digest"]


def create_minimal_agent_stack(client: TestClient) -> tuple[dict, dict, dict, dict]:
    provider = client.post(
        "/admin/resources/model_providers",
        headers=auth(),
        json={
            "resource": {
                "name": "Scripted",
                "adapter": "scripted",
                "base_url": "https://model.invalid/v1",
            },
            "expected_generation": generation(client),
            "expected_catalog_digest": catalog_digest(client),
        },
    )
    assert provider.status_code == 200, provider.text
    provider = provider.json()
    model = client.post(
        "/admin/resources/models",
        headers=auth(),
        json={
            "resource": {
                "name": "scripted-model",
                "provider_ref": provider["id"],
                "model_name": "scripted-model",
                "capabilities": {"tool_calling": True},
            },
            "expected_generation": generation(client),
            "expected_catalog_digest": catalog_digest(client),
        },
    )
    assert model.status_code == 200, model.text
    model = model.json()
    agent = client.post(
        "/admin/agents",
        headers=auth(),
        json={
            "resource": {"name": "assistant", "model_ref": model["id"]},
            "expected_generation": generation(client),
            "expected_catalog_digest": catalog_digest(client),
        },
    )
    assert agent.status_code == 200, agent.text
    agent = agent.json()
    endpoint = client.post(
        "/admin/endpoints",
        headers=auth(),
        json={
            "resource": {
                "name": "assistant_api",
                "target_ref": agent["id"],
                "protocols": ["rest", "meldfabric_remote"],
            },
            "expected_generation": generation(client),
            "expected_catalog_digest": catalog_digest(client),
        },
    )
    assert endpoint.status_code == 200, endpoint.text
    return provider, model, agent, endpoint.json()


def test_management_crud_uses_server_ids_generations_and_cascade_delete(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    kit.model_adapters.register(ScriptedModelAdapter(ScriptedStrandsModel()))
    with TestClient(kit.create_app()) as client:
        provider, model, agent, endpoint = create_minimal_agent_stack(client)
        assert provider["id"].startswith("provider_")
        assert model["id"].startswith("model_")
        assert agent["id"].startswith("agent_")
        assert endpoint["id"].startswith("endpoint_")

        status = client.get("/admin/runtime/config-status", headers=auth()).json()
        assert status["latest_generation"] > status["active_generation"]
        applied = client.post(
            "/admin/runtime/apply",
            headers=auth(),
            json={
                "expected_generation": status["latest_generation"],
                "expected_catalog_digest": catalog_digest(client),
            },
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["active_generation"] == status["latest_generation"]

        references = client.get(f"/admin/resource-references/{model['id']}", headers=auth()).json()
        assert references["total"] == 1
        assert references["groups"][0]["items"][0]["owner_name"] == "assistant"

        deleted = client.delete(
            f"/admin/agents/{agent['id']}?expected_generation={generation(client)}"
            f"&expected_catalog_digest={catalog_digest(client)}",
            headers=auth(),
        )
        assert deleted.status_code == 200, deleted.text
        assert endpoint["id"] in deleted.json()["removed_endpoint_ids"]
        config = client.get("/admin/config", headers=auth()).json()
        assert not config["agents"]
        assert not config["endpoints"]


def test_endpoint_key_list_hides_inactive_keys_by_default(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    kit.model_adapters.register(ScriptedModelAdapter(ScriptedStrandsModel()))
    with TestClient(kit.create_app()) as client:
        _, _, _, endpoint = create_minimal_agent_stack(client)
        created = client.post(
            f"/admin/endpoints/{endpoint['id']}/keys",
            headers=auth(),
            json={"kind": "agent_api"},
        )
        assert created.status_code == 200, created.text
        key_id = created.json()["id"]
        revoked = client.patch(
            f"/admin/keys/{key_id}",
            headers=auth(),
            json={"status": "revoked"},
        )
        assert revoked.status_code == 200, revoked.text

        active_only = client.get(
            f"/admin/endpoints/{endpoint['id']}/keys",
            headers=auth(),
        )
        all_keys = client.get(
            f"/admin/endpoints/{endpoint['id']}/keys?include_inactive=true",
            headers=auth(),
        )

        assert active_only.status_code == 200
        assert active_only.json()["items"] == []
        assert all_keys.status_code == 200
        assert all_keys.json()["items"][0]["id"] == key_id
        assert all_keys.json()["items"][0]["status"] == "revoked"


def test_playground_lists_latest_endpoints_and_persists_continuable_history(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    model = ScriptedStrandsModel(lambda _messages, _tools, index: f"response {index}")
    kit.model_adapters.register(ScriptedModelAdapter(model))
    with TestClient(kit.create_app()) as client:
        _, _, _, endpoint = create_minimal_agent_stack(client)
        pending = client.get("/admin/playground/endpoints", headers=auth())
        assert pending.status_code == 200
        assert pending.json()["items"][0]["exists_in_runtime"] is False

        unavailable = client.post(
            "/admin/playground/conversations",
            headers=auth(),
            json={"endpoint_id": endpoint["id"]},
        )
        assert unavailable.status_code == 409
        status = client.get("/admin/runtime/config-status", headers=auth()).json()
        applied = client.post(
            "/admin/runtime/apply",
            headers=auth(),
            json={
                "expected_generation": status["latest_generation"],
                "expected_catalog_digest": catalog_digest(client),
            },
        )
        assert applied.status_code == 200, applied.text

        created = client.post(
            "/admin/playground/conversations",
            headers=auth(),
            json={"endpoint_id": endpoint["id"]},
        )
        assert created.status_code == 200, created.text
        conversation = created.json()
        assert conversation["user_id"].startswith("playground:ws_")

        for index, content in enumerate(("hello", "continue"), start=1):
            with client.stream(
                "POST",
                f"/admin/playground/conversations/{conversation['id']}/runs",
                headers=auth(),
                json={"message_id": f"user-{index}", "content": content},
            ) as response:
                assert response.status_code == 200, response.read().decode()
                events = [
                    json.loads(line.removeprefix("data: "))
                    for line in response.iter_lines()
                    if line.startswith("data: ")
                ]
            assert events[0]["type"] == "RUN_STARTED"
            assert events[-1]["type"] == "RUN_FINISHED"

        detail = client.get(
            f"/admin/playground/conversations/{conversation['id']}",
            headers=auth(),
        )
        assert detail.status_code == 200
        body = detail.json()
        assert body["conversation"]["title"] == "hello"
        assert [item["role"] for item in body["messages"] if item["kind"] == "message"] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert len(model.calls) == 2
        assert any(
            block.get("text") == "hello"
            for message in model.calls[-1]["messages"]
            for block in message.get("content", [])
            if isinstance(block, dict)
        )

        deleted = client.delete(
            f"/admin/playground/conversations/{conversation['id']}",
            headers=auth(),
        )
        assert deleted.status_code == 200
        assert deleted.json()["agent_session_and_memory_retained"] is True
        assert (
            client.get(
                f"/admin/playground/conversations/{conversation['id']}",
                headers=auth(),
            ).status_code
            == 404
        )


def test_code_registration_schema_drives_resource_validation(tmp_path) -> None:
    class PluginSettings(BaseModel):
        threshold: int = Field(ge=1, description="minimum accepted score")

    @register.plugin(
        name="management_schema_plugin",
        description="plugin used to verify the code catalog",
        config_model=PluginSettings,
    )
    class ManagementSchemaPlugin:
        pass

    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    with TestClient(kit.create_app()) as client:
        capabilities = client.get("/admin/runtime/capabilities", headers=auth()).json()
        definition = next(
            item
            for item in capabilities["code_registrations"]["plugin"]
            if item["name"] == "management_schema_plugin"
        )
        assert definition["config_schema"]["properties"]["threshold"]["minimum"] == 1

        invalid = client.post(
            "/admin/resources/plugins",
            headers=auth(),
            json={
                "resource": {
                    "kind": "registered",
                    "name": "schema-plugin",
                    "registration_id": definition["id"],
                    "config": {"threshold": 0},
                },
                "expected_generation": generation(client),
                "expected_catalog_digest": catalog_digest(client),
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "CONFIG_VALIDATION_FAILED"

        created = client.post(
            "/admin/resources/plugins",
            headers=auth(),
            json={
                "resource": {
                    "kind": "registered",
                    "name": "schema-plugin",
                    "registration_id": definition["id"],
                    "config": {"threshold": 2},
                },
                "expected_generation": generation(client),
                "expected_catalog_digest": catalog_digest(client),
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["registration_id"] == definition["id"]


def test_backup_target_is_transient_and_contains_verified_control_data(tmp_path: Path) -> None:
    kit = AgentKit(state_dir=tmp_path / "source", admin_key="test-admin-key")
    target = tmp_path / "backup" / "control.db"
    target.parent.mkdir()
    with TestClient(kit.create_app()) as client:
        response = client.post(
            "/admin/system/backups",
            headers=auth(),
            json={
                "engine": "sqlite",
                "url": f"sqlite:///{target}",
                "options": {},
                "admin_key": "test-admin-key",
                "overwrite": False,
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["backup_id"].startswith("backup_")
    assert target.exists()
    assert str(target) not in kit.config.model_dump_json()


def test_backup_rejects_database_type_that_disagrees_with_url(tmp_path: Path) -> None:
    kit = AgentKit(state_dir=tmp_path / "source", admin_key="test-admin-key")
    with TestClient(kit.create_app()) as client:
        response = client.post(
            "/admin/system/backups",
            headers=auth(),
            json={
                "engine": "postgresql",
                "url": f"sqlite:///{tmp_path / 'backup.db'}",
                "options": {},
                "admin_key": "test-admin-key",
                "overwrite": False,
            },
        )
    assert response.status_code == 422
    assert response.json()["error"]["details"] == {
        "selected_engine": "postgresql",
        "url_engine": "sqlite",
    }


def test_runtime_suspension_blocks_non_health_traffic_until_restart(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    with TestClient(kit.create_app()) as client:
        unconfirmed = client.post("/admin/runtime/suspend", headers=auth(), json={"confirm": False})
        assert unconfirmed.status_code == 422
        suspended = client.post("/admin/runtime/suspend", headers=auth(), json={"confirm": True})
        assert suspended.status_code == 200, suspended.text
        assert suspended.json()["suspended"] is True
        for path in ("/admin/", "/admin/config", "/api/invoke", "/docs", "/ready"):
            response = client.get(path, headers=auth())
            assert response.status_code == 503, (path, response.text)
            assert response.json()["error"]["code"] == "SERVICE_SUSPENDED"
        assert client.get("/health").status_code == 200
        assert client.get("/health/live").status_code == 200

    restarted = AgentKit(state_dir=tmp_path)
    with TestClient(restarted.create_app()) as client:
        assert client.get("/admin/config", headers=auth()).status_code == 200


def test_runtime_suspension_reaches_other_registered_instances(tmp_path) -> None:
    first = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    second = AgentKit(state_dir=tmp_path)
    first.agent_runtime.instance_heartbeat_interval_seconds = 0.05
    second.agent_runtime.instance_heartbeat_interval_seconds = 0.05

    with (
        TestClient(first.create_app()) as first_client,
        TestClient(second.create_app()) as second_client,
    ):
        suspended = first_client.post(
            "/admin/runtime/suspend", headers=auth(), json={"confirm": True}
        )
        assert suspended.status_code == 200
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if second_client.get("/admin/config", headers=auth()).status_code == 503:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("second runtime instance did not adopt suspension")


def test_configuration_write_rejects_mixed_ready_code_catalogs(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    with TestClient(kit.create_app()) as client:
        client.portal.call(
            partial(
                kit.storage.system.register_runtime_instance,
                instance_id="foreign-code-instance",
                workspace_id=kit.config.workspace.id,
                hostname="foreign-pod",
                process_id=999,
                config_generation=kit.runtime_config.config_generation,
                catalog_digest="0" * 64,
            )
        )
        response = client.patch(
            "/admin/config",
            headers=auth(),
            json={
                "patch": {"workspace": {"name": "must-not-save"}},
                "expected_generation": generation(client),
                "expected_catalog_digest": kit.catalog_digest,
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CODE_CATALOG_CONFLICT"


def test_admin_key_can_be_user_defined_and_old_key_expires_immediately(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    new_key = "a-user-defined-admin-key"
    with TestClient(kit.create_app()) as client:
        response = client.put(
            "/admin/security/admin-key",
            headers=auth(),
            json={"new_key": new_key, "confirm": True},
        )
        assert response.status_code == 200, response.text
        assert client.get("/admin/config", headers=auth()).status_code == 401
        assert (
            client.get(
                "/admin/config",
                headers={"Authorization": f"Bearer {new_key}"},
            ).status_code
            == 200
        )


def test_snapshot_can_be_deleted_without_reusing_its_version(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    with TestClient(kit.create_app()) as client:
        created = client.post(
            "/admin/config/snapshots",
            headers=auth(),
            json={"label": "temporary", "expected_generation": generation(client)},
        )
        assert created.status_code == 200, created.text
        version = created.json()["version"]
        deleted = client.delete(f"/admin/config/snapshots/{version}", headers=auth())
        assert deleted.status_code == 200, deleted.text
        assert client.get("/admin/config/snapshots", headers=auth()).json()["items"] == []

        next_snapshot = client.post(
            "/admin/config/snapshots",
            headers=auth(),
            json={"label": "next", "expected_generation": generation(client)},
        )
        assert next_snapshot.status_code == 200, next_snapshot.text
        assert next_snapshot.json()["version"] == version + 1
