from __future__ import annotations

import json

import httpx
import pytest

from meldfabric.remote import A2ARemoteClient, RemoteCallContext


def agent_card(*, name: str = "External Agent", agent_id: str = "external_agent"):
    return {
        "name": name,
        "description": "An A2A-only Agent",
        "url": "http://remote.test/a2a/external_agent/",
        "version": "1.0.0",
        "protocolVersion": "0.3.0",
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [
            {
                "id": agent_id,
                "name": name,
                "description": "External test Agent",
                "tags": ["test"],
            }
        ],
    }


@pytest.mark.asyncio
async def test_a2a_remote_client_uses_official_sdk_for_discovery_and_invoke() -> None:
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer remote-key"
        if request.method == "GET":
            return httpx.Response(200, json=agent_card())
        payload = json.loads(request.content)
        seen_methods.append(payload["method"])
        assert payload["params"]["message"]["role"] == "user"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "kind": "task",
                    "id": "task_remote",
                    "contextId": "thread-1",
                    "status": {"state": "completed"},
                    "artifacts": [
                        {
                            "artifactId": "result",
                            "parts": [{"kind": "text", "text": "external result"}],
                        }
                    ],
                    "metadata": {"meldfabric": {"configGeneration": 3}},
                },
            },
        )

    client = A2ARemoteClient(
        "http://remote.test",
        "remote-key",
        transport=httpx.MockTransport(handler),
    )

    descriptor = await client.discover()
    result = await client.invoke(
        messages=[{"role": "user", "content": "delegate this"}],
        user_id="user-1",
        thread_id="thread-1",
        remote_context=RemoteCallContext.root(service_id="parent", agent_id="main"),
    )

    assert descriptor.agent_id == "external_agent"
    assert descriptor.protocols == ["a2a/0.3"]
    assert result.result["messages"][0]["content"] == "external result"
    assert result.config_generation == 3
    assert seen_methods == ["message/send"]


@pytest.mark.asyncio
async def test_a2a_remote_client_translates_native_input_required_task() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=agent_card(name="Interactive", agent_id="interactive"),
            )
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "kind": "task",
                    "id": "task_waiting",
                    "contextId": "thread-1",
                    "status": {
                        "state": "input-required",
                        "message": {
                            "kind": "message",
                            "messageId": "interrupt-message",
                            "contextId": "thread-1",
                            "taskId": "task_waiting",
                            "role": "agent",
                            "parts": [
                                {
                                    "kind": "data",
                                    "data": {
                                        "interrupts": [
                                            {
                                                "id": "interrupt-1",
                                                "value": {"message": "Approve?"},
                                            }
                                        ]
                                    },
                                }
                            ],
                        },
                    },
                },
            },
        )

    client = A2ARemoteClient(
        "http://remote.test",
        "remote-key",
        transport=httpx.MockTransport(handler),
    )
    result = await client.invoke(
        messages=[{"role": "user", "content": "run"}],
        user_id="user-1",
        thread_id="thread-1",
        remote_context=RemoteCallContext.root(service_id="parent", agent_id="main"),
    )

    assert result.status == "interrupted"
    assert result.result["__interrupt__"][0]["id"] == "interrupt-1"
