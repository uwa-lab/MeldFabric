# ruff: noqa: E402
"""Acceptance script runnable from the base wheel without optional integrations."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory


class NoOptionalIntegrations:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {
            "docker",
            "langfuse",
        }:
            raise ModuleNotFoundError(f"optional adapter intentionally absent: {fullname}")
        return None


sys.meta_path.insert(0, NoOptionalIntegrations())

from fastapi.testclient import TestClient

from meldfabric import AgentKit, register
from meldfabric.config.models import (
    AgentAsToolOrchestration,
    AgentDefinition,
    AgentToolMember,
    MemoryManagerConfig,
    ModelCapabilities,
    ModelProvider,
    ModelResource,
    PublishedEndpoint,
    SessionManagerConfig,
    ToolBinding,
)

results: list[int] = []
requests: list[dict] = []


@register.tool(id="smoke_add", description="Add two numbers")
def smoke_add(a: int, b: int) -> int:
    result = a + b
    results.append(result)
    return result


class ModelService(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": [{"id": "smoke-model"}]}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(body)
        messages = body["messages"]
        names = {item["function"]["name"] for item in body.get("tools", [])}
        last = messages[-1]
        content, function, arguments = "sum=5", None, None
        if last["role"] == "user" and "recall" in str(last.get("content")):
            content = (
                "remembered"
                if any(
                    message["role"] == "user" and "delegate" in str(message.get("content"))
                    for message in messages
                )
                else "new-user"
            )
        elif last["role"] != "tool":
            if "worker" in names:
                function, arguments = "worker", {"input": "Add 2 and 3"}
            elif "smoke_add" in names:
                function, arguments = "smoke_add", {"a": 2, "b": 3}
        delta = {"role": "assistant", "content": content if function is None else None}
        if function:
            delta["tool_calls"] = [
                {
                    "index": 0,
                    "id": f"call_{len(requests)}",
                    "type": "function",
                    "function": {
                        "name": function,
                        "arguments": json.dumps(arguments),
                    },
                }
            ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {
            "id": f"chatcmpl-{len(requests)}",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "smoke-model",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": "tool_calls" if function else "stop",
                }
            ],
        }
        self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())


def run() -> None:
    os.environ["MELDFABRIC_ADMIN_KEY"] = "core-install-smoke-admin"
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelService)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with TemporaryDirectory(prefix="meldfabric-core-smoke-") as folder:
            state_dir = Path(folder) / "state"
            kit = AgentKit(state_dir=state_dir)
            provider = ModelProvider(
                id="provider_local",
                name="Local",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
            )
            model = ModelResource(
                id="model_local",
                name="local",
                provider_ref=provider.id,
                model_name="smoke-model",
                capabilities=ModelCapabilities(tool_calling=True),
            )
            child = AgentDefinition(
                id="agent_worker",
                name="worker",
                model_ref=model.id,
                description="Do arithmetic",
                tool_bindings=[ToolBinding(tool_ref="smoke_add")],
            )
            coordinator = AgentDefinition(
                id="agent_coordinator",
                name="coordinator",
                model_ref=model.id,
                session_manager=SessionManagerConfig(database_ref="db_default"),
                memory_manager=MemoryManagerConfig(database_ref="db_default"),
            )
            orchestration = AgentAsToolOrchestration(
                id="orch_team",
                name="team",
                coordinator_ref=coordinator.id,
                members=[AgentToolMember(agent_ref=child.id)],
            )
            endpoint = PublishedEndpoint(
                id="endpoint_team",
                name="team_api",
                target_type="orchestration",
                target_ref=orchestration.id,
                protocols=["rest", "ag_ui"],
            )
            asyncio.run(
                kit.apatch_config(
                    {
                        "resources": {
                            "model_providers": [provider.model_dump(mode="json")],
                            "models": [model.model_dump(mode="json")],
                        },
                        "agents": [
                            coordinator.model_dump(mode="json"),
                            child.model_dump(mode="json"),
                        ],
                        "orchestrations": [orchestration.model_dump(mode="json")],
                        "endpoints": [endpoint.model_dump(mode="json")],
                    }
                )
            )
            admin = {"Authorization": "Bearer core-install-smoke-admin"}
            with TestClient(kit.create_app()) as client:
                assert client.get("/docs").status_code == 200
                assert client.get("/admin").status_code == 200
                catalog = client.get("/admin/runtime/capabilities", headers=admin).json()
                assert not catalog["features"]["sandbox"]["available"]
                assert not catalog["features"]["langfuse"]["available"]
                saved = client.put(
                    f"/admin/resources/model-providers/{provider.id}/secret",
                    headers=admin,
                    json={
                        "value": "local-test-key",
                        "expected_catalog_digest": catalog["code_catalog_digest"],
                    },
                )
                assert saved.status_code == 200, saved.text
                applied = client.post(
                    "/admin/runtime/apply",
                    headers=admin,
                    json={
                        "expected_generation": saved.json()["config_generation"],
                        "expected_catalog_digest": catalog["code_catalog_digest"],
                    },
                )
                assert applied.status_code == 200, applied.text
                created = client.post(
                    f"/admin/endpoints/{endpoint.id}/keys",
                    headers=admin,
                    json={"kind": "agent_api"},
                )
                key = created.json()["value"]
                auth = {"Authorization": "Bearer " + key}
                response = client.post(
                    "/api/invoke",
                    headers=auth,
                    json={
                        "messages": [{"role": "user", "content": "delegate"}],
                        "user_id": "alice",
                        "thread_id": "thread",
                    },
                )
                assert response.status_code == 200, response.text
                assert "sum=5" in response.text and results == [5], response.text
                streamed = client.post(
                    "/api/ag-ui",
                    headers={**auth, "X-User-ID": "alice"},
                    json={
                        "threadId": "stream-thread",
                        "runId": "stream-run",
                        "messages": [{"id": "stream-input", "role": "user", "content": "recall"}],
                    },
                )
                events = [
                    json.loads(line.removeprefix("data: "))
                    for line in streamed.text.splitlines()
                    if line.startswith("data: ")
                ]
                assert events[0]["type"] == "RUN_STARTED", events
                assert events[-1]["outcome"] == {"type": "success"}, events
            restarted = AgentKit(state_dir=state_dir)
            with TestClient(restarted.create_app()) as client:
                response = client.post(
                    "/api/invoke",
                    headers=auth,
                    json={
                        "messages": [{"role": "user", "content": "recall"}],
                        "user_id": "alice",
                        "thread_id": "thread",
                    },
                )
                assert response.status_code == 200 and "remembered" in response.text
            assert b"local-test-key" not in (state_dir / "control.db").read_bytes()
        print("CORE_INSTALL_OK: Strands, API, tool, orchestration, SQLite and sessions")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    run()
