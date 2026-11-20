from __future__ import annotations

import asyncio
import socket
import threading

import httpx
import pytest
import uvicorn

from meldfabric import AgentKit
from meldfabric.config.models import (
    GraphEdge,
    GraphNode,
    GraphOrchestration,
    PublishedEndpoint,
    RemoteAgent,
)
from meldfabric.protocols.a2a import A2A_PROTOCOL
from meldfabric.remote import A2ARemoteClient, MeldFabricRemoteClient, RemoteCallContext
from meldfabric.remote.manager import RemoteAgentManager
from tests.support.strands_model import ScriptedStrandsModel
from tests.support.workspace import configure_strands_workspace


@pytest.mark.asyncio
async def test_meldfabric_remote_discovery_topology_and_invoke_are_end_to_end(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    await configure_strands_workspace(kit, ScriptedStrandsModel(), include_orchestrations=False)
    app = kit.create_app()
    async with app.router.lifespan_context(app):
        repository = await kit.storage.require_secrets(kit.runtime_config)
        key = await repository.create_inbound_key(
            kind="remote_access",
            agent_id="endpoint_agent",
            scopes=["discover", "invoke", "topology:read"],
        )
        transport = httpx.ASGITransport(app=app)
        client = MeldFabricRemoteClient("http://testserver", key.value, transport=transport)
        descriptor = await client.discover()
        topology = await client.topology()
        result = await client.invoke(
            messages=[{"role": "user", "content": "hello"}],
            user_id="user-one",
            thread_id="thread-remote",
            remote_context=RemoteCallContext.root(
                service_id="parent-service", agent_id="parent-agent"
            ),
        )

    assert descriptor.agent_id == "endpoint_agent"
    assert "meldfabric_remote/2" in descriptor.protocols
    assert topology.nodes[0].agent_id == "endpoint_agent"
    assert result.status == "completed"
    assert result.result["output"]


@pytest.mark.asyncio
async def test_native_a2a_card_and_invocation_are_end_to_end(tmp_path) -> None:
    kit = AgentKit(state_dir=tmp_path, admin_key="test-admin-key")
    await configure_strands_workspace(kit, ScriptedStrandsModel(), include_orchestrations=False)
    app = kit.create_app()
    async with app.router.lifespan_context(app):
        repository = await kit.storage.require_secrets(kit.runtime_config)
        key = await repository.create_inbound_key(
            kind="remote_access",
            agent_id="endpoint_agent",
            scopes=["discover", "invoke"],
        )
        client = A2ARemoteClient(
            "http://testserver",
            key.value,
            transport=httpx.ASGITransport(app=app),
        )
        descriptor = await client.discover()
        result = await client.invoke(
            messages=[{"role": "user", "content": "hello over A2A"}],
            user_id="user-one",
            thread_id="thread-a2a",
            remote_context=RemoteCallContext.root(
                service_id="parent-service", agent_id="parent-agent"
            ),
        )

    assert descriptor.protocols == [A2A_PROTOCOL]
    assert result.status == "completed"
    assert result.result["messages"]


def test_remote_manager_prefers_meldfabric_semantics_when_peer_advertises_both() -> None:
    resource = RemoteAgent(
        name="peer",
        base_url="https://peer.invalid",
        protocols=["meldfabric_remote/2", A2A_PROTOCOL],
    )
    both = RemoteAgentManager._default_client_factory(resource, "key")
    a2a_only = RemoteAgentManager._default_client_factory(
        resource.model_copy(update={"protocols": [A2A_PROTOCOL]}), "key"
    )
    assert isinstance(both, MeldFabricRemoteClient)
    assert isinstance(a2a_only, A2ARemoteClient)


@pytest.mark.asyncio
async def test_native_graph_executes_a_remote_a2a_node(tmp_path) -> None:
    remote_model = ScriptedStrandsModel(lambda *_args: "remote graph response")
    remote = AgentKit(state_dir=tmp_path / "remote", admin_key="remote-admin-key")
    await configure_strands_workspace(remote, remote_model, include_orchestrations=False)
    remote_app = remote.create_app()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(remote_app, host="127.0.0.1", port=port, log_level="error")
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as remote_http:
            for _ in range(100):
                try:
                    if (await remote_http.get("/health/live")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("remote A2A test service did not start")
            key_response = await remote_http.post(
                "/admin/endpoints/endpoint_agent/keys",
                headers={"Authorization": "Bearer remote-admin-key"},
                json={"kind": "remote_access"},
            )
            assert key_response.status_code == 200, key_response.text
            remote_key = key_response.json()["value"]

        parent = AgentKit(state_dir=tmp_path / "parent", admin_key="parent-admin-key")
        workspace = await configure_strands_workspace(
            parent,
            ScriptedStrandsModel(lambda *_args: "local graph response"),
            include_orchestrations=False,
        )
        local_agent = workspace["agents"][0]
        remote_resource = RemoteAgent(
            id="remote_graph_peer",
            name="graph_peer",
            base_url=base_url,
            protocols=[A2A_PROTOCOL],
        )
        graph = GraphOrchestration(
            id="orch_remote_graph",
            name="remote_graph",
            nodes=[
                GraphNode(id="node_local", target_ref=local_agent.id),
                GraphNode(
                    id="node_remote",
                    target_type="remote_agent",
                    target_ref=remote_resource.id,
                ),
            ],
            edges=[GraphEdge(source="node_local", target="node_remote")],
            entry_points=["node_local"],
        )
        endpoint = PublishedEndpoint(
            id="endpoint_remote_graph",
            name="remote_graph_api",
            target_type="orchestration",
            target_ref=graph.id,
            protocols=["rest"],
        )
        loaded = await parent.apatch_config(
            {
                "resources": {"remote_agents": [remote_resource.model_dump(mode="json")]},
                "orchestrations": [graph.model_dump(mode="json")],
                "endpoints": [
                    *[item.model_dump(mode="json") for item in parent.config.endpoints],
                    endpoint.model_dump(mode="json"),
                ],
            }
        )
        await parent.aapply_latest_config(expected_generation=loaded.config.config_generation)
        parent_app = parent.create_app()
        async with parent_app.router.lifespan_context(parent_app):
            parent_http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=parent_app),
                base_url="http://parent.test",
            )
            async with parent_http:
                admin = {"Authorization": "Bearer parent-admin-key"}
                catalog_digest = (
                    await parent_http.get("/admin/runtime/capabilities", headers=admin)
                ).json()["code_catalog_digest"]
                saved = await parent_http.put(
                    f"/admin/resources/remote_agents/{remote_resource.id}/secret",
                    headers=admin,
                    json={
                        "value": remote_key,
                        "expected_catalog_digest": catalog_digest,
                    },
                )
                assert saved.status_code == 200, saved.text
                applied = await parent_http.post(
                    "/admin/runtime/apply",
                    headers=admin,
                    json={
                        "expected_generation": saved.json()["config_generation"],
                        "expected_catalog_digest": catalog_digest,
                    },
                )
                assert applied.status_code == 200, applied.text
                created = await parent_http.post(
                    f"/admin/endpoints/{endpoint.id}/keys",
                    headers=admin,
                    json={"kind": "agent_api"},
                )
                assert created.status_code == 200, created.text
                invoked = await parent_http.post(
                    "/api/invoke",
                    headers={"Authorization": f"Bearer {created.json()['value']}"},
                    json={
                        "messages": [{"role": "user", "content": "run the graph"}],
                        "user_id": "graph-user",
                        "thread_id": "graph-thread",
                    },
                )
                assert invoked.status_code == 200, invoked.text
                assert "remote graph response" in invoked.text
                assert remote_model.calls
    finally:
        server.should_exit = True
        await asyncio.to_thread(server_thread.join, 15)
        assert not server_thread.is_alive()
