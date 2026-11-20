from __future__ import annotations

from types import SimpleNamespace

import pytest

from meldfabric.config.models import MCPServer
from meldfabric.mcp.manager import MCPManager


class FakeMCPClient:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self, *_args) -> None:
        self.stop_calls += 1


@pytest.mark.asyncio
async def test_mcp_diagnostic_is_isolated_from_runtime_cache(monkeypatch) -> None:
    manager = MCPManager(secrets_getter=lambda _revision: None)
    server = MCPServer(id="mcp_test", name="test", url="http://mcp.invalid")
    clients: list[FakeMCPClient] = []

    async def build(_revision, _server):
        client = FakeMCPClient()
        clients.append(client)
        return client

    async def load(_provider):
        return [
            SimpleNamespace(
                tool_name="search",
                tool_spec={"description": "Search", "inputSchema": {"type": "object"}},
            )
        ]

    monkeypatch.setattr(manager, "_build_strands_client", build)
    monkeypatch.setattr(manager, "_load_tools", load)

    await manager._strands_client(1, server)
    runtime_client = clients[0]
    status = await manager.test_server(1, server)

    assert status.reachable is True
    assert runtime_client.stop_calls == 0
    assert clients[1].stop_calls == 1
    await manager.aclose()
    assert runtime_client.stop_calls == 1
