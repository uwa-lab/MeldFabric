"""Strict, normalized configuration schema stored in the control database."""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from meldfabric.config.structured import (
    validate_database_options,
    validate_json_object,
    validate_json_schema,
)
from meldfabric.domain.enums import (
    DatabaseEngine,
    InteractionPolicy,
    ManagedBy,
    SessionPolicy,
)
from meldfabric.domain.names import (
    http_header_name,
    http_headers,
    management_path,
    model_name,
    version_name,
)
from meldfabric.errors import ConfigurationError, PromptRenderError
from meldfabric.prompts import PromptRenderer

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$"
    ),
]
ResourceName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
    BeforeValidator(model_name),
]
PromptVersionName = Annotated[str, BeforeValidator(version_name)]
JsonObject = Annotated[dict[str, Any], AfterValidator(validate_json_object)]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=False,
        protected_namespaces=(),
    )


class WorkspaceInfo(StrictModel):
    id: Identifier
    name: str = "MeldFabric"
    timezone: str = "Asia/Shanghai"


class BaseResource(StrictModel):
    id: Identifier = Field(default_factory=lambda: f"res_{uuid4().hex}")
    name: str = Field(min_length=1, max_length=128)
    display_name: str | None = None
    enabled: bool = True
    managed_by: ManagedBy = ManagedBy.CONFIG
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def preserve_human_name(cls, value: Any) -> Any:
        field = cls.model_fields.get("name")
        constrained = field is not None and any(
            isinstance(metadata, BeforeValidator) and metadata.func is model_name
            for metadata in field.metadata
        )
        if constrained and isinstance(value, dict) and isinstance(value.get("name"), str):
            original = value["name"]
            if (
                original.strip()
                and model_name(original) != original
                and not value.get("display_name")
            ):
                value = {**value, "display_name": original.strip()}
        return value


class DatabaseConnection(BaseResource):
    engine: DatabaseEngine
    url: str
    options: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def options_match_engine(self) -> DatabaseConnection:
        try:
            options = validate_database_options(
                self.options,
                engine=(
                    self.engine.value
                    if isinstance(self.engine, DatabaseEngine)
                    else str(self.engine)
                ),
            )
        except ConfigurationError as exc:
            raise ValueError(exc.message) from exc
        object.__setattr__(self, "options", options)
        return self


class ModelProvider(BaseResource):
    name: str = Field(min_length=1, max_length=128)
    adapter: str = "openai_compatible"
    base_url: str
    api_key_secret_ref: Identifier | None = None
    timeout_seconds: float = Field(default=30.0, gt=0)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("headers")
    @classmethod
    def valid_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return http_headers(value)


class ModelCapabilities(StrictModel):
    """Explicit input and tool policy; None means unverified, never guessed by name."""

    tool_calling: bool | None = None
    image_inputs: bool | None = None
    audio_inputs: bool | None = None
    video_inputs: bool | None = None
    pdf_inputs: bool | None = None


class ModelResource(BaseResource):
    name: str = Field(default="model", min_length=1, max_length=512)
    provider_ref: Identifier
    model_name: str
    protocol: Literal["chat_completions", "responses"] = "chat_completions"
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    timeout_seconds: float | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_retries: int | None = Field(default=None, ge=0)
    parameters: JsonObject = Field(default_factory=dict)

    @field_validator("parameters")
    @classmethod
    def connection_fields_belong_to_provider(cls, value: dict[str, Any]) -> dict[str, Any]:
        reserved = {
            "model",
            "model_name",
            "base_url",
            "openai_api_base",
            "api_key",
            "openai_api_key",
            "profile",
            "protocol",
            "use_responses_api",
        }
        if forbidden := reserved.intersection(value):
            raise ValueError(
                f"configure model name and provider credentials in their dedicated fields, "
                f"not parameters: {sorted(forbidden)}"
            )
        return value


class PromptVersion(StrictModel):
    version: PromptVersionName
    content: str

    @field_validator("content")
    @classmethod
    def template_uses_only_safe_variables(cls, value: str) -> str:
        try:
            PromptRenderer().validate(value)
        except PromptRenderError as exc:
            raise ValueError(exc.message) from exc
        return value


class PromptConfig(StrictModel):
    selected_version: PromptVersionName = "v1"
    versions: list[PromptVersion] = Field(
        default_factory=lambda: [PromptVersion(version="v1", content="")], min_length=1
    )

    @model_validator(mode="after")
    def selected_version_exists(self) -> PromptConfig:
        versions = [item.version for item in self.versions]
        if len(versions) != len(set(versions)):
            raise ValueError("prompt version identifiers must be unique")
        if self.selected_version not in versions:
            raise ValueError(f"selected prompt version {self.selected_version!r} does not exist")
        return self


class ToolResource(BaseResource):
    name: ResourceName
    registration_digest: str | None = None
    description: str = ""
    managed_by: ManagedBy = ManagedBy.CODE
    python_path: str | None = None
    requires_sandbox: bool = False
    parameter_descriptions: dict[str, str] = Field(default_factory=dict)


class MCPServer(BaseResource):
    name: ResourceName
    transport: Literal["streamable_http"] = "streamable_http"
    url: str
    credential_secret_ref: Identifier | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    credential_header: str = "Authorization"
    credential_scheme: str = "Bearer"
    timeout_seconds: float = Field(default=30.0, gt=0)

    @field_validator("credential_header")
    @classmethod
    def valid_credential_header(cls, value: str) -> str:
        return http_header_name(value)

    @field_validator("headers")
    @classmethod
    def valid_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return http_headers(value)

    @field_validator("credential_scheme")
    @classmethod
    def valid_credential_scheme(cls, value: str) -> str:
        value = value.strip()
        return http_header_name(value) if value else value


class SkillSource(BaseResource):
    path: str
    order: int = 0


class RegisteredPluginResource(BaseResource):
    """A trusted application plugin registered by host Python code."""

    kind: Literal["registered"] = "registered"
    name: ResourceName
    registration_id: Identifier
    registration_digest: str | None = None
    config: JsonObject = Field(default_factory=dict)


class AgentSkillsPluginResource(BaseResource):
    """Strands AgentSkills configuration backed by reusable Skill directories."""

    kind: Literal["agent_skills"] = "agent_skills"
    name: ResourceName
    skill_source_refs: list[Identifier] = Field(min_length=1)
    strict: bool = False


class ContextOffloaderPluginResource(BaseResource):
    """Strands ContextOffloader configuration with durable MeldFabric storage."""

    kind: Literal["context_offloader"] = "context_offloader"
    name: ResourceName
    database_ref: Identifier
    max_result_tokens: int = Field(default=2500, ge=100)
    preview_tokens: int = Field(default=1000, ge=50)
    include_retrieval_tool: bool = True


class GoalLoopPluginResource(BaseResource):
    """Strands GoalLoop configuration; planning is not an Agent core switch."""

    kind: Literal["goal_loop"] = "goal_loop"
    name: ResourceName
    goal: str = Field(min_length=1)
    max_attempts: int = Field(default=3, ge=1, le=20)
    timeout_seconds: float = Field(default=300, gt=0)
    preserve_context: bool = True


PluginResource = Annotated[
    RegisteredPluginResource
    | AgentSkillsPluginResource
    | ContextOffloaderPluginResource
    | GoalLoopPluginResource,
    Field(discriminator="kind"),
]


class HookResource(BaseResource):
    """Reusable configuration for a code-registered hook or hook group."""

    name: ResourceName
    registration_id: Identifier
    registration_digest: str | None = None
    config: JsonObject = Field(default_factory=dict)


class RegisteredInterventionResource(BaseResource):
    kind: Literal["registered"] = "registered"
    name: ResourceName
    registration_id: Identifier
    registration_digest: str | None = None
    config: JsonObject = Field(default_factory=dict)
    on_error: Literal["throw", "proceed", "deny"] = "throw"


class HumanInTheLoopInterventionResource(BaseResource):
    """Native Strands HumanInTheLoop intervention in interrupt/resume mode."""

    kind: Literal["human_in_the_loop"] = "human_in_the_loop"
    name: ResourceName
    tool_refs: list[Identifier] = Field(default_factory=list)
    mcp_tool_names: list[str] = Field(default_factory=list)
    enable_trust: bool = False


InterventionResource = Annotated[
    RegisteredInterventionResource | HumanInTheLoopInterventionResource,
    Field(discriminator="kind"),
]


class SandboxProfile(BaseResource):
    provider: str = "docker"
    network_enabled: bool = False
    cpu_limit: float | None = Field(default=None, gt=0)
    memory_limit_mb: int | None = Field(default=None, gt=0)
    timeout_seconds: int = Field(default=300, gt=0)
    ttl_seconds: int = Field(default=900, gt=0)
    config: JsonObject = Field(default_factory=dict)


class ObservabilityProvider(BaseResource):
    adapter: Literal["langfuse"] = "langfuse"
    host: str
    public_key: str
    secret_key_ref: Identifier | None = None
    sample_rate: float = Field(default=1.0, ge=0, le=1)
    fail_closed: bool = False
    environment: str | None = None


class RemoteAgent(BaseResource):
    name: ResourceName
    description: str = ""
    managed_by: ManagedBy = ManagedBy.REMOTE
    base_url: str
    credential_secret_ref: Identifier | None = None
    remote_service_id: str | None = None
    remote_agent_id: str | None = None
    protocols: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=60.0, gt=0)


class ResourceBinding(StrictModel):
    id: Identifier = Field(default_factory=lambda: f"bind_{uuid4().hex}")
    enabled: bool = True


class ToolBinding(ResourceBinding):
    tool_ref: Identifier


class MCPBinding(ResourceBinding):
    mcp_ref: Identifier


class PluginBinding(ResourceBinding):
    plugin_ref: Identifier
    order: int = 0


class HookBinding(ResourceBinding):
    hook_ref: Identifier
    order: int = 0


class InterventionBinding(ResourceBinding):
    intervention_ref: Identifier
    order: int = 0


class RemoteToolBinding(ResourceBinding):
    remote_agent_ref: Identifier
    session_policy: SessionPolicy = SessionPolicy.PARENT_THREAD
    interaction_policy: InteractionPolicy = InteractionPolicy.BUBBLE_TO_PARENT

    @model_validator(mode="after")
    def per_call_cannot_resume_interactions(self) -> RemoteToolBinding:
        if (
            self.session_policy is SessionPolicy.PER_CALL
            and self.interaction_policy is not InteractionPolicy.FAIL
        ):
            raise ValueError(
                "per_call remote sessions require interaction_policy='fail' because "
                "the remote conversation is intentionally not reusable"
            )
        return self


class SessionManagerConfig(StrictModel):
    """Native Strands SnapshotSessionManager configuration."""

    database_ref: Identifier
    save_latest_on: Literal["invocation", "message"] = "invocation"
    checkpointing: bool = False


class MemoryManagerConfig(StrictModel):
    """Native Strands MemoryManager configuration backed by a database resource."""

    database_ref: Identifier
    namespace: str | None = None
    search_tool: bool = True
    add_tool: bool = False
    injection: bool = True


class ConversationManagerConfig(StrictModel):
    strategy: Literal["sliding_window", "summarizing", "none"] = "sliding_window"
    window_size: int = Field(default=40, ge=2)
    truncate_tool_results: bool = True
    per_turn: bool | int = False
    summary_ratio: float = Field(default=0.3, ge=0.1, le=0.8)
    preserve_recent_messages: int = Field(default=10, ge=1)
    summarization_system_prompt: str | None = None
    pin_first: int | None = Field(default=None, ge=0)
    proactive_compression: bool = False


class ContextManagerConfig(StrictModel):
    strategy: Literal["disabled", "auto", "agentic"] = "auto"


class RetryStrategyConfig(StrictModel):
    enabled: bool = True
    max_attempts: int = Field(default=6, ge=1, le=20)
    initial_delay_seconds: float = Field(default=4, ge=0)
    max_delay_seconds: float = Field(default=128, ge=0)


class StructuredOutputConfig(StrictModel):
    enabled: bool = False
    name: ResourceName = "AgentOutput"
    description: str | None = None
    json_schema: JsonObject = Field(default_factory=dict)
    prompt: str | None = None

    @model_validator(mode="after")
    def enabled_schema_is_object(self) -> StructuredOutputConfig:
        if self.enabled or self.json_schema:
            try:
                validate_json_schema(
                    self.json_schema,
                    source="structured output JSON schema",
                    require_object=True,
                )
            except ConfigurationError as exc:
                raise ValueError(exc.message) from exc
        return self


class BackgroundTasksConfig(StrictModel):
    enabled: bool = False


class AgentDefinition(BaseResource):
    """A reusable Strands Agent specification, independent of publication and orchestration."""

    name: ResourceName
    description: str = ""
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    model_ref: Identifier | None = None
    initial_state: JsonObject = Field(default_factory=dict)
    tool_bindings: list[ToolBinding] = Field(default_factory=list)
    mcp_bindings: list[MCPBinding] = Field(default_factory=list)
    remote_tool_bindings: list[RemoteToolBinding] = Field(default_factory=list)
    plugin_bindings: list[PluginBinding] = Field(default_factory=list)
    hook_bindings: list[HookBinding] = Field(default_factory=list)
    intervention_bindings: list[InterventionBinding] = Field(default_factory=list)
    conversation_manager: ConversationManagerConfig = Field(
        default_factory=ConversationManagerConfig
    )
    context_manager: ContextManagerConfig = Field(default_factory=ContextManagerConfig)
    session_manager: SessionManagerConfig | None = None
    memory_manager: MemoryManagerConfig | None = None
    retry_strategy: RetryStrategyConfig = Field(default_factory=RetryStrategyConfig)
    structured_output: StructuredOutputConfig = Field(default_factory=StructuredOutputConfig)
    background_tasks: BackgroundTasksConfig = Field(default_factory=BackgroundTasksConfig)
    sandbox_ref: Identifier | None = None
    observability_ref: Identifier | None = None


class OrchestrationSessionConfig(StrictModel):
    database_ref: Identifier
    save_latest_on: Literal["invocation", "message"] = "invocation"


class AgentToolMember(StrictModel):
    id: Identifier = Field(default_factory=lambda: f"member_{uuid4().hex}")
    agent_ref: Identifier
    name: ResourceName | None = None
    description: str | None = None
    preserve_context: bool = True


class AgentAsToolOrchestration(BaseResource):
    kind: Literal["agent_as_tool"] = "agent_as_tool"
    name: ResourceName
    description: str = ""
    coordinator_ref: Identifier
    members: list[AgentToolMember] = Field(default_factory=list)


class SwarmOrchestration(BaseResource):
    kind: Literal["swarm"] = "swarm"
    name: ResourceName
    description: str = ""
    agent_refs: list[Identifier] = Field(min_length=2)
    entry_agent_ref: Identifier | None = None
    max_handoffs: int = Field(default=20, ge=1, le=100)
    max_iterations: int = Field(default=20, ge=1, le=100)
    execution_timeout_seconds: float = Field(default=900, gt=0)
    node_timeout_seconds: float = Field(default=300, gt=0)
    session_manager: OrchestrationSessionConfig | None = None

    @model_validator(mode="after")
    def entry_is_a_member(self) -> SwarmOrchestration:
        if len(self.agent_refs) != len(set(self.agent_refs)):
            raise ValueError("Swarm agent references must be unique")
        if self.entry_agent_ref is not None and self.entry_agent_ref not in self.agent_refs:
            raise ValueError("Swarm entry_agent_ref must be one of agent_refs")
        return self


class CanvasPosition(StrictModel):
    """Persisted editor coordinates; ignored by the Strands runtime."""

    x: float = Field(ge=-1_000_000, le=1_000_000)
    y: float = Field(ge=-1_000_000, le=1_000_000)


class GraphNode(StrictModel):
    id: Identifier = Field(default_factory=lambda: f"node_{uuid4().hex}")
    target_type: Literal["agent", "remote_agent"] = "agent"
    target_ref: Identifier
    position: CanvasPosition | None = None


class GraphEdge(StrictModel):
    id: Identifier = Field(default_factory=lambda: f"edge_{uuid4().hex}")
    source: Identifier
    target: Identifier
    condition_registration_id: Identifier | None = None
    condition_registration_digest: str | None = None
    condition_config: JsonObject = Field(default_factory=dict)


class GraphOrchestration(BaseResource):
    kind: Literal["graph"] = "graph"
    name: ResourceName
    description: str = ""
    nodes: list[GraphNode] = Field(min_length=1)
    edges: list[GraphEdge] = Field(default_factory=list)
    entry_points: list[Identifier] = Field(min_length=1)
    primary_entry_point: Identifier | None = None
    max_node_executions: int = Field(default=100, ge=1)
    session_manager: OrchestrationSessionConfig | None = None

    @model_validator(mode="after")
    def graph_is_well_formed(self) -> GraphOrchestration:
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Graph node identifiers must be unique")
        nodes_by_id = {node.id: node for node in self.nodes}
        valid = set(nodes_by_id)
        if len(self.entry_points) != len(set(self.entry_points)):
            raise ValueError("Graph entry points must be unique")
        if not set(self.entry_points).issubset(valid):
            raise ValueError("Graph entry points must reference graph nodes")
        primary = self.primary_entry_point or self.entry_points[0]
        if primary not in self.entry_points:
            raise ValueError("Graph primary entry point must be one of entry_points")
        if nodes_by_id[primary].target_type != "agent":
            raise ValueError("Graph primary entry point must be a local Agent node")
        object.__setattr__(self, "primary_entry_point", primary)
        edge_pairs: set[tuple[str, str]] = set()
        for edge in self.edges:
            if edge.source not in valid or edge.target not in valid:
                raise ValueError("Graph edges must reference graph nodes")
            if edge.source == edge.target:
                raise ValueError("Graph nodes cannot connect to themselves")
            pair = (edge.source, edge.target)
            if pair in edge_pairs:
                raise ValueError("Graph edges must be unique by source and target")
            edge_pairs.add(pair)
        return self


class WorkflowTask(StrictModel):
    id: Identifier = Field(default_factory=lambda: f"task_{uuid4().hex}")
    agent_ref: Identifier
    instruction: str = Field(min_length=1)
    dependency_refs: list[Identifier] = Field(default_factory=list)
    position: CanvasPosition | None = None


class WorkflowOrchestration(BaseResource):
    kind: Literal["workflow"] = "workflow"
    name: ResourceName
    description: str = ""
    tasks: list[WorkflowTask] = Field(min_length=1)
    primary_task_ref: Identifier | None = None

    @model_validator(mode="after")
    def workflow_is_a_dag(self) -> WorkflowOrchestration:
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Workflow task identifiers must be unique")
        valid = set(task_ids)
        dependencies = {task.id: set(task.dependency_refs) for task in self.tasks}
        for task in self.tasks:
            if len(task.dependency_refs) != len(set(task.dependency_refs)):
                raise ValueError("Workflow task dependencies must be unique")
            if task.id in task.dependency_refs:
                raise ValueError("Workflow tasks cannot depend on themselves")
        if any(not refs.issubset(valid) for refs in dependencies.values()):
            raise ValueError("Workflow dependencies must reference workflow tasks")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("Workflow task dependencies must form a DAG")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in dependencies[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        roots = [task.id for task in self.tasks if not task.dependency_refs]
        primary = self.primary_task_ref or roots[0]
        if primary not in roots:
            raise ValueError("Workflow primary task must be a start task without dependencies")
        object.__setattr__(self, "primary_task_ref", primary)
        return self


OrchestrationDefinition = Annotated[
    AgentAsToolOrchestration | SwarmOrchestration | GraphOrchestration | WorkflowOrchestration,
    Field(discriminator="kind"),
]

EndpointProtocol = Literal["rest", "ag_ui", "meldfabric_remote", "a2a"]


def _default_endpoint_protocols() -> list[EndpointProtocol]:
    return ["rest", "ag_ui"]


class PublishedEndpoint(BaseResource):
    """An executable Agent or orchestration exposed through one or more protocols."""

    name: ResourceName
    description: str = ""
    target_type: Literal["agent", "orchestration"] = "agent"
    target_ref: Identifier
    protocols: list[EndpointProtocol] = Field(default_factory=_default_endpoint_protocols)

    @model_validator(mode="after")
    def protocols_are_unique(self) -> PublishedEndpoint:
        if not self.protocols:
            raise ValueError("published endpoint requires at least one protocol")
        if len(self.protocols) != len(set(self.protocols)):
            raise ValueError("published endpoint protocols must be unique")
        if "a2a" in self.protocols and self.target_type != "agent":
            raise ValueError("native Strands A2A publication currently requires an Agent target")
        return self


class ResourcesConfig(StrictModel):
    databases: list[DatabaseConnection] = Field(default_factory=list)
    model_providers: list[ModelProvider] = Field(default_factory=list)
    models: list[ModelResource] = Field(default_factory=list)
    tools: list[ToolResource] = Field(default_factory=list)
    mcp_servers: list[MCPServer] = Field(default_factory=list)
    skill_sources: list[SkillSource] = Field(default_factory=list)
    plugins: list[PluginResource] = Field(default_factory=list)
    hooks: list[HookResource] = Field(default_factory=list)
    interventions: list[InterventionResource] = Field(default_factory=list)
    sandbox_profiles: list[SandboxProfile] = Field(default_factory=list)
    observability_providers: list[ObservabilityProvider] = Field(default_factory=list)
    remote_agents: list[RemoteAgent] = Field(default_factory=list)

    def iter_resources(self):
        for field_name in type(self).model_fields:
            yield from getattr(self, field_name)


class ManagementConfig(StrictModel):
    path: str = "/admin"

    @field_validator("path", mode="before")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return management_path(value)


class RuntimeConfig(StrictModel):
    driver: Literal["embedded_fastapi"] = "embedded_fastapi"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    workers: int = Field(default=1, ge=1)
    maintenance_interval_seconds: int = Field(default=60, ge=5, le=3600)
    management: ManagementConfig = Field(default_factory=ManagementConfig)


class WorkspaceConfig(StrictModel):
    schema_version: Literal["4"] = "4"
    config_generation: int = Field(default=1, ge=1)
    workspace: WorkspaceInfo
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    agents: list[AgentDefinition] = Field(default_factory=list)
    orchestrations: list[OrchestrationDefinition] = Field(default_factory=list)
    endpoints: list[PublishedEndpoint] = Field(default_factory=list)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @model_validator(mode="after")
    def validate_identity_and_references(self) -> WorkspaceConfig:
        provider_names = {item.id: item.name for item in self.resources.model_providers}
        for model in self.resources.models:
            if model.provider_ref in provider_names:
                qualified_name = f"{provider_names[model.provider_ref]}/{model.model_name}"
                object.__setattr__(model, "name", qualified_name)
                object.__setattr__(model, "display_name", qualified_name)
        resources = list(self.resources.iter_resources())
        all_entities = [*resources, *self.agents, *self.orchestrations, *self.endpoints]
        duplicates = [
            key for key, count in Counter(item.id for item in all_entities).items() if count > 1
        ]
        if duplicates:
            raise ValueError(f"resource identifiers must be globally unique: {sorted(duplicates)}")

        for collection_name in type(self.resources).model_fields:
            collection = getattr(self.resources, collection_name)
            duplicate_names = [
                name
                for name, count in Counter(item.name for item in collection).items()
                if count > 1
            ]
            if duplicate_names:
                raise ValueError(
                    f"resource names must be unique within {collection_name}: "
                    f"{sorted(duplicate_names)}"
                )
        for label, collection in (
            ("Agent", self.agents),
            ("orchestration", self.orchestrations),
            ("endpoint", self.endpoints),
        ):
            duplicate_names = [
                name
                for name, count in Counter(item.name for item in collection).items()
                if count > 1
            ]
            if duplicate_names:
                raise ValueError(f"{label} names must be unique: {sorted(duplicate_names)}")

        bindings = [
            binding
            for agent in self.agents
            for binding in [
                *agent.tool_bindings,
                *agent.mcp_bindings,
                *agent.remote_tool_bindings,
                *agent.plugin_bindings,
                *agent.hook_bindings,
                *agent.intervention_bindings,
            ]
        ]
        duplicate_bindings = [
            key for key, count in Counter(item.id for item in bindings).items() if count > 1
        ]
        entity_ids = {item.id for item in all_entities}
        colliding_bindings = sorted(entity_ids.intersection(item.id for item in bindings))
        if duplicate_bindings or colliding_bindings:
            raise ValueError(
                "binding identifiers must be globally unique and must not collide with "
                f"resource identifiers: {sorted(set(duplicate_bindings + colliding_bindings))}"
            )

        by_id = {item.id: item for item in all_entities}
        providers = {item.id for item in self.resources.model_providers}
        for model in self.resources.models:
            self._require_ref(by_id, model.provider_ref, f"model {model.id}.provider_ref")
            if model.provider_ref not in providers:
                raise ValueError(f"model {model.id}.provider_ref must reference a model provider")
            if model.enabled and not by_id[model.provider_ref].enabled:
                raise ValueError(f"model {model.id}.provider_ref references a disabled provider")

        pools = {
            name: {item.id for item in getattr(self.resources, name)}
            for name in type(self.resources).model_fields
        }

        def active_ref(reference, pool, path):
            self._optional_typed_ref(reference, pools[pool], path)
            if reference is not None and not by_id[reference].enabled:
                raise ValueError(f"{path} references disabled resource {reference!r}")

        def validate_agent(agent: AgentDefinition) -> None:
            for name, pool in (
                ("model_ref", "models"),
                ("sandbox_ref", "sandbox_profiles"),
                ("observability_ref", "observability_providers"),
            ):
                active_ref(getattr(agent, name), pool, f"agent {agent.id}.{name}")
            for name in ("session_manager", "memory_manager"):
                storage = getattr(agent, name)
                if storage is not None:
                    active_ref(
                        storage.database_ref, "databases", f"agent {agent.id}.{name}.database_ref"
                    )
            for field, ref_field, pool in (
                ("tool_bindings", "tool_ref", "tools"),
                ("mcp_bindings", "mcp_ref", "mcp_servers"),
                ("remote_tool_bindings", "remote_agent_ref", "remote_agents"),
                ("plugin_bindings", "plugin_ref", "plugins"),
                ("hook_bindings", "hook_ref", "hooks"),
                ("intervention_bindings", "intervention_ref", "interventions"),
            ):
                for binding in getattr(agent, field):
                    reference = getattr(binding, ref_field)
                    # Code-owned tools are registered by @register.tool in the running
                    # process. Their administrative resource row is optional;
                    # runtime compilation remains the source of truth for
                    # availability. All other resource kinds are fully declared.
                    if pool != "tools" or reference in pools[pool]:
                        self._typed_ref(reference, pools[pool], f"binding {binding.id}")
                    if not binding.enabled:
                        continue
                    if pool != "tools" or reference in pools[pool]:
                        active_ref(reference, pool, f"binding {binding.id}")
                    if (
                        pool == "tools"
                        and reference in by_id
                        and by_id[reference].requires_sandbox
                        and agent.sandbox_ref is None
                    ):
                        raise ValueError(
                            f"tool binding {binding.id} requires agent {agent.id} to bind a sandbox"
                        )
            resumable_intervention = any(
                binding.enabled
                and isinstance(
                    by_id[binding.intervention_ref],
                    HumanInTheLoopInterventionResource,
                )
                for binding in agent.intervention_bindings
            )
            if resumable_intervention and agent.session_manager is None:
                raise ValueError(
                    f"agent {agent.id} uses resumable interventions but has no session manager"
                )

        for agent in self.agents:
            validate_agent(agent)

        for plugin in self.resources.plugins:
            if isinstance(plugin, AgentSkillsPluginResource):
                for reference in plugin.skill_source_refs:
                    active_ref(reference, "skill_sources", f"plugin {plugin.id}.skill_source_refs")
            elif isinstance(plugin, ContextOffloaderPluginResource):
                active_ref(plugin.database_ref, "databases", f"plugin {plugin.id}.database_ref")
        for intervention in self.resources.interventions:
            if isinstance(intervention, HumanInTheLoopInterventionResource):
                for reference in intervention.tool_refs:
                    if reference in pools["tools"]:
                        active_ref(
                            reference,
                            "tools",
                            f"intervention {intervention.id}.tool_refs",
                        )

        agent_ids = {agent.id for agent in self.agents}
        remote_ids = {item.id for item in self.resources.remote_agents}
        orchestration_ids = {item.id for item in self.orchestrations}
        for orchestration in self.orchestrations:
            if isinstance(orchestration, AgentAsToolOrchestration):
                if (
                    orchestration.coordinator_ref not in agent_ids
                    or not by_id[orchestration.coordinator_ref].enabled
                ):
                    raise ValueError(
                        f"orchestration {orchestration.id}.coordinator_ref references "
                        "an unavailable Agent"
                    )
                for member in orchestration.members:
                    if member.agent_ref not in agent_ids or not by_id[member.agent_ref].enabled:
                        raise ValueError(
                            f"orchestration member {member.id} references unavailable Agent"
                        )
            elif isinstance(orchestration, SwarmOrchestration):
                for reference in orchestration.agent_refs:
                    if reference not in agent_ids or not by_id[reference].enabled:
                        raise ValueError(
                            f"Swarm {orchestration.id} references unavailable Agent {reference!r}"
                        )
            elif isinstance(orchestration, GraphOrchestration):
                for node in orchestration.nodes:
                    valid = agent_ids if node.target_type == "agent" else remote_ids
                    if node.target_ref not in valid or not by_id[node.target_ref].enabled:
                        raise ValueError(
                            f"Graph node {node.id} references unavailable {node.target_type}"
                        )
            elif isinstance(orchestration, WorkflowOrchestration):
                for task in orchestration.tasks:
                    if task.agent_ref not in agent_ids or not by_id[task.agent_ref].enabled:
                        raise ValueError(f"Workflow task {task.id} references unavailable Agent")
            session = getattr(orchestration, "session_manager", None)
            if session is not None:
                active_ref(
                    session.database_ref,
                    "databases",
                    f"orchestration {orchestration.id}.session_manager.database_ref",
                )

        for endpoint in self.endpoints:
            valid = agent_ids if endpoint.target_type == "agent" else orchestration_ids
            if endpoint.target_ref not in valid or not by_id[endpoint.target_ref].enabled:
                raise ValueError(
                    f"endpoint {endpoint.id} references unavailable {endpoint.target_type}"
                )
            if endpoint.target_type == "orchestration":
                target = by_id[endpoint.target_ref]
                if isinstance(target, GraphOrchestration):
                    primary_entry = next(
                        node for node in target.nodes if node.id == target.primary_entry_point
                    )
                    if primary_entry.target_type != "agent":
                        raise ValueError(
                            f"endpoint {endpoint.id} cannot publish a Graph whose primary "
                            "entry point is a remote Agent"
                        )
        return self

    @staticmethod
    def _require_ref(by_id: dict[str, BaseResource], ref: str, path: str) -> None:
        if ref not in by_id:
            raise ValueError(f"{path} references unknown resource {ref!r}")

    @staticmethod
    def _typed_ref(ref: str, valid_ids: set[str], path: str) -> None:
        if ref not in valid_ids:
            raise ValueError(f"{path} references an unknown or incompatible resource {ref!r}")

    @classmethod
    def _optional_typed_ref(cls, ref: str | None, valid_ids: set[str], path: str) -> None:
        if ref is not None:
            cls._typed_ref(ref, valid_ids, path)
