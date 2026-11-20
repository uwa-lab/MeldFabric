from __future__ import annotations

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from meldfabric import AgentKit, register
from meldfabric.runtime import EmbeddedFastAPIDriver
from meldfabric.runtime.bootstrap import agent_kit_from_environment
from meldfabric.runtime.drivers import (
    BOOTSTRAP_ADMIN_KEY_ENV_ENV,
    BOOTSTRAP_CONTROL_DATABASE_ENV,
    BOOTSTRAP_REGISTRATION_MODULES_ENV,
    BOOTSTRAP_STATE_DIR_ENV,
)


@register.graph_condition(name="always")
def always_condition(state) -> bool:
    return state is not None


@pytest.mark.asyncio
async def test_embedded_driver_uses_import_factory_for_multiple_workers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    loaded = await kit.apatch_config({"runtime": {"workers": 3, "host": "0.0.0.0", "port": 8123}})
    await kit.aapply_latest_config(expected_generation=loaded.config.config_generation)
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))

    driver = kit.runtime_driver()
    assert isinstance(driver, EmbeddedFastAPIDriver)
    driver.run()

    assert calls == [
        (
            "meldfabric.runtime.bootstrap:create_embedded_app",
            {"factory": True, "host": "0.0.0.0", "port": 8123, "workers": 3},
        )
    ]
    assert os.environ[BOOTSTRAP_CONTROL_DATABASE_ENV] == kit.repository.database.sync_url
    assert os.environ[BOOTSTRAP_STATE_DIR_ENV] == str(kit.repository.state_dir)


def test_bootstrap_reconstructs_workspace_and_decorated_registrations(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    monkeypatch.setenv(BOOTSTRAP_CONTROL_DATABASE_ENV, original.repository.database.sync_url)
    monkeypatch.setenv(BOOTSTRAP_STATE_DIR_ENV, str(original.repository.state_dir))
    monkeypatch.setenv(BOOTSTRAP_ADMIN_KEY_ENV_ENV, "MELDFABRIC_ADMIN_KEY")
    monkeypatch.setenv(
        BOOTSTRAP_REGISTRATION_MODULES_ENV,
        json.dumps(["tests.test_runtime_drivers"]),
    )

    reconstructed = agent_kit_from_environment()
    assert reconstructed.config.workspace.id == original.config.workspace.id
    assert any(
        item["name"] == "always"
        for item in reconstructed.registrations.catalog()["graph_condition"]
    )


@pytest.mark.asyncio
async def test_follower_pod_adopts_only_the_explicitly_applied_generation(tmp_path) -> None:
    publisher = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    follower = AgentKit(state_dir=tmp_path)
    await follower.storage.initialize(follower.runtime_config)
    await follower.persistence.initialize(follower.runtime_config)
    saved = await publisher.apatch_config({"workspace": {"name": "cross-pod"}})

    await follower.asynchronize_configuration()
    assert follower.config.workspace.name == "cross-pod"
    assert follower.runtime_config.workspace.name != "cross-pod"

    await publisher.aapply_latest_config(expected_generation=saved.config.config_generation)
    await follower.asynchronize_configuration()
    assert follower.runtime_config.workspace.name == "cross-pod"
    await follower.persistence.close()
    await follower.storage.close()


@pytest.mark.asyncio
async def test_background_maintenance_adopts_an_applied_generation(tmp_path) -> None:
    publisher = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    follower = AgentKit(state_dir=tmp_path)
    follower.agent_runtime.maintenance_interval_seconds = 0.05
    await follower.storage.initialize(follower.runtime_config)
    await follower.persistence.initialize(follower.runtime_config)
    await follower.agent_runtime.start()
    saved = await publisher.apatch_config({"workspace": {"name": "published"}})
    await publisher.aapply_latest_config(expected_generation=saved.config.config_generation)

    async with asyncio.timeout(2):
        while follower.runtime_config.config_generation != saved.config.config_generation:
            await asyncio.sleep(0.02)
    assert follower.runtime_config.workspace.name == "published"
    await follower.agent_runtime.close()
    await follower.persistence.close()
    await follower.storage.close()


def test_embedded_app_combines_management_and_business_routes(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    headers = {"Authorization": "Bearer test-admin-key"}
    with TestClient(kit.create_app()) as client:
        assert client.get("/admin/config", headers=headers).status_code == 200
        assert client.post("/api/invoke", json={}).status_code == 422
