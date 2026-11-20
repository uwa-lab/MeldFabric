"""Lazy MCP client construction, discovery, and compiled tool caching."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from strands.tools import ToolProvider
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

from meldfabric.config.models import AgentDefinition, MCPServer, WorkspaceConfig
from meldfabric.domain.names import model_name
from meldfabric.errors import AgentNotReadyError, OptionalDependencyError

if TYPE_CHECKING:
    from strands.tools.mcp import MCPClient

    from meldfabric.storage import SecretReader


@dataclass(frozen=True, slots=True)
class MCPServerStatus:
    server_id: str
    reachable: bool
    tools: tuple[dict[str, Any], ...]
    error: str | None = None


class _AliasedMCPTool(AgentTool):
    """Expose a model-safe name while the MCP tool keeps its wire name."""

    def __init__(self, source: AgentTool, alias: str) -> None:
        super().__init__()
        self.source = source
        self.alias = alias

    @property
    def tool_name(self) -> str:
        return self.alias

    @property
    def tool_spec(self) -> ToolSpec:
        return {**self.source.tool_spec, "name": self.alias}

    @property
    def tool_type(self) -> str:
        return self.source.tool_type

    async def stream(
        self,
        tool_use: ToolUse,
        invocation_state: dict[str, Any],
        **kwargs: Any,
    ) -> ToolGenerator:
        async for event in self.source.stream(tool_use, invocation_state, **kwargs):
            yield event


class _NormalizedMCPProvider(ToolProvider):
    def __init__(self, client: MCPClient) -> None:
        self.client = client
        self.sources: dict[str, str] = {}

    async def load_tools(self, **kwargs: Any) -> list[AgentTool]:
        loaded = await self.client.load_tools(**kwargs)
        result: list[AgentTool] = []
        sources: dict[str, str] = {}
        for item in loaded:
            alias = model_name(item.tool_name)
            if alias in sources:
                raise AgentNotReadyError(
                    "MCP tool names collide after normalization",
                    details={"tool_names": [sources[alias], item.tool_name], "alias": alias},
                )
            sources[alias] = item.tool_name
            result.append(_AliasedMCPTool(item, alias))
        self.sources = sources
        return result

    def add_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.client.add_consumer(consumer_id, **kwargs)

    def remove_consumer(self, consumer_id: Any, **kwargs: Any) -> None:
        self.client.remove_consumer(consumer_id, **kwargs)


class MCPManager:
    def __init__(
        self,
        *,
        secrets_getter: Callable[[int], SecretReader | None],
    ) -> None:
        self._secrets_getter = secrets_getter
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._strands_clients: dict[tuple[int, str], MCPClient] = {}
        self._strands_providers: dict[tuple[int, str], _NormalizedMCPProvider] = {}
        self._strands_tool_names: dict[tuple[int, str], tuple[str, ...]] = {}
        self._strands_tool_sources: dict[tuple[int, str], dict[str, str]] = {}
        self._strands_catalog: dict[tuple[int, str], tuple[dict[str, Any], ...]] = {}

    async def strands_tools_for_agent(
        self,
        config: WorkspaceConfig,
        agent: AgentDefinition,
    ) -> tuple[list[Any], set[str]]:
        """Return native Strands MCP providers and their normalized tool names."""

        servers = {item.id: item for item in config.resources.mcp_servers}
        providers: list[Any] = []
        names: set[str] = set()
        for binding in agent.mcp_bindings:
            if not binding.enabled:
                continue
            server = servers.get(binding.mcp_ref)
            if server is None or not server.enabled:
                raise AgentNotReadyError(
                    f"MCP server {binding.mcp_ref!r} is unavailable",
                    details={"agent_id": agent.id, "mcp_ref": binding.mcp_ref},
                )
            client, server_names = await self._strands_client(
                config.config_generation,
                server,
            )
            duplicates = names.intersection(server_names)
            if duplicates:
                raise AgentNotReadyError(
                    f"duplicate compiled MCP tool names: {sorted(duplicates)}",
                    details={"agent_id": agent.id, "mcp_ref": server.id},
                )
            names.update(server_names)
            providers.append(client)
        return providers, names

    async def _strands_client(
        self,
        revision: int,
        server: MCPServer,
    ) -> tuple[ToolProvider, tuple[str, ...]]:
        key = (revision, server.id)
        if key in self._strands_clients:
            return self._strands_providers[key], self._strands_tool_names[key]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._strands_clients:
                return self._strands_providers[key], self._strands_tool_names[key]
            client = await self._build_strands_client(revision, server)
            provider = _NormalizedMCPProvider(client)
            try:
                loaded = await self._load_tools(provider)
            except Exception:
                await asyncio.to_thread(client.stop, None, None, None)
                raise
            names = tuple(item.tool_name for item in loaded)
            if len(names) != len(set(names)):
                await asyncio.to_thread(client.stop, None, None, None)
                raise AgentNotReadyError(
                    "MCP tool names collide after prefixing",
                    details={"mcp_ref": server.id},
                )
            self._strands_clients[key] = client
            self._strands_providers[key] = provider
            self._strands_tool_names[key] = names
            self._strands_tool_sources[key] = dict(provider.sources)
            self._strands_catalog[key] = tuple(
                {
                    "name": item.tool_name,
                    "description": item.tool_spec.get("description", ""),
                    "args_schema": item.tool_spec.get("inputSchema"),
                }
                for item in loaded
            )
            return provider, names

    async def _build_strands_client(self, revision: int, server: MCPServer) -> MCPClient:
        try:
            from strands.tools.mcp import MCPClient
        except ImportError as exc:
            raise OptionalDependencyError(
                "MCP support is unavailable in the installed Strands runtime; "
                "reinstall meldfabric with its core dependencies"
            ) from exc

        headers = dict(server.headers)
        if server.credential_secret_ref is not None:
            repository = self._secrets_getter(revision)
            if repository is None:
                raise AgentNotReadyError(
                    "the control secret store is not ready, so MCP credentials cannot be loaded",
                    details={"mcp_ref": server.id},
                )
            credential = await repository.get_secret(server.credential_secret_ref)
            if credential is None:
                raise AgentNotReadyError(
                    f"MCP credential {server.credential_secret_ref!r} was not found",
                    details={"mcp_ref": server.id},
                )
            headers[server.credential_header] = (
                f"{server.credential_scheme} {credential}"
                if server.credential_scheme
                else credential
            )
        return MCPClient(
            url=server.url,
            headers=headers or None,
            startup_timeout=max(1, int(server.timeout_seconds)),
            prefix=server.name,
            application_name="meldfabric",
        )

    async def test_server(self, revision: int, server: MCPServer) -> MCPServerStatus:
        client: MCPClient | None = None
        try:
            # Diagnostics are deliberately isolated from the revision-scoped runtime cache.
            client = await self._build_strands_client(revision, server)
            provider = _NormalizedMCPProvider(client)
            loaded = await self._load_tools(provider)
            tools = tuple(
                {
                    "name": item.tool_name,
                    "description": item.tool_spec.get("description", ""),
                    "args_schema": item.tool_spec.get("inputSchema"),
                }
                for item in loaded
            )
        except Exception as exc:
            return MCPServerStatus(
                server_id=server.id,
                reachable=False,
                tools=(),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if client is not None:
                await asyncio.gather(
                    asyncio.to_thread(client.stop, None, None, None),
                    return_exceptions=True,
                )
        return MCPServerStatus(
            server_id=server.id,
            reachable=True,
            tools=tools,
        )

    @staticmethod
    async def _load_tools(provider: _NormalizedMCPProvider) -> list[Any]:
        async def load() -> list[Any]:
            return list(await provider.load_tools())

        return await asyncio.to_thread(lambda: asyncio.run(load()))

    async def prune(self, active_revisions: set[int]) -> None:
        retired: list[MCPClient] = []
        for key in list(self._strands_clients):
            if key[0] not in active_revisions:
                retired.append(self._strands_clients.pop(key))
                self._strands_providers.pop(key, None)
                self._strands_tool_names.pop(key, None)
                self._strands_tool_sources.pop(key, None)
                self._strands_catalog.pop(key, None)
                self._locks.pop(key, None)
        await asyncio.gather(
            *(asyncio.to_thread(client.stop, None, None, None) for client in retired),
            return_exceptions=True,
        )

    async def aclose(self) -> None:
        clients = list(self._strands_clients.values())
        self._strands_clients.clear()
        self._strands_providers.clear()
        self._strands_tool_names.clear()
        self._strands_tool_sources.clear()
        self._strands_catalog.clear()
        self._locks.clear()
        await asyncio.gather(
            *(asyncio.to_thread(client.stop, None, None, None) for client in clients),
            return_exceptions=True,
        )
