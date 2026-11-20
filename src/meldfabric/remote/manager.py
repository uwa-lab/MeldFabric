"""Remote Agent discovery, delegation, session mapping, and topology."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from meldfabric.config.models import (
    AgentDefinition,
    RemoteAgent,
    RemoteToolBinding,
    WorkspaceConfig,
)
from meldfabric.domain import InteractionPolicy, SessionPolicy
from meldfabric.domain.identity import identity_digest
from meldfabric.domain.serialization import jsonable
from meldfabric.errors import (
    AgentNotReadyError,
    RemoteAgentCycleError,
    RemoteAgentUnreachableError,
    RemoteInteractionNotSupportedError,
)
from meldfabric.protocols.a2a import A2A_PROTOCOL
from meldfabric.remote.a2a_client import A2ARemoteClient
from meldfabric.remote.client import MeldFabricRemoteClient
from meldfabric.remote.models import (
    RemoteAgentDescriptor,
    RemoteCallContext,
    RemoteTopology,
    TopologyEdge,
    TopologyNode,
    TopologyUnreachable,
)

if TYPE_CHECKING:
    from meldfabric.storage import SecretReader, SystemRepository

ClientFactory = Callable[[RemoteAgent, str], Any]
MappingRepositoryGetter = Callable[[WorkspaceConfig], Awaitable["SystemRepository"]]
logger = logging.getLogger(__name__)


class RemoteAgentManager:
    def __init__(
        self,
        *,
        secrets_getter: Callable[[int], SecretReader | None],
        mapping_repository_getter: MappingRepositoryGetter,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._secrets_getter = secrets_getter
        self._mapping_repository_getter = mapping_repository_getter
        self._client_factory = client_factory or self._default_client_factory
        self._descriptors: dict[tuple[int, str], RemoteAgentDescriptor] = {}
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}

    @staticmethod
    def _default_client_factory(resource: RemoteAgent, credential: str) -> Any:
        # Prefer MeldFabric Remote when both are present because it preserves MeldFabric's
        # session, interruption and delegation-chain semantics. A2A is the
        # native fallback for A2A-only peers.
        if not any(item.startswith("meldfabric_remote/") for item in resource.protocols) and (
            A2A_PROTOCOL in resource.protocols
        ):
            return A2ARemoteClient(
                resource.base_url,
                credential,
                timeout_seconds=resource.timeout_seconds,
                service_id=resource.remote_service_id,
                agent_id=resource.remote_agent_id,
            )
        return MeldFabricRemoteClient(
            resource.base_url,
            credential,
            timeout_seconds=resource.timeout_seconds,
        )

    async def discover_resource(
        self,
        revision: int,
        resource: RemoteAgent,
        *,
        refresh: bool = False,
    ) -> RemoteAgentDescriptor:
        key = (revision, resource.id)
        if not refresh and key in self._descriptors:
            return self._descriptors[key]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if not refresh and key in self._descriptors:
                return self._descriptors[key]
            client = await self.client_for(resource, revision=revision)
            descriptor = await client.discover()
            if resource.remote_service_id and descriptor.service_id != resource.remote_service_id:
                raise RemoteAgentUnreachableError(
                    "remote service identity changed",
                    details={
                        "remote_agent_ref": resource.id,
                        "expected_service_id": resource.remote_service_id,
                        "actual_service_id": descriptor.service_id,
                    },
                )
            if resource.remote_agent_id and descriptor.agent_id != resource.remote_agent_id:
                raise RemoteAgentUnreachableError(
                    "remote agent identity changed",
                    details={
                        "remote_agent_ref": resource.id,
                        "expected_agent_id": resource.remote_agent_id,
                        "actual_agent_id": descriptor.agent_id,
                    },
                )
            self._descriptors[key] = descriptor
            return descriptor

    async def client_for(self, resource: RemoteAgent, *, revision: int) -> Any:
        secrets_repository = self._secrets_getter(revision)
        if secrets_repository is None:
            raise AgentNotReadyError(
                "the control secret store is not ready for remote Agent credentials",
                details={"remote_agent_ref": resource.id},
            )
        if resource.credential_secret_ref is None:
            raise AgentNotReadyError(
                f"remote Agent {resource.id!r} has no access key configured",
                details={"remote_agent_ref": resource.id},
            )
        credential = await secrets_repository.get_secret(resource.credential_secret_ref)
        if credential is None:
            raise AgentNotReadyError(
                f"remote Agent credential {resource.credential_secret_ref!r} was not found",
                details={"remote_agent_ref": resource.id},
            )
        return self._client_factory(resource, credential)

    async def strands_tools_for_agent(
        self,
        config: WorkspaceConfig,
        agent: AgentDefinition,
    ) -> list[Any]:
        """Compile remote bindings as native Strands delegation tools."""

        from strands import tool
        from strands.types.tools import ToolContext

        resources = {item.id: item for item in config.resources.remote_agents}
        result: list[Any] = []
        names: set[str] = set()
        for binding in agent.remote_tool_bindings:
            if not binding.enabled:
                continue
            resource = resources.get(binding.remote_agent_ref)
            if resource is None or not resource.enabled:
                raise AgentNotReadyError(
                    f"remote Agent {binding.remote_agent_ref!r} is unavailable",
                    details={"agent_id": agent.id, "binding_id": binding.id},
                )
            descriptor = await self.discover_resource(config.config_generation, resource)
            if descriptor.status != "active":
                raise AgentNotReadyError(
                    f"remote Agent {descriptor.node_id!r} is disabled",
                    details={"agent_id": agent.id, "binding_id": binding.id},
                )
            if resource.name in names:
                raise AgentNotReadyError(f"duplicate remote Agent tool name {resource.name!r}")
            names.add(resource.name)

            def make_delegate(
                selected_resource: RemoteAgent,
                selected_binding: RemoteToolBinding,
                selected_descriptor: RemoteAgentDescriptor,
            ):
                async def delegate(request: str, tool_context: ToolContext) -> str:
                    """Delegate a task to the selected remote Agent."""

                    return await self._invoke_strands_tool(
                        config,
                        agent,
                        selected_resource,
                        selected_binding,
                        selected_descriptor,
                        request,
                        tool_context,
                    )

                return delegate

            result.append(
                tool(
                    name=resource.name,
                    description=resource.description or descriptor.description,
                    context="tool_context",
                )(make_delegate(resource, binding, descriptor))
            )
        return result

    async def _invoke_strands_tool(
        self,
        config: WorkspaceConfig,
        local_agent: AgentDefinition,
        resource: RemoteAgent,
        binding: RemoteToolBinding,
        descriptor: RemoteAgentDescriptor,
        request: str,
        tool_context: Any,
    ) -> str:
        invocation_state = tool_context.invocation_state
        user_id = str(invocation_state.get("meldfabric_user_id") or "anonymous")
        parent_thread_id = str(
            invocation_state.get("meldfabric_internal_thread_id") or f"thread_{uuid.uuid4().hex}"
        )
        raw_context = invocation_state.get("meldfabric_remote_context")
        if isinstance(raw_context, RemoteCallContext):
            context = raw_context
        elif isinstance(raw_context, dict):
            context = RemoteCallContext.model_validate(raw_context)
        else:
            context = RemoteCallContext.root(
                service_id=config.workspace.id,
                agent_id=local_agent.id,
            )
        repository = await self._mapping_repository_getter(config)
        interaction_id = repository.remote_interaction_id(
            workspace_id=config.workspace.id,
            local_agent_id=local_agent.id,
            binding_id=binding.id,
            parent_thread_id=parent_thread_id,
        )
        interaction = await repository.get_remote_interaction(interaction_id)
        client = await self.client_for(resource, revision=config.config_generation)
        if interaction is None:
            delegated_context = context.delegate_to(descriptor.node_id)
            remote_thread_id = await self._remote_thread_id(
                config,
                local_agent_id=local_agent.id,
                remote_agent_id=resource.id,
                binding=binding,
                user_id=user_id,
                parent_thread_id=parent_thread_id,
            )
            remote_result = await client.invoke(
                messages=[
                    {
                        "id": f"remote_message_{uuid.uuid4().hex}",
                        "role": "user",
                        "content": request,
                    }
                ],
                user_id=user_id,
                thread_id=remote_thread_id,
                remote_context=delegated_context,
            )
            if not self._is_interrupted(remote_result):
                return self._remote_result_text(remote_result.result)
            if binding.interaction_policy == InteractionPolicy.FAIL:
                await self._cancel_interrupted_remote(
                    client,
                    remote_result.run_id,
                    user_id,
                    remote_thread_id,
                )
                raise RemoteInteractionNotSupportedError(
                    "remote Agent requested interaction but this binding is configured to fail",
                    details={"binding_id": binding.id, "remote_agent_ref": resource.id},
                )
            stages = [
                {
                    "request": self._interaction_request(
                        remote_result.result,
                        binding=binding,
                        resource=resource,
                        remote_thread_id=remote_thread_id,
                        remote_run_id=remote_result.run_id,
                    ),
                    "delegation_request": request,
                }
            ]
            interaction = await repository.put_remote_interaction(
                interaction_id=interaction_id,
                workspace_id=config.workspace.id,
                local_agent_id=local_agent.id,
                binding_id=binding.id,
                remote_agent_id=resource.id,
                user_id=user_id,
                parent_thread_id=parent_thread_id,
                remote_thread_id=remote_thread_id,
                remote_run_id=remote_result.run_id,
                remote_context=delegated_context.model_dump(mode="json"),
                stages=stages,
            )
        return await self._continue_strands_interaction(
            repository=repository,
            interaction=interaction,
            config=config,
            local_agent=local_agent,
            resource=resource,
            binding=binding,
            client=client,
            user_id=user_id,
            tool_context=tool_context,
        )

    async def _continue_strands_interaction(
        self,
        *,
        repository: SystemRepository,
        interaction: Any,
        config: WorkspaceConfig,
        local_agent: AgentDefinition,
        resource: RemoteAgent,
        binding: RemoteToolBinding,
        client: Any,
        user_id: str,
        tool_context: Any,
    ) -> str:
        from meldfabric.remote.models import RemoteRunResult

        stages = [dict(item) for item in interaction.stages]
        remote_run_id = interaction.remote_run_id
        delegated_context = RemoteCallContext.model_validate(interaction.remote_context)
        index = 0
        while index < len(stages):
            if index >= 8:
                raise RemoteInteractionNotSupportedError(
                    "remote interaction exceeded the maximum of eight rounds",
                    details={"binding_id": binding.id, "remote_agent_ref": resource.id},
                )
            stage = stages[index]
            cached_response = stage.get("remote_response")
            if isinstance(cached_response, dict):
                remote_result = RemoteRunResult.model_validate(cached_response)
            else:
                if "answer" in stage:
                    answer = stage["answer"]
                elif binding.interaction_policy == InteractionPolicy.PARENT_AUTO_ANSWER:
                    answer = await self._strands_auto_answer(
                        tool_context,
                        {
                            **stage["request"],
                            "delegation_request": stages[0].get("delegation_request"),
                        },
                    )
                else:
                    answer = tool_context.interrupt(
                        f"meldfabric:remote:{binding.id}:{index}",
                        reason=stage["request"],
                    )
                stage["answer"] = jsonable(answer, include_object_id=True)
                await self._save_interaction(
                    repository,
                    interaction_id=interaction.id,
                    config=config,
                    local_agent=local_agent,
                    resource=resource,
                    binding=binding,
                    interaction=interaction,
                    remote_run_id=remote_run_id,
                    stages=stages,
                )
                remote_result = await client.resume(
                    run_id=remote_run_id,
                    user_id=user_id,
                    thread_id=interaction.remote_thread_id,
                    remote_context=delegated_context,
                    resume=self._resume_entries(stage["request"], answer),
                )
                stage["remote_response"] = remote_result.model_dump(mode="json")
            remote_run_id = remote_result.run_id
            if not self._is_interrupted(remote_result):
                await self._save_interaction(
                    repository,
                    interaction_id=interaction.id,
                    config=config,
                    local_agent=local_agent,
                    resource=resource,
                    binding=binding,
                    interaction=interaction,
                    remote_run_id=remote_run_id,
                    stages=stages,
                )
                await repository.delete_remote_interaction(interaction.id)
                return self._remote_result_text(remote_result.result)
            if index + 1 == len(stages):
                stages.append(
                    {
                        "request": self._interaction_request(
                            remote_result.result,
                            binding=binding,
                            resource=resource,
                            remote_thread_id=interaction.remote_thread_id,
                            remote_run_id=remote_result.run_id,
                        )
                    }
                )
            await self._save_interaction(
                repository,
                interaction_id=interaction.id,
                config=config,
                local_agent=local_agent,
                resource=resource,
                binding=binding,
                interaction=interaction,
                remote_run_id=remote_run_id,
                stages=stages,
            )
            index += 1
        raise RemoteInteractionNotSupportedError("remote interaction state is incomplete")

    async def _strands_auto_answer(self, tool_context: Any, request: dict[str, Any]) -> Any:
        import json

        from strands import Agent

        action_requests = request.get("action_requests")
        action_count = len(action_requests) if isinstance(action_requests, list) else 0
        instruction = (
            "Answer a delegated Agent interaction. Return only JSON as "
            '{"decisions":[{"type":"approve|reject|edit","message":"..."}]} '
            f"with exactly {action_count} decisions. Reject when intent or safety is unclear."
            if action_count
            else "Answer the delegated Agent's question concisely and directly."
        )
        helper = Agent(
            model=tool_context.agent.model,
            system_prompt=instruction,
            callback_handler=None,
        )
        try:
            result = await helper.invoke_async(
                json.dumps(request, ensure_ascii=False),
                invocation_context=tool_context.invocation_context,
            )
        finally:
            await helper.shutdown_async()
        content = str(result).strip()
        if not action_count:
            return content
        if content.startswith("```"):
            lines = content.splitlines()
            content = "\n".join(lines[1:-1]).removeprefix("json").lstrip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
        if (
            isinstance(parsed, dict)
            and isinstance(parsed.get("decisions"), list)
            and len(parsed["decisions"]) == action_count
        ):
            return parsed
        return {
            "decisions": [
                {"type": "reject", "message": "Parent Agent returned no valid decision"}
                for _ in range(action_count)
            ]
        }

    @staticmethod
    async def _cancel_interrupted_remote(
        client: Any,
        run_id: str,
        user_id: str,
        thread_id: str,
    ) -> None:
        try:
            await client.cancel(run_id=run_id, user_id=user_id, thread_id=thread_id)
        except Exception:
            logger.warning("failed to cancel interrupted remote run %s", run_id, exc_info=True)

    async def _save_interaction(
        self,
        repository: SystemRepository,
        *,
        interaction_id: str,
        config: WorkspaceConfig,
        local_agent: AgentDefinition,
        resource: RemoteAgent,
        binding: RemoteToolBinding,
        interaction: Any,
        remote_run_id: str,
        stages: list[dict[str, Any]],
    ) -> None:
        await repository.put_remote_interaction(
            interaction_id=interaction_id,
            workspace_id=config.workspace.id,
            local_agent_id=local_agent.id,
            binding_id=binding.id,
            remote_agent_id=resource.id,
            user_id=interaction.user_id,
            parent_thread_id=interaction.parent_thread_id,
            remote_thread_id=interaction.remote_thread_id,
            remote_run_id=remote_run_id,
            remote_context=interaction.remote_context,
            stages=stages,
        )

    @staticmethod
    def _is_interrupted(result: Any) -> bool:
        return result.status == "interrupted" or bool(result.result.get("__interrupt__"))

    @classmethod
    def _interaction_request(
        cls,
        result: dict[str, Any],
        *,
        binding: RemoteToolBinding,
        resource: RemoteAgent,
        remote_thread_id: str,
        remote_run_id: str,
    ) -> dict[str, Any]:
        interrupts = result.get("__interrupt__", [])
        if not isinstance(interrupts, list):
            interrupts = [interrupts]
        serialized = [jsonable(item, include_object_id=True) for item in interrupts]
        action_requests: list[dict[str, Any]] = []
        review_configs: list[dict[str, Any]] = []
        messages: list[str] = []
        for item in serialized:
            value = item.get("reason", item.get("value")) if isinstance(item, dict) else None
            if not isinstance(value, dict):
                continue
            raw_actions = value.get("action_requests")
            raw_reviews = value.get("review_configs")
            if isinstance(raw_actions, list):
                action_requests.extend(
                    dict(action) for action in raw_actions if isinstance(action, dict)
                )
                if isinstance(raw_reviews, list):
                    review_configs.extend(
                        dict(review) for review in raw_reviews if isinstance(review, dict)
                    )
            message = value.get("message") or value.get("description")
            if message:
                messages.append(str(message))
        request: dict[str, Any] = {
            "kind": "remote_agent_interaction",
            "message": "\n".join(messages) or f"Remote Agent {resource.name!r} requires input.",
            "interaction_policy": binding.interaction_policy.value,
            "remote": {
                "resource_id": resource.id,
                "binding_id": binding.id,
                "thread_id": remote_thread_id,
                "run_id": remote_run_id,
            },
            "remote_interrupts": serialized,
        }
        if action_requests:
            while len(review_configs) < len(action_requests):
                review_configs.append({"allowed_decisions": ["approve", "reject", "edit"]})
            request["action_requests"] = action_requests
            request["review_configs"] = review_configs
        return request

    @staticmethod
    def _resume_entries(
        request: dict[str, Any],
        answer: Any,
    ) -> list[dict[str, Any]]:
        interrupts = request.get("remote_interrupts", [])
        decisions = answer.get("decisions") if isinstance(answer, dict) else None
        decision_index = 0
        entries: list[dict[str, Any]] = []
        for item in interrupts:
            if not isinstance(item, dict) or not item.get("id"):
                raise RemoteInteractionNotSupportedError(
                    "remote Agent returned an interrupt without an id"
                )
            value = item.get("reason", item.get("value"))
            action_count = 0
            if isinstance(value, dict) and isinstance(value.get("action_requests"), list):
                action_count = len(value["action_requests"])
            if action_count and isinstance(decisions, list):
                selected = decisions[decision_index : decision_index + action_count]
                if len(selected) != action_count:
                    raise RemoteInteractionNotSupportedError(
                        "the parent answer does not cover every remote tool decision"
                    )
                payload: Any = {"decisions": selected}
                decision_index += action_count
            else:
                payload = answer
            entries.append(
                {
                    "interrupt_id": str(item["id"]),
                    "status": "resolved",
                    "payload": payload,
                }
            )
        if not entries:
            raise RemoteInteractionNotSupportedError(
                "remote Agent reported interruption without interrupt descriptors"
            )
        return entries

    async def _remote_thread_id(
        self,
        config: WorkspaceConfig,
        *,
        local_agent_id: str,
        remote_agent_id: str,
        binding: RemoteToolBinding,
        user_id: str,
        parent_thread_id: str,
    ) -> str:
        if binding.session_policy == SessionPolicy.PER_CALL:
            return f"remote_{uuid.uuid4().hex}"
        if binding.session_policy == SessionPolicy.PARENT_THREAD:
            mapping_key = identity_digest(
                config.workspace.id,
                "parent_thread",
                local_agent_id,
                binding.id,
                parent_thread_id,
                remote_agent_id,
            )
        else:
            mapping_key = identity_digest(
                config.workspace.id,
                "per_user",
                local_agent_id,
                binding.id,
                user_id,
                remote_agent_id,
            )
        repository = await self._mapping_repository_getter(config)
        existing = await repository.get_remote_mapping(mapping_key)
        if existing is not None:
            return existing.remote_thread_id
        remote_thread_id = f"remote_{uuid.uuid4().hex}"
        mapping = await repository.put_remote_mapping(
            mapping_key=mapping_key,
            local_agent_id=local_agent_id,
            remote_agent_id=remote_agent_id,
            remote_thread_id=remote_thread_id,
            policy=binding.session_policy.value,
            user_id=user_id if binding.session_policy == SessionPolicy.PER_USER else None,
            parent_thread_id=(
                parent_thread_id if binding.session_policy == SessionPolicy.PARENT_THREAD else None
            ),
        )
        return mapping.remote_thread_id

    async def topology(
        self,
        config: WorkspaceConfig,
        agent: AgentDefinition,
        *,
        max_depth: int,
        root_agent_id: str | None = None,
        root_name: str | None = None,
    ) -> RemoteTopology:
        root = TopologyNode(
            service_id=config.workspace.id,
            agent_id=root_agent_id or agent.id,
            name=root_name or agent.name,
            status="active" if agent.enabled else "disabled",
            config_generation=config.config_generation,
        )
        topology = RemoteTopology(nodes=[root])
        resources = {item.id: item for item in config.resources.remote_agents}
        for binding in agent.remote_tool_bindings:
            if not binding.enabled:
                continue
            resource = resources.get(binding.remote_agent_ref)
            if resource is None or not resource.enabled:
                topology.unreachable.append(
                    TopologyUnreachable(
                        source=root.id,
                        remote_agent_ref=binding.remote_agent_ref,
                        error_code="AGENT_DISABLED",
                        message="remote Agent resource is missing or disabled",
                    )
                )
                continue
            try:
                descriptor = await self.discover_resource(config.config_generation, resource)
                node = TopologyNode(
                    service_id=descriptor.service_id,
                    agent_id=descriptor.agent_id,
                    name=descriptor.name,
                    status=descriptor.status,
                    config_generation=descriptor.config_generation,
                )
                topology.nodes.append(node)
                topology.edges.append(
                    TopologyEdge(
                        source=root.id,
                        target=node.id,
                        binding_id=binding.id,
                    )
                )
                if max_depth <= 0:
                    topology.truncated = True
                    continue
                downstream = await (
                    await self.client_for(resource, revision=config.config_generation)
                ).topology(max_depth=max_depth - 1)
                topology.nodes.extend(downstream.nodes)
                topology.edges.extend(downstream.edges)
                topology.cycles.extend(downstream.cycles)
                topology.unreachable.extend(downstream.unreachable)
                topology.truncated = topology.truncated or downstream.truncated
            except Exception as exc:
                code = getattr(exc, "code", "REMOTE_AGENT_UNREACHABLE")
                topology.unreachable.append(
                    TopologyUnreachable(
                        source=root.id,
                        remote_agent_ref=resource.id,
                        error_code=code,
                        message=str(exc),
                    )
                )
        self._deduplicate_and_detect_cycles(topology)
        return topology

    async def validate_bindings(
        self,
        config: WorkspaceConfig,
        agent: AgentDefinition,
        *,
        max_depth: int = 10,
    ) -> None:
        """Reject a binding update when its reachable topology is unsafe or unknown."""

        topology = await self.topology(config, agent, max_depth=max_depth)
        if topology.unreachable:
            raise RemoteAgentUnreachableError(
                "remote Agent topology preflight could not reach every enabled binding",
                details={
                    "agent_id": agent.id,
                    "unreachable": [item.model_dump(mode="json") for item in topology.unreachable],
                },
            )
        if topology.cycles:
            raise RemoteAgentCycleError(
                "remote Agent topology preflight detected a delegation cycle",
                details={"agent_id": agent.id, "cycles": topology.cycles},
            )

    @staticmethod
    def _remote_result_text(result: dict[str, Any]) -> str:
        for message in reversed(result.get("messages", [])):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                if content:
                    return content
                continue
            if isinstance(content, list):
                text = "".join(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and "text" in block
                )
                if text:
                    return text
        return str(result)

    @staticmethod
    def _deduplicate_and_detect_cycles(topology: RemoteTopology) -> None:
        topology.nodes = list({item.id: item for item in topology.nodes}.values())
        topology.edges = list(
            {(item.source, item.target, item.binding_id): item for item in topology.edges}.values()
        )
        adjacency: dict[str, list[str]] = {}
        for edge in topology.edges:
            adjacency.setdefault(edge.source, []).append(edge.target)
        cycles = {tuple(cycle) for cycle in topology.cycles}
        visited: set[str] = set()
        active: list[str] = []

        def visit(node: str) -> None:
            if node in active:
                index = active.index(node)
                cycles.add(tuple([*active[index:], node]))
                return
            if node in visited:
                return
            visited.add(node)
            active.append(node)
            for target in adjacency.get(node, []):
                visit(target)
            active.pop()

        for node in list(adjacency):
            visit(node)
        topology.cycles = [list(item) for item in sorted(cycles)]

    def prune(self, active_revisions: set[int]) -> None:
        for key in list(self._descriptors):
            if key[0] not in active_revisions:
                self._descriptors.pop(key, None)
                self._locks.pop(key, None)
