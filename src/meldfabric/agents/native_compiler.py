"""Compile the MeldFabric domain model directly into native Strands executables.

The public domain has three independent layers:

* :class:`AgentDefinition` describes one reusable Strands ``Agent``;
* an orchestration composes Agent definitions as tools, a Swarm, or a Graph;
* a published endpoint selects the executable exposed by the service.

Strands owns messages, snapshots, interrupts, plugins, hooks, and multi-Agent execution.
MeldFabric supplies configuration generations, identity, SQL adapters, resource resolution, and
protocol adapters around those native objects.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Union

import httpx
from pydantic import BaseModel, Field, create_model
from strands import Agent
from strands.agent.conversation_manager import (
    NullConversationManager,
    SlidingWindowConversationManager,
    SummarizingConversationManager,
)
from strands.event_loop._retry import ModelRetryStrategy
from strands.hooks import AgentInitializedEvent, HookProvider, HookRegistry
from strands.session import SnapshotSessionManager

from meldfabric.agents.types import NativeExecutionResult
from meldfabric.config.models import (
    AgentAsToolOrchestration,
    AgentDefinition,
    AgentSkillsPluginResource,
    ContextOffloaderPluginResource,
    GoalLoopPluginResource,
    GraphOrchestration,
    HumanInTheLoopInterventionResource,
    ModelCapabilities,
    PublishedEndpoint,
    RegisteredInterventionResource,
    RegisteredPluginResource,
    SwarmOrchestration,
    ToolResource,
    WorkflowOrchestration,
    WorkspaceConfig,
)
from meldfabric.domain.serialization import jsonable
from meldfabric.errors import (
    AgentDisabledError,
    AgentNotFoundError,
    AgentNotReadyError,
    AgentRunError,
    InterruptResumeError,
    ModelInputUnsupportedError,
    ModelNotConfiguredError,
    ResourceNotFoundError,
)
from meldfabric.identity import UserIdentity
from meldfabric.prompts.renderer import PromptRenderer
from meldfabric.registrations import RegistrationRuntime, extension_close_callback
from meldfabric.remote.models import RemoteCallContext
from meldfabric.sandbox import SandboxContext
from meldfabric.tools import (
    ToolDefinition,
    build_sandboxed_strands_tool,
    build_strands_tool,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from strands.storage import Storage

    from meldfabric.mcp import MCPManager
    from meldfabric.models import ModelAdapterRegistry
    from meldfabric.remote import RemoteAgentManager
    from meldfabric.sandbox import SandboxManager
    from meldfabric.skills import SkillManager
    from meldfabric.storage import SecretReader
    from meldfabric.storage.database import AsyncDatabase

logger = logging.getLogger(__name__)


async def _close_group(callbacks: Any) -> None:
    operations = []
    for callback in callbacks:
        try:
            value = callback()
            operations.append(value if inspect.isawaitable(value) else asyncio.sleep(0))
        except BaseException as exc:  # Cleanup must continue after one failing resource.
            logger.warning(
                "failed to begin closing a native executable resource",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
    if not operations:
        return
    for result in await asyncio.gather(*operations, return_exceptions=True):
        if isinstance(result, BaseException):
            logger.warning(
                "failed to close a native executable resource",
                exc_info=(type(result), result, result.__traceback__),
            )


def _runtime_tool_name(tool: Any) -> str | None:
    name = getattr(tool, "tool_name", None)
    if isinstance(name, str) and name:
        return name
    specification = getattr(tool, "tool_spec", None)
    if isinstance(specification, dict) and isinstance(specification.get("name"), str):
        return specification["name"]
    return None


def _validate_runtime_tool_names(tools: list[Any], plugins: list[Any], *, agent_name: str) -> None:
    owners: dict[str, list[str]] = {}
    for tool in tools:
        if name := _runtime_tool_name(tool):
            owners.setdefault(name, []).append("agent binding")
    for plugin in plugins:
        plugin_name = str(getattr(plugin, "name", type(plugin).__name__))
        for tool in getattr(plugin, "tools", ()) or ():
            if name := _runtime_tool_name(tool):
                owners.setdefault(name, []).append(f"plugin {plugin_name!r}")
    duplicates = {name: sources for name, sources in owners.items() if len(sources) > 1}
    if duplicates:
        raise AgentNotReadyError(
            f"agent {agent_name!r} has duplicate runtime tool names",
            details={"tool_name_conflicts": duplicates},
        )


def _sdk_identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]
    return f"{prefix}_{digest}"


class _RestoreConfiguredPrompt(HookProvider):
    """A persisted snapshot must not override the active configuration prompt."""

    def __init__(self, prompt: str) -> None:
        self.prompt = prompt

    def register_hooks(self, registry: HookRegistry, **_kwargs: Any) -> None:
        registry.add_callback(AgentInitializedEvent, self._restore)

    def _restore(self, event: AgentInitializedEvent) -> None:
        event.agent.system_prompt = self.prompt


@dataclass(slots=True)
class ExecutableInstance:
    root: Any
    primary: Agent
    agents: list[Agent] = field(default_factory=list)
    close_callbacks: list[Callable[[], Awaitable[Any]]] = field(default_factory=list)

    async def close(self) -> None:
        # Preserve lifecycle order: flush Agent-owned state, then close code extensions,
        # finally release orchestration clients and other compiler-owned resources.
        await _close_group(agent.shutdown_async for agent in reversed(self.agents))
        await _close_group(
            callback
            for agent in reversed(self.agents)
            for callback in reversed(getattr(agent, "_meldfabric_extension_closers", ()))
        )
        await _close_group(reversed(self.close_callbacks))


@dataclass(frozen=True, slots=True)
class AgentBlueprint:
    workspace_id: str
    config_generation: int
    timezone: str
    definition: AgentDefinition
    prompt_template: str
    model: Any
    model_capabilities: Any
    tools: tuple[Any, ...]
    plugin_factories: tuple[Callable[..., Any], ...]
    hook_factories: tuple[Callable[..., Any], ...]
    intervention_factories: tuple[Callable[..., Any], ...]
    session_storage: Storage | None
    memory_database: AsyncDatabase | None
    context_offloader_storages: dict[str, Storage]
    sandbox_manager: SandboxManager | None
    sandbox_profile: Any | None
    structured_output_model: type[BaseModel] | None

    async def create(
        self,
        *,
        user_id: str,
        thread_id: str,
        trace_attributes: dict[str, Any] | None = None,
        identity: Any | None = None,
        endpoint_id: str | None = None,
        run_id: str | None = None,
        as_orchestration_node: bool = False,
        extra_tools: list[Any] | None = None,
        prompt_suffix: str | None = None,
    ) -> Agent:
        definition = self.definition
        prompt = PromptRenderer().render(
            self.prompt_template,
            user_id=user_id,
            timezone=self.timezone,
        )
        if prompt_suffix:
            prompt = f"{prompt}\n\n{prompt_suffix}" if prompt else prompt_suffix
        runtime_context = {
            "user_id": user_id,
            "thread_id": thread_id,
            "identity": identity,
            "endpoint_id": endpoint_id,
            "run_id": run_id,
            "trace_attributes": dict(trace_attributes or {}),
        }
        plugins = [
            await _materialize(factory, **runtime_context) for factory in self.plugin_factories
        ]
        hooks: list[Any] = [_RestoreConfiguredPrompt(prompt)]
        hooks.extend(
            [await _materialize(factory, **runtime_context) for factory in self.hook_factories]
        )
        interventions = [
            await _materialize(factory, **runtime_context)
            for factory in self.intervention_factories
        ]
        session_manager = None
        if definition.session_manager is not None and not as_orchestration_node:
            session_manager = SnapshotSessionManager(
                session_id=thread_id,
                storage=self.session_storage,
                save_latest_on=definition.session_manager.save_latest_on,
            )
        memory_manager = self._memory_manager(user_id)
        conversation_manager = (
            self._conversation_manager()
            if definition.context_manager.strategy == "disabled"
            else None
        )
        retry_strategy = self._retry_strategy()
        sandbox = None
        if self.sandbox_profile is not None:
            if self.sandbox_manager is None:
                raise AgentNotReadyError("the configured Strands sandbox adapter is unavailable")
            sandbox = await self.sandbox_manager.create_strands_sandbox(
                self.sandbox_profile,
                context=SandboxContext(
                    workspace_id=self.workspace_id,
                    agent_id=definition.id,
                    user_id=user_id,
                    thread_id=thread_id,
                    config_generation=self.config_generation,
                ),
            )

        _validate_runtime_tool_names(
            [*self.tools, *(extra_tools or [])],
            plugins,
            agent_name=definition.name,
        )

        sdk_agent_id = _sdk_identifier("meldfabric_agent", self.workspace_id, definition.id)

        def construct() -> Agent:
            agent = Agent(
                model=self.model,
                tools=[*self.tools, *(extra_tools or [])],
                system_prompt=prompt,
                structured_output_model=self.structured_output_model,
                structured_output_prompt=definition.structured_output.prompt,
                conversation_manager=conversation_manager,
                agent_id=sdk_agent_id,
                name=definition.name,
                description=definition.description,
                state=dict(definition.initial_state),
                context_manager=(
                    False
                    if definition.context_manager.strategy == "disabled"
                    else definition.context_manager.strategy
                ),
                plugins=plugins,
                hooks=hooks,
                interventions=interventions,
                session_manager=session_manager,
                memory_manager=memory_manager,
                retry_strategy=retry_strategy,
                checkpointing=(
                    bool(definition.session_manager.checkpointing)
                    if definition.session_manager is not None
                    else False
                ),
                sandbox=sandbox,
                storage=None if as_orchestration_node else self.session_storage,
                background_tasks=definition.background_tasks.enabled,
                trace_attributes=trace_attributes,
                callback_handler=None,
            )
            closers = [
                closer
                for value in [*plugins, *hooks, *interventions]
                if (closer := extension_close_callback(value)) is not None
            ]
            vars(agent)["_meldfabric_extension_closers"] = closers
            return agent

        return await asyncio.to_thread(construct)

    def _conversation_manager(self) -> Any:
        config = self.definition.conversation_manager
        compression = True if config.proactive_compression else None
        if config.strategy == "none":
            return NullConversationManager(proactive_compression=compression)
        if config.strategy == "summarizing":
            return SummarizingConversationManager(
                summary_ratio=config.summary_ratio,
                preserve_recent_messages=config.preserve_recent_messages,
                summarization_system_prompt=config.summarization_system_prompt,
                pin_first=config.pin_first,
                proactive_compression=compression,
            )
        return SlidingWindowConversationManager(
            window_size=config.window_size,
            should_truncate_results=config.truncate_tool_results,
            per_turn=config.per_turn,
            pin_first=config.pin_first,
            proactive_compression=compression,
        )

    def _retry_strategy(self) -> Any:
        config = self.definition.retry_strategy
        if not config.enabled:
            return None
        return ModelRetryStrategy(
            max_attempts=config.max_attempts,
            initial_delay=int(config.initial_delay_seconds),
            max_delay=int(config.max_delay_seconds),
        )

    def _memory_manager(self, user_id: str) -> Any:
        config = self.definition.memory_manager
        if config is None or self.memory_database is None:
            return None
        from strands.memory import MemoryManager

        from meldfabric.storage import SQLMemoryStore, memory_namespace

        namespace = config.namespace or "default"
        scope = "\x1f".join((self.workspace_id, self.definition.id, namespace, user_id))
        store = SQLMemoryStore(
            self.memory_database,
            namespace=memory_namespace(self.workspace_id, self.definition.id, namespace),
            scope_key=scope,
            schema_ready=True,
        )
        return MemoryManager(
            stores=[store],
            search_tool_config=config.search_tool,
            add_tool_config=config.add_tool,
            injection=config.injection,
        )


@dataclass(frozen=True, slots=True)
class ExecutableBlueprint:
    executable_id: str
    kind: Literal["agent", "agent_as_tool", "swarm", "graph", "workflow"]
    create_instance: Callable[..., Awaitable[ExecutableInstance]]

    async def create(self, **kwargs: Any) -> ExecutableInstance:
        return await self.create_instance(**kwargs)


@dataclass(frozen=True, slots=True)
class StrandsCompiledExecutable:
    endpoint_id: str
    primary_agent_id: str
    config_generation: int
    blueprint: ExecutableBlueprint
    model_capabilities: Any

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        *,
        user_id: str,
        thread_id: str,
        identity: UserIdentity | None = None,
        remote_context: RemoteCallContext | None = None,
    ) -> NativeExecutionResult:
        completed: NativeExecutionResult | None = None
        async for event in self.stream(
            messages,
            user_id=user_id,
            thread_id=thread_id,
            identity=identity,
            remote_context=remote_context,
        ):
            if isinstance(event, NativeExecutionResult):
                completed = event
        if completed is None:
            raise AgentRunError("Strands execution finished without a result")
        return completed

    async def stream(
        self,
        runtime_input: Any,
        *,
        user_id: str,
        thread_id: str,
        remote_context: RemoteCallContext | None = None,
        metadata: dict[str, Any] | None = None,
        trace_attributes: dict[str, Any] | None = None,
        identity: UserIdentity | None = None,
        endpoint_id: str | None = None,
        run_id: str | None = None,
    ):
        instance = await self.blueprint.create(
            user_id=user_id,
            thread_id=thread_id,
            trace_attributes=trace_attributes,
            identity=identity,
            endpoint_id=endpoint_id or self.endpoint_id,
            run_id=run_id,
        )
        prompt = self._prompt(instance.primary, runtime_input)
        if not isinstance(instance.root, Agent):
            prompt = self._multiagent_input(prompt)
        invocation_state = {
            "meldfabric_internal_thread_id": thread_id,
            "meldfabric_user_id": user_id,
            "meldfabric_remote_context": (
                remote_context.model_dump(mode="json") if remote_context is not None else None
            ),
            "metadata": dict(metadata or {}),
        }
        result = None
        try:
            async for event in instance.root.stream_async(
                prompt,
                invocation_state=invocation_state,
            ):
                if isinstance(event, dict) and event.get("result") is not None:
                    result = event["result"]
                yield event
            if result is None:
                raise AgentRunError("Strands execution stream did not provide an AgentResult")
            interrupts = [
                item.to_dict() if hasattr(item, "to_dict") else jsonable(item)
                for item in (getattr(result, "interrupts", ()) or ())
            ]
            serialized_messages = [jsonable(message) for message in instance.primary.messages]
            if not all(isinstance(message, dict) for message in serialized_messages):
                raise AgentRunError("Strands returned a message that cannot be serialized")
            messages = [dict(message) for message in serialized_messages]
            if not isinstance(instance.root, Agent) and not interrupts:
                messages = [{"role": "assistant", "content": [{"text": str(result)}]}]
            yield NativeExecutionResult(
                executable_id=self.blueprint.executable_id,
                output=str(result),
                messages=messages,
                state=dict(instance.primary.state.get()),
                stop_reason=getattr(result, "stop_reason", None),
                interrupts=interrupts,
                structured_output=getattr(result, "structured_output", None),
                metrics=getattr(result, "metrics", None),
            )
        finally:
            await instance.close()

    async def resume_input(
        self,
        entries: list[dict[str, Any]],
        *,
        user_id: str,
        thread_id: str,
    ) -> list[dict[str, Any]]:
        del user_id, thread_id
        responses = []
        seen: set[str] = set()
        for entry in entries:
            interrupt_id = str(entry.get("interrupt_id") or "")
            if not interrupt_id or interrupt_id in seen:
                raise InterruptResumeError("interrupt responses require unique interrupt ids")
            seen.add(interrupt_id)
            payload = entry.get("payload")
            if entry.get("status") == "cancelled":
                payload = False
            if payload is None:
                raise InterruptResumeError("interrupt response payload cannot be null")
            responses.append(
                {
                    "interruptResponse": {
                        "interruptId": interrupt_id,
                        "response": payload,
                    }
                }
            )
        if not responses:
            raise InterruptResumeError("at least one interrupt response is required")
        return responses

    def _prompt(self, agent: Agent, runtime_input: Any) -> list[dict[str, Any]]:
        if isinstance(runtime_input, list) and all(
            isinstance(item, dict) and "interruptResponse" in item for item in runtime_input
        ):
            return runtime_input
        raw_messages = (
            runtime_input.get("messages", []) if isinstance(runtime_input, dict) else runtime_input
        )
        if not isinstance(raw_messages, list):
            raise AgentRunError("Agent input messages must be a list")
        seen = set(agent.state.get("meldfabric_seen_message_ids") or [])
        accepted_ids: list[str] = []
        normalized: list[dict[str, Any]] = []
        for raw in raw_messages:
            if not isinstance(raw, dict):
                continue
            message_id = str(raw.get("id") or "")
            if message_id and message_id in seen:
                continue
            role = str(raw.get("role") or "user")
            if role not in {"user", "assistant"}:
                continue
            normalized.append(
                {
                    "role": role,
                    "content": self._content_blocks(raw.get("content", "")),
                }
            )
            if message_id:
                accepted_ids.append(message_id)
        if agent.messages and not accepted_ids and normalized:
            normalized = [
                next(
                    (item for item in reversed(normalized) if item["role"] == "user"),
                    normalized[-1],
                )
            ]
        if accepted_ids:
            agent.state.set("meldfabric_seen_message_ids", [*seen, *accepted_ids][-10000:])
        if not normalized:
            raise AgentRunError(
                "the request contains no new user or assistant message",
                details={"reason": "NO_NEW_MESSAGE"},
            )
        return normalized

    @staticmethod
    def _multiagent_input(prompt: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if all("interruptResponse" in item for item in prompt):
            return prompt
        latest = next((item for item in reversed(prompt) if item.get("role") == "user"), None)
        if latest is None or not latest.get("content"):
            raise AgentRunError("a multi-Agent execution requires a new user message")
        return latest["content"]

    def _content_blocks(self, content: Any) -> list[dict[str, Any]]:
        capabilities = self.model_capabilities
        if isinstance(content, str):
            return [{"text": content}]
        result: list[dict[str, Any]] = []
        for raw in content if isinstance(content, list) else [content]:
            if isinstance(raw, str):
                result.append({"text": raw})
                continue
            if not isinstance(raw, dict):
                result.append({"text": str(raw)})
                continue
            block_type = raw.get("type")
            if block_type in {"text", "input_text", "output_text"} or (
                "text" in raw and block_type is None
            ):
                result.append({"text": str(raw.get("text", ""))})
            elif "image" in raw or block_type in {"image_url", "input_image"}:
                _require_capability(capabilities.image_inputs, "image")
                value = raw.get("image") or raw.get("image_url") or raw.get("url")
                if isinstance(value, dict):
                    value = value.get("url")
                result.append({"image": _data_media(value, kind="image")})
            elif "document" in raw or block_type in {"file", "input_file"}:
                _require_capability(capabilities.pdf_inputs, "document")
                if "document" in raw:
                    result.append({"document": raw["document"]})
                else:
                    file_value = raw.get("file") or raw
                    data = file_value.get("file_data") if isinstance(file_value, dict) else None
                    document = _data_media(data, kind="document")
                    document["name"] = str(file_value.get("filename", "document"))
                    result.append({"document": document})
            elif "audio" in raw or block_type in {"input_audio", "audio"}:
                _require_capability(capabilities.audio_inputs, "audio")
                audio = raw.get("audio") or raw.get("input_audio")
                if isinstance(audio, dict) and "source" in audio:
                    result.append({"audio": audio})
                elif isinstance(audio, dict) and isinstance(audio.get("data"), str):
                    result.append(
                        {
                            "audio": {
                                "format": str(audio.get("format") or "wav"),
                                "source": {"bytes": base64.b64decode(audio["data"])},
                            }
                        }
                    )
                else:
                    raise ModelInputUnsupportedError("audio input has an invalid shape")
            elif "video" in raw:
                _require_capability(capabilities.video_inputs, "video")
                result.append({"video": raw["video"]})
            else:
                raise ModelInputUnsupportedError(
                    "the request contains an unsupported content block",
                    details={"content_type": str(block_type or next(iter(raw), "unknown"))},
                )
        return result


class NativeStrandsCompiler:
    def __init__(
        self,
        *,
        model_adapters: ModelAdapterRegistry,
        secrets: SecretReader | None,
        strands_storages: Mapping[str, Storage] | None,
        strands_databases: dict[str, AsyncDatabase] | None,
        registration_runtime: RegistrationRuntime,
        mcp_manager: MCPManager,
        remote_agent_manager: RemoteAgentManager,
        skill_manager: SkillManager,
        sandbox_manager: SandboxManager,
        tool_definitions: dict[str, ToolDefinition] | None = None,
    ) -> None:
        self.model_adapters = model_adapters
        self.secrets = secrets
        self.strands_storages = strands_storages or {}
        self.strands_databases = strands_databases or {}
        self.registrations = registration_runtime
        self.mcp_manager = mcp_manager
        self.remote_agent_manager = remote_agent_manager
        self.skill_manager = skill_manager
        self.sandbox_manager = sandbox_manager
        self.tool_definitions = tool_definitions or {}
        self._agents: dict[str, AgentBlueprint] = {}

    async def compile_endpoint(
        self,
        config: WorkspaceConfig,
        endpoint_id: str,
    ) -> StrandsCompiledExecutable:
        endpoint = next((item for item in config.endpoints if item.id == endpoint_id), None)
        if endpoint is None:
            raise AgentNotFoundError(f"published endpoint {endpoint_id!r} does not exist")
        if not endpoint.enabled:
            raise AgentDisabledError(f"published endpoint {endpoint_id!r} is disabled")
        blueprint = await self._compile_executable(config, endpoint)
        capabilities = await self._primary_capabilities(config, endpoint)
        return StrandsCompiledExecutable(
            endpoint_id=endpoint.id,
            primary_agent_id=self._primary_agent_ref(config, endpoint),
            config_generation=config.config_generation,
            blueprint=blueprint,
            model_capabilities=capabilities,
        )

    @staticmethod
    def _primary_agent_ref(config: WorkspaceConfig, endpoint: PublishedEndpoint) -> str:
        if endpoint.target_type == "agent":
            return endpoint.target_ref
        orchestration = next(
            item for item in config.orchestrations if item.id == endpoint.target_ref
        )
        if isinstance(orchestration, AgentAsToolOrchestration):
            return orchestration.coordinator_ref
        if isinstance(orchestration, SwarmOrchestration):
            return orchestration.entry_agent_ref or orchestration.agent_refs[0]
        if isinstance(orchestration, GraphOrchestration):
            entry = next(
                node for node in orchestration.nodes if node.id == orchestration.primary_entry_point
            )
            if entry.target_type != "agent":
                raise AgentNotReadyError(
                    "a published Graph must have a local Agent as its primary entry point"
                )
            return entry.target_ref
        if isinstance(orchestration, WorkflowOrchestration):
            return next(
                task.agent_ref
                for task in orchestration.tasks
                if task.id == orchestration.primary_task_ref
            )
        raise AgentNotReadyError("the endpoint has no primary Agent")

    async def _compile_executable(
        self,
        config: WorkspaceConfig,
        endpoint: PublishedEndpoint,
    ) -> ExecutableBlueprint:
        if endpoint.target_type == "agent":
            blueprint = await self._agent(config, endpoint.target_ref)

            async def create_agent(**runtime: Any) -> ExecutableInstance:
                agent = await blueprint.create(**runtime)
                return ExecutableInstance(root=agent, primary=agent, agents=[agent])

            return ExecutableBlueprint(endpoint.target_ref, "agent", create_agent)
        orchestration = next(
            (item for item in config.orchestrations if item.id == endpoint.target_ref),
            None,
        )
        if orchestration is None or not orchestration.enabled:
            raise AgentNotReadyError(f"orchestration {endpoint.target_ref!r} is unavailable")
        if isinstance(orchestration, AgentAsToolOrchestration):
            return await self._agent_as_tool(config, orchestration)
        if isinstance(orchestration, SwarmOrchestration):
            return await self._swarm(config, orchestration)
        if isinstance(orchestration, GraphOrchestration):
            return await self._graph(config, orchestration)
        if isinstance(orchestration, WorkflowOrchestration):
            return await self._workflow(config, orchestration)
        raise AgentNotReadyError(f"unsupported orchestration {orchestration.kind!r}")

    async def _agent_as_tool(
        self,
        config: WorkspaceConfig,
        orchestration: AgentAsToolOrchestration,
    ) -> ExecutableBlueprint:
        coordinator = await self._agent(config, orchestration.coordinator_ref)
        members = [
            (member, await self._agent(config, member.agent_ref))
            for member in orchestration.members
        ]

        async def create(**runtime: Any) -> ExecutableInstance:
            owned: list[Agent] = []
            tools: list[Any] = []
            for member, blueprint in members:
                member_agent = await blueprint.create(
                    user_id=runtime["user_id"],
                    thread_id=_sdk_identifier(
                        "fmt",
                        runtime["thread_id"],
                        orchestration.id,
                        member.id,
                    ),
                    trace_attributes=runtime.get("trace_attributes"),
                )
                owned.append(member_agent)
                tools.append(
                    member_agent.as_tool(
                        name=member.name or member_agent.name,
                        description=member.description or member_agent.description,
                        preserve_context=member.preserve_context,
                    )
                )
            primary = await coordinator.create(extra_tools=tools, **runtime)
            owned.append(primary)
            return ExecutableInstance(root=primary, primary=primary, agents=owned)

        return ExecutableBlueprint(orchestration.id, "agent_as_tool", create)

    async def _swarm(
        self,
        config: WorkspaceConfig,
        orchestration: SwarmOrchestration,
    ) -> ExecutableBlueprint:
        blueprints = [
            await self._agent(config, reference) for reference in orchestration.agent_refs
        ]
        storage = self._orchestration_storage(orchestration)

        async def create(**runtime: Any) -> ExecutableInstance:
            from strands.multiagent import Swarm

            agents = [
                await blueprint.create(as_orchestration_node=True, **runtime)
                for blueprint in blueprints
            ]
            entry = (
                next(
                    agent
                    for agent, reference in zip(agents, orchestration.agent_refs, strict=True)
                    if reference == orchestration.entry_agent_ref
                )
                if orchestration.entry_agent_ref
                else agents[0]
            )
            session = (
                SnapshotSessionManager(
                    session_id=runtime["thread_id"],
                    storage=storage,
                    save_latest_on=orchestration.session_manager.save_latest_on,
                )
                if orchestration.session_manager is not None
                else None
            )
            root = Swarm(
                agents,
                entry_point=entry,
                max_handoffs=orchestration.max_handoffs,
                max_iterations=orchestration.max_iterations,
                execution_timeout=orchestration.execution_timeout_seconds,
                node_timeout=orchestration.node_timeout_seconds,
                session_manager=session,
                id=_sdk_identifier("meldfabric_swarm", config.workspace.id, orchestration.id),
                trace_attributes=runtime.get("trace_attributes"),
            )
            return ExecutableInstance(root=root, primary=entry, agents=agents)

        return ExecutableBlueprint(orchestration.id, "swarm", create)

    async def _graph(
        self,
        config: WorkspaceConfig,
        orchestration: GraphOrchestration,
    ) -> ExecutableBlueprint:
        local_nodes = {
            node.id: await self._agent(config, node.target_ref)
            for node in orchestration.nodes
            if node.target_type == "agent"
        }
        remote_resources = {
            node.id: next(
                item for item in config.resources.remote_agents if item.id == node.target_ref
            )
            for node in orchestration.nodes
            if node.target_type == "remote_agent"
        }
        storage = self._orchestration_storage(orchestration)

        async def create(**runtime: Any) -> ExecutableInstance:
            from a2a.client import ClientConfig
            from strands.agent.a2a_agent import A2AAgent
            from strands.multiagent import GraphBuilder

            agents: list[Agent] = []
            remote_clients: list[httpx.AsyncClient] = []
            graph_nodes: dict[str, Any] = {}
            builder = GraphBuilder()
            try:
                for node in orchestration.nodes:
                    if node.target_type == "agent":
                        graph_agent: Any = await local_nodes[node.id].create(
                            as_orchestration_node=True, **runtime
                        )
                        agents.append(graph_agent)
                    else:
                        resource = remote_resources[node.id]
                        if "a2a/0.3" not in resource.protocols:
                            raise AgentNotReadyError(
                                f"remote Agent {resource.id!r} does not advertise native A2A"
                            )
                        if resource.credential_secret_ref is None or self.secrets is None:
                            raise AgentNotReadyError(
                                f"remote Agent {resource.id!r} has no A2A credential configured"
                            )
                        credential = await self.secrets.get_secret(resource.credential_secret_ref)
                        if credential is None:
                            raise AgentNotReadyError(
                                f"remote Agent credential for {resource.id!r} was not found"
                            )
                        client = httpx.AsyncClient(
                            headers={"Authorization": f"Bearer {credential}"},
                            timeout=resource.timeout_seconds,
                        )
                        remote_clients.append(client)
                        graph_agent = A2AAgent(
                            resource.base_url,
                            name=resource.name,
                            description=resource.description,
                            timeout=max(1, int(resource.timeout_seconds)),
                            client_config=ClientConfig(httpx_client=client, streaming=True),
                        )
                    graph_nodes[node.id] = graph_agent
                    builder.add_node(graph_agent, node_id=node.id)
                for edge in orchestration.edges:
                    condition = None
                    if edge.condition_registration_id:
                        condition = self.registrations.build_graph_condition(
                            edge.condition_registration_id,
                            edge=edge,
                            orchestration=orchestration,
                            workspace=config,
                        )
                    builder.add_edge(edge.source, edge.target, condition=condition)
                for entry in orchestration.entry_points:
                    builder.set_entry_point(entry)
                builder.set_max_node_executions(orchestration.max_node_executions)
                builder.set_graph_id(
                    _sdk_identifier("meldfabric_graph", config.workspace.id, orchestration.id)
                )
                if orchestration.session_manager is not None:
                    builder.set_session_manager(
                        SnapshotSessionManager(
                            session_id=runtime["thread_id"],
                            storage=storage,
                            save_latest_on=orchestration.session_manager.save_latest_on,
                        )
                    )
                root = builder.build()
                primary_node = next(
                    node
                    for node in orchestration.nodes
                    if node.id == orchestration.primary_entry_point
                )
                primary = graph_nodes[primary_node.id]
                if not isinstance(primary, Agent):
                    raise AgentNotReadyError("a published Graph must start with a local Agent")
                return ExecutableInstance(
                    root=root,
                    primary=primary,
                    agents=agents,
                    close_callbacks=[client.aclose for client in remote_clients],
                )
            except BaseException:
                await asyncio.gather(
                    *(agent.shutdown_async() for agent in reversed(agents)),
                    return_exceptions=True,
                )
                await asyncio.gather(
                    *(client.aclose() for client in reversed(remote_clients)),
                    return_exceptions=True,
                )
                raise

        return ExecutableBlueprint(orchestration.id, "graph", create)

    async def _workflow(
        self,
        config: WorkspaceConfig,
        orchestration: WorkflowOrchestration,
    ) -> ExecutableBlueprint:
        task_agents = {
            task.id: await self._agent(config, task.agent_ref) for task in orchestration.tasks
        }

        async def create(**runtime: Any) -> ExecutableInstance:
            from strands.multiagent import GraphBuilder

            agents: list[Agent] = []
            graph_agents: dict[str, Agent] = {}
            builder = GraphBuilder()
            for task in orchestration.tasks:
                agent = await task_agents[task.id].create(
                    as_orchestration_node=True,
                    prompt_suffix=f"Workflow task: {task.instruction}",
                    **runtime,
                )
                agents.append(agent)
                graph_agents[task.id] = agent
                builder.add_node(agent, node_id=task.id)
            for task in orchestration.tasks:
                for dependency in task.dependency_refs:
                    builder.add_edge(dependency, task.id)
            builder.set_max_node_executions(len(orchestration.tasks))
            builder.set_graph_id(
                _sdk_identifier("meldfabric_workflow", config.workspace.id, orchestration.id)
            )
            root = builder.build()
            root_task = next(
                task for task in orchestration.tasks if task.id == orchestration.primary_task_ref
            )
            return ExecutableInstance(
                root=root,
                primary=graph_agents[root_task.id],
                agents=agents,
            )

        return ExecutableBlueprint(orchestration.id, "workflow", create)

    async def _agent(self, config: WorkspaceConfig, agent_id: str) -> AgentBlueprint:
        existing = self._agents.get(agent_id)
        if existing is not None:
            return existing
        definition = next((item for item in config.agents if item.id == agent_id), None)
        if definition is None or not definition.enabled:
            raise AgentNotReadyError(f"Agent definition {agent_id!r} is unavailable")
        model, model_resource = await self._resolve_model(config, definition)
        tools_enabled = model_resource.capabilities.tool_calling is not False
        tools = await self._compile_tools(config, definition) if tools_enabled else []
        session_storage = (
            self._storage(definition.session_manager.database_ref)
            if definition.session_manager is not None
            else None
        )
        memory_database = (
            self._database(definition.memory_manager.database_ref)
            if definition.memory_manager is not None
            else None
        )
        plugins, offload_storages = self._plugin_factories(config, definition)
        hooks = self._hook_factories(config, definition)
        interventions = self._intervention_factories(config, definition)
        sandbox_profile = (
            next(
                (
                    item
                    for item in config.resources.sandbox_profiles
                    if item.id == definition.sandbox_ref and item.enabled
                ),
                None,
            )
            if definition.sandbox_ref
            else None
        )
        if definition.sandbox_ref and sandbox_profile is None:
            raise AgentNotReadyError(f"sandbox {definition.sandbox_ref!r} is unavailable")
        blueprint = AgentBlueprint(
            workspace_id=config.workspace.id,
            config_generation=config.config_generation,
            timezone=config.workspace.timezone,
            definition=definition,
            prompt_template=self._resolve_prompt(definition),
            model=model,
            model_capabilities=model_resource.capabilities,
            tools=tuple(tools),
            plugin_factories=tuple(plugins),
            hook_factories=tuple(hooks),
            intervention_factories=tuple(interventions),
            session_storage=session_storage,
            memory_database=memory_database,
            context_offloader_storages=offload_storages,
            sandbox_manager=self.sandbox_manager,
            sandbox_profile=sandbox_profile,
            structured_output_model=_structured_output_model(definition),
        )
        self._agents[agent_id] = blueprint
        return blueprint

    async def _resolve_model(self, config: WorkspaceConfig, agent: AgentDefinition):
        if agent.model_ref is None:
            raise ModelNotConfiguredError(f"Agent {agent.id!r} has no model configured")
        model = next((item for item in config.resources.models if item.id == agent.model_ref), None)
        if model is None or not model.enabled:
            raise ModelNotConfiguredError(f"model resource {agent.model_ref!r} is unavailable")
        provider = next(
            (item for item in config.resources.model_providers if item.id == model.provider_ref),
            None,
        )
        if provider is None or not provider.enabled:
            raise ModelNotConfiguredError(f"model provider {model.provider_ref!r} is unavailable")
        return await self.model_adapters.build_strands(provider, model, secrets=self.secrets), model

    async def _compile_tools(
        self,
        config: WorkspaceConfig,
        agent: AgentDefinition,
    ) -> list[Any]:
        resources = {item.id: item for item in config.resources.tools}
        for registered_definition in self.tool_definitions.values():
            resources.setdefault(
                registered_definition.id,
                ToolResource(
                    id=registered_definition.id,
                    name=registered_definition.name,
                    display_name=registered_definition.display_name,
                    description=registered_definition.description,
                    python_path=registered_definition.python_path,
                    requires_sandbox=registered_definition.requires_sandbox,
                ),
            )
        compiled: list[Any] = []
        names: set[str] = set()
        for binding in agent.tool_bindings:
            if not binding.enabled:
                continue
            resource = resources.get(binding.tool_ref)
            tool_definition = self.tool_definitions.get(binding.tool_ref)
            if resource is None or not resource.enabled or tool_definition is None:
                raise AgentNotReadyError(f"tool {binding.tool_ref!r} is unavailable")
            if resource.name in names:
                raise AgentNotReadyError(f"duplicate tool name {resource.name!r}")
            names.add(resource.name)
            if resource.requires_sandbox or tool_definition.requires_sandbox:
                compiled.append(
                    await self._sandboxed_tool(config, agent, resource, tool_definition)
                )
            else:
                compiled.append(
                    build_strands_tool(
                        tool_definition,
                        name=resource.name,
                        description=resource.description or tool_definition.description,
                        parameter_descriptions=resource.parameter_descriptions,
                    )
                )
        if agent.mcp_bindings:
            providers, mcp_names = await self.mcp_manager.strands_tools_for_agent(config, agent)
            if duplicates := names.intersection(mcp_names):
                raise AgentNotReadyError(f"duplicate MCP tool names: {sorted(duplicates)}")
            names.update(mcp_names)
            compiled.extend(providers)
        if agent.remote_tool_bindings:
            remote_tools = await self.remote_agent_manager.strands_tools_for_agent(config, agent)
            remote_names = {item.tool_name for item in remote_tools}
            if duplicates := names.intersection(remote_names):
                raise AgentNotReadyError(f"duplicate remote Agent tools: {sorted(duplicates)}")
            compiled.extend(remote_tools)
        return compiled

    async def _sandboxed_tool(self, config, agent, resource, definition):
        if agent.sandbox_ref is None:
            raise AgentNotReadyError(f"tool {resource.id!r} requires a sandbox")
        profile = next(
            (item for item in config.resources.sandbox_profiles if item.id == agent.sandbox_ref),
            None,
        )
        if profile is None or not profile.enabled:
            raise AgentNotReadyError(f"sandbox {agent.sandbox_ref!r} is unavailable")
        await self.sandbox_manager.ensure_available(profile, revision=config.config_generation)

        async def execute(arguments: dict[str, Any], run_context: dict[str, Any]) -> Any:
            configurable = run_context.get("configurable", {})
            return await self.sandbox_manager.execute_tool(
                context=SandboxContext(
                    workspace_id=config.workspace.id,
                    agent_id=agent.id,
                    user_id=str(configurable.get("meldfabric_user_id") or "anonymous"),
                    thread_id=str(configurable.get("meldfabric_internal_thread_id") or "unscoped"),
                    config_generation=config.config_generation,
                ),
                profile=profile,
                python_path=definition.python_path,
                arguments=arguments,
            )

        return build_sandboxed_strands_tool(
            definition,
            name=resource.name,
            description=resource.description or definition.description,
            parameter_descriptions=resource.parameter_descriptions,
            executor=execute,
        )

    def _plugin_factories(
        self,
        config: WorkspaceConfig,
        agent: AgentDefinition,
    ) -> tuple[list[Callable[..., Any]], dict[str, Storage]]:
        resources = {item.id: item for item in config.resources.plugins}
        factories: list[Callable[..., Any]] = []
        storages: dict[str, Storage] = {}
        for binding in sorted(agent.plugin_bindings, key=lambda item: item.order):
            if not binding.enabled:
                continue
            resource = resources.get(binding.plugin_ref)
            if resource is None or not resource.enabled:
                raise AgentNotReadyError(f"plugin {binding.plugin_ref!r} is unavailable")
            if isinstance(resource, AgentSkillsPluginResource):
                paths = [
                    path
                    for _virtual, path, _source in self.skill_manager.runtime_sources(
                        config,
                        set(resource.skill_source_refs),
                    )
                ]

                strict = resource.strict

                def skills_factory(*, paths=paths, strict=strict, **_runtime: Any):
                    from strands.vended_plugins.skills import AgentSkills

                    return AgentSkills(paths, strict=strict)

                factories.append(skills_factory)
            elif isinstance(resource, ContextOffloaderPluginResource):
                storage = self._storage(resource.database_ref)
                storages[resource.id] = storage

                def offloader_factory(
                    *,
                    storage=storage,
                    resource=resource,
                    **_runtime: Any,
                ):
                    from strands.vended_plugins.context_offloader import ContextOffloader

                    return ContextOffloader(
                        storage=storage,
                        max_result_tokens=resource.max_result_tokens,
                        preview_tokens=resource.preview_tokens,
                        include_retrieval_tool=resource.include_retrieval_tool,
                    )

                factories.append(offloader_factory)
            elif isinstance(resource, GoalLoopPluginResource):

                def goal_factory(*, resource=resource, **_runtime: Any):
                    from strands.vended_plugins.goal import GoalLoop

                    return GoalLoop(
                        goal=resource.goal,
                        max_attempts=resource.max_attempts,
                        timeout=resource.timeout_seconds,
                        preserve_context=resource.preserve_context,
                    )

                factories.append(goal_factory)
            elif isinstance(resource, RegisteredPluginResource):
                definition = self.registrations.get("plugin", resource.registration_id)
                if definition is None:
                    raise AgentNotReadyError(
                        f"plugin registration {resource.registration_id!r} is not registered"
                    )

                def registered_factory(
                    *,
                    resource=resource,
                    **runtime: Any,
                ):
                    return self.registrations.build_plugin(
                        resource.registration_id,
                        config=dict(resource.config),
                        resource=resource,
                        agent=agent,
                        workspace=config,
                        runtime=runtime,
                    )

                factories.append(registered_factory)
        return factories, storages

    def _hook_factories(self, config: WorkspaceConfig, agent: AgentDefinition):
        resources = {item.id: item for item in config.resources.hooks}
        result = []
        for binding in sorted(agent.hook_bindings, key=lambda item: item.order):
            if not binding.enabled:
                continue
            resource = resources.get(binding.hook_ref)
            if resource is None or not resource.enabled:
                raise AgentNotReadyError(f"hook {binding.hook_ref!r} is unavailable")
            definition = self.registrations.get("hook", resource.registration_id)
            if definition is None:
                raise AgentNotReadyError(
                    f"hook registration {resource.registration_id!r} is not registered"
                )
            result.append(
                lambda *, resource=resource, **runtime: self.registrations.build_hook(
                    resource.registration_id,
                    config=dict(resource.config),
                    resource=resource,
                    agent=agent,
                    workspace=config,
                    runtime=runtime,
                )
            )
        return result

    def _intervention_factories(self, config: WorkspaceConfig, agent: AgentDefinition):
        resources = {item.id: item for item in config.resources.interventions}
        tools = {item.id: item for item in config.resources.tools}
        for definition in self.tool_definitions.values():
            tools.setdefault(
                definition.id,
                ToolResource(
                    id=definition.id,
                    name=definition.name,
                    display_name=definition.display_name,
                    description=definition.description,
                    python_path=definition.python_path,
                    requires_sandbox=definition.requires_sandbox,
                ),
            )
        result = []
        for binding in sorted(agent.intervention_bindings, key=lambda item: item.order):
            if not binding.enabled:
                continue
            resource = resources.get(binding.intervention_ref)
            if resource is None or not resource.enabled:
                raise AgentNotReadyError(
                    f"intervention {binding.intervention_ref!r} is unavailable"
                )
            if isinstance(resource, HumanInTheLoopInterventionResource):
                allowed = [tools[reference].name for reference in resource.tool_refs]
                allowed.extend(resource.mcp_tool_names)

                def hitl_factory(*, allowed=allowed, resource=resource, **_runtime: Any):
                    from strands.vended_interventions.hitl import HumanInTheLoop

                    return HumanInTheLoop(
                        allowed_tools=allowed or None,
                        enable_trust=resource.enable_trust,
                    )

                result.append(hitl_factory)
            elif isinstance(resource, RegisteredInterventionResource):
                definition = self.registrations.get("intervention", resource.registration_id)
                if definition is None:
                    raise AgentNotReadyError(
                        f"intervention registration {resource.registration_id!r} is not registered"
                    )
                result.append(
                    lambda *, resource=resource, **runtime: self.registrations.build_intervention(
                        resource.registration_id,
                        config=dict(resource.config),
                        resource=resource,
                        agent=agent,
                        workspace=config,
                        runtime=runtime,
                        on_error=resource.on_error,
                    )
                )
        return result

    def _orchestration_storage(self, orchestration: Any) -> Storage | None:
        session = getattr(orchestration, "session_manager", None)
        return self._storage(session.database_ref) if session is not None else None

    def _storage(self, reference: str) -> Storage:
        try:
            return self.strands_storages[reference]
        except KeyError as exc:
            raise AgentNotReadyError(f"Strands storage {reference!r} is unavailable") from exc

    def _database(self, reference: str) -> AsyncDatabase:
        try:
            return self.strands_databases[reference]
        except KeyError as exc:
            raise AgentNotReadyError(
                f"Strands memory database {reference!r} is unavailable"
            ) from exc

    async def _primary_capabilities(self, config: WorkspaceConfig, endpoint: PublishedEndpoint):
        if endpoint.target_type == "agent":
            return (await self._agent(config, endpoint.target_ref)).model_capabilities
        orchestration = next(
            item for item in config.orchestrations if item.id == endpoint.target_ref
        )
        if isinstance(orchestration, AgentAsToolOrchestration):
            reference = orchestration.coordinator_ref
        elif isinstance(orchestration, SwarmOrchestration):
            reference = orchestration.entry_agent_ref or orchestration.agent_refs[0]
        elif isinstance(orchestration, GraphOrchestration):
            references = [
                node.target_ref
                for node in orchestration.nodes
                if node.id in orchestration.entry_points and node.target_type == "agent"
            ]
            capabilities = [
                (await self._agent(config, reference)).model_capabilities
                for reference in references
            ]
            return _shared_capabilities(capabilities)
        elif isinstance(orchestration, WorkflowOrchestration):
            references = [
                task.agent_ref for task in orchestration.tasks if not task.dependency_refs
            ]
            capabilities = [
                (await self._agent(config, reference)).model_capabilities
                for reference in references
            ]
            return _shared_capabilities(capabilities)
        else:
            raise AgentNotReadyError(f"unsupported orchestration {orchestration.kind!r}")
        return (await self._agent(config, reference)).model_capabilities

    @staticmethod
    def _resolve_prompt(agent: AgentDefinition) -> str:
        for version in agent.prompt.versions:
            if version.version == agent.prompt.selected_version:
                return version.content
        raise ResourceNotFoundError(
            f"Agent Prompt does not contain version {agent.prompt.selected_version!r}"
        )


async def _materialize(factory: Callable[..., Any], **runtime: Any) -> Any:
    value = factory(**runtime)
    return await value if inspect.isawaitable(value) else value


def _structured_output_model(agent: AgentDefinition) -> type[BaseModel] | None:
    config = agent.structured_output
    if not config.enabled:
        return None
    schema = config.json_schema
    raw_properties = schema.get("properties")
    properties = dict(raw_properties) if isinstance(raw_properties, dict) else {}
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for name, definition in properties.items():
        if not isinstance(definition, dict):
            definition = {}
        annotation = _schema_type(definition)
        default = ... if name in required else None
        if name not in required:
            annotation = Union.__getitem__((annotation, type(None)))
        fields[name] = (
            annotation,
            Field(default=default, description=definition.get("description")),
        )
    return create_model(
        config.name,
        __doc__=config.description,
        **fields,
    )


def _schema_type(schema: dict[str, Any]) -> Any:
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return Literal.__getitem__(tuple(schema["enum"]))
    kind = schema.get("type")
    if kind == "string":
        return str
    if kind == "integer":
        return int
    if kind == "number":
        return float
    if kind == "boolean":
        return bool
    if kind == "array":
        return list.__class_getitem__(_schema_type(schema.get("items") or {}))
    if kind == "object":
        return dict[str, Any]
    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list) and variants:
        types = tuple(_schema_type(item) for item in variants if isinstance(item, dict))
        if types:
            return Union.__getitem__(types)
    return Any


def _require_capability(value: bool | None, kind: str) -> None:
    if value is False:
        raise ModelInputUnsupportedError(
            f"the selected model is configured not to accept {kind} input",
            details={"input_kind": kind},
        )


def _shared_capabilities(values: list[ModelCapabilities]) -> ModelCapabilities:
    """Return the conservative input contract shared by all parallel start Agents."""

    combined: dict[str, bool | None] = {}
    for field_name in ModelCapabilities.model_fields:
        states = [getattr(value, field_name) for value in values]
        combined[field_name] = (
            False
            if any(state is False for state in states)
            else True
            if states and all(states)
            else None
        )
    return ModelCapabilities(**combined)


def _data_media(value: Any, *, kind: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value.startswith("data:"):
        raise ModelInputUnsupportedError(
            f"{kind} URLs are not fetched by the Agent service; send a base64 data URL"
        )
    header, separator, payload = value.partition(",")
    if not separator or ";base64" not in header:
        raise ModelInputUnsupportedError(f"{kind} input must be a base64 data URL")
    media_type = header[5:].split(";", 1)[0].lower()
    format_name = media_type.rsplit("/", 1)[-1].replace("jpg", "jpeg")
    if kind == "document" and format_name == "plain":
        format_name = "txt"
    try:
        decoded = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise ModelInputUnsupportedError(f"{kind} input contains invalid base64") from exc
    return {"format": format_name, "source": {"bytes": decoded}}
