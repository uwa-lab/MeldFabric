"""Pure reconciliation of persisted configuration with process-local code registrations."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from meldfabric.config.models import (
    GraphOrchestration,
    HookResource,
    HumanInTheLoopInterventionResource,
    RegisteredInterventionResource,
    RegisteredPluginResource,
    ToolResource,
    WorkspaceConfig,
)
from meldfabric.domain.enums import ManagedBy
from meldfabric.registrations.catalog import RegistrationDefinition

ReconciliationCode = Literal[
    "REGISTRATION_MISSING",
    "REGISTRATION_CHANGED",
    "CONFIG_NORMALIZED",
    "CONFIG_INCOMPATIBLE",
]
ReconciliationSeverity = Literal["info", "warning", "critical"]
PublicRegistrationKind = Literal["tool", "plugin", "hook", "intervention", "graph_condition"]


class ReconciliationEffect(BaseModel):
    """One configured entity whose effective behavior is affected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_type: Literal["resource", "agent", "orchestration", "endpoint"]
    entity_id: str
    entity_name: str
    relation: str
    enabled: bool = True


class ReconciliationIssue(BaseModel):
    """One deterministic difference between persisted data and the code catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ReconciliationCode
    severity: ReconciliationSeverity
    registration_kind: PublicRegistrationKind
    registration_id: str
    subject_name: str
    resource_id: str | None = None
    message: str
    effects: list[ReconciliationEffect] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class ReconciliationReport(BaseModel):
    """Serializable report for one raw configuration generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    config_generation: int
    catalog_digest: str
    changed: bool
    issues: list[ReconciliationIssue] = Field(default_factory=list)

    @property
    def critical_count(self) -> int:
        return sum(issue.severity == "critical" for issue in self.issues)

    def summary(self) -> dict[str, Any]:
        counts = {"info": 0, "warning": 0, "critical": 0}
        for issue in self.issues:
            counts[issue.severity] += 1
        return {
            **self.model_dump(mode="json"),
            "counts": counts,
            "total": len(self.issues),
        }


class ReconciliationResult(BaseModel):
    """The clean effective configuration plus its complete impact report."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    config: WorkspaceConfig
    report: ReconciliationReport
    catalog_digest: str


def _catalog_digest(definitions: tuple[RegistrationDefinition, ...]) -> str:
    # All callers use the same immutable catalog snapshot. Import locally to keep this
    # pure function independently testable with synthetic definitions.
    import hashlib
    import json

    payload = sorted(
        (
            {"id": definition.id, "registration_digest": definition.digest}
            for definition in definitions
        ),
        key=lambda item: item["id"],
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _definition_maps(
    definitions: tuple[RegistrationDefinition, ...],
) -> dict[PublicRegistrationKind, dict[str, RegistrationDefinition]]:
    result: dict[PublicRegistrationKind, dict[str, RegistrationDefinition]] = {
        "tool": {},
        "plugin": {},
        "hook": {},
        "intervention": {},
        "graph_condition": {},
    }
    for definition in definitions:
        if definition.kind in {"hook", "hook_group"}:
            result["hook"][definition.id] = definition
        elif definition.kind in {"intervention", "intervention_group"}:
            result["intervention"][definition.id] = definition
        elif definition.kind == "tool":
            result["tool"][definition.id] = definition
        elif definition.kind == "plugin":
            result["plugin"][definition.id] = definition
        elif definition.kind == "graph_condition":
            result["graph_condition"][definition.id] = definition
    return result


def _effect(
    entity_type: Literal["resource", "agent", "orchestration", "endpoint"],
    entity: Any,
    relation: str,
) -> ReconciliationEffect:
    return ReconciliationEffect(
        entity_type=entity_type,
        entity_id=entity.id,
        entity_name=getattr(entity, "display_name", None) or entity.name,
        relation=relation,
        enabled=bool(getattr(entity, "enabled", True)),
    )


def _normalize_config(
    definition: RegistrationDefinition, value: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    config_model = definition.config_model
    if config_model is None:
        return {}, None
    try:
        normalized = config_model.model_validate(value).model_dump(mode="json")
        return normalized, None
    except Exception as exc:
        return None, str(exc)


def _config_delta(
    before: Any,
    after: Any,
    *,
    path: str = "",
) -> dict[str, list[str]]:
    """Summarize normalized JSON without returning its potentially sensitive values."""

    result: dict[str, list[str]] = {
        "removed_fields": [],
        "defaulted_fields": [],
        "normalized_fields": [],
    }
    if isinstance(before, dict) and isinstance(after, dict):
        before_keys = set(before)
        after_keys = set(after)
        result["removed_fields"].extend(
            f"{path}.{key}" if path else key for key in sorted(before_keys - after_keys)
        )
        result["defaulted_fields"].extend(
            f"{path}.{key}" if path else key for key in sorted(after_keys - before_keys)
        )
        for key in sorted(before_keys & after_keys):
            child_path = f"{path}.{key}" if path else key
            nested = _config_delta(before[key], after[key], path=child_path)
            for category, values in nested.items():
                result[category].extend(values)
        return result
    if before != after and path:
        result["normalized_fields"].append(path)
    return result


def reconcile_workspace(
    raw_config: WorkspaceConfig,
    definitions: tuple[RegistrationDefinition, ...],
    *,
    catalog_digest: str | None = None,
) -> ReconciliationResult:
    """Return a clean effective config without mutating or persisting the raw generation.

    This is the only code/config compatibility pipeline. Startup, save, apply and historical
    resume must all use it so their semantics cannot drift apart.
    """

    raw = deepcopy(raw_config)
    maps = _definition_maps(definitions)
    digest = catalog_digest or _catalog_digest(definitions)
    issues: list[ReconciliationIssue] = []
    removed_resource_ids: set[str] = set()
    direct_agents_by_registration: dict[str, set[str]] = defaultdict(set)
    direct_orchestrations_by_registration: dict[str, set[str]] = defaultdict(set)
    directly_bound_tool_ids: set[str] = set()
    intervention_agent_ids: dict[str, set[str]] = defaultdict(set)
    agents_by_id = {agent.id: agent for agent in raw.agents}
    orchestrations_by_id = {item.id: item for item in raw.orchestrations}
    orchestrations_by_agent: dict[str, list[Any]] = defaultdict(list)
    endpoints_by_target: dict[tuple[str, str], list[Any]] = defaultdict(list)

    for orchestration in raw.orchestrations:
        if orchestration.kind == "agent_as_tool":
            agent_refs = {
                orchestration.coordinator_ref,
                *(member.agent_ref for member in orchestration.members),
            }
        elif orchestration.kind == "swarm":
            agent_refs = set(orchestration.agent_refs)
        elif orchestration.kind == "graph":
            agent_refs = {
                node.target_ref for node in orchestration.nodes if node.target_type == "agent"
            }
        elif orchestration.kind == "workflow":
            agent_refs = {task.agent_ref for task in orchestration.tasks}
        else:
            agent_refs = set()
        for agent_id in agent_refs:
            orchestrations_by_agent[agent_id].append(orchestration)
    for endpoint in raw.endpoints:
        endpoints_by_target[(endpoint.target_type, endpoint.target_ref)].append(endpoint)

    def transitive_effects(
        direct_agents: set[str],
        direct_orchestrations: set[str],
    ) -> list[ReconciliationEffect]:
        """Use the precomputed reference graph to expand direct impact once per issue."""

        effects: list[ReconciliationEffect] = []
        affected_orchestrations = set(direct_orchestrations)
        for agent_id in direct_agents:
            for orchestration in orchestrations_by_agent.get(agent_id, ()):
                if orchestration.id in affected_orchestrations:
                    continue
                affected_orchestrations.add(orchestration.id)
                effects.append(_effect("orchestration", orchestration, "uses affected Agent"))
        affected_endpoints: dict[str, Any] = {}
        for agent_id in direct_agents:
            for endpoint in endpoints_by_target.get(("agent", agent_id), ()):
                affected_endpoints[endpoint.id] = endpoint
        for orchestration_id in affected_orchestrations:
            for endpoint in endpoints_by_target.get(("orchestration", orchestration_id), ()):
                affected_endpoints[endpoint.id] = endpoint
        effects.extend(
            _effect("endpoint", endpoint, "publishes affected target")
            for endpoint in affected_endpoints.values()
        )
        return effects

    def add_issue(
        *,
        code: ReconciliationCode,
        kind: PublicRegistrationKind,
        registration_id: str,
        resource: Any | None,
        message: str,
        severity: ReconciliationSeverity = "warning",
        details: dict[str, Any] | None = None,
        subject_name: str | None = None,
    ) -> None:
        direct_agents = direct_agents_by_registration[registration_id]
        direct_orchestrations = direct_orchestrations_by_registration[registration_id]
        effects: list[ReconciliationEffect] = []
        if resource is not None:
            effects.append(_effect("resource", resource, "code-backed configuration"))
        effects.extend(
            _effect("agent", agents_by_id[item], "has an affected binding")
            for item in sorted(direct_agents)
            if item in agents_by_id
        )
        effects.extend(
            _effect("orchestration", orchestrations_by_id[item], "uses affected condition")
            for item in sorted(direct_orchestrations)
            if item in orchestrations_by_id
        )
        effects.extend(transitive_effects(direct_agents, direct_orchestrations))
        # Disabled or unmounted configuration is visible drift, not a runtime outage.
        runtime_effect = any(
            effect.enabled and effect.entity_type == "endpoint" for effect in effects
        )
        effective_severity: ReconciliationSeverity = severity
        if severity == "critical" and not runtime_effect:
            effective_severity = "warning" if effects else "info"
        elif severity == "warning" and not runtime_effect:
            effective_severity = "info"
        unique_effects: list[ReconciliationEffect] = []
        seen_effects: set[tuple[str, str, str]] = set()
        for effect in effects:
            key = (effect.entity_type, effect.entity_id, effect.relation)
            if key in seen_effects:
                continue
            seen_effects.add(key)
            unique_effects.append(effect)
        definition = maps[kind].get(registration_id)
        resource_name = (
            getattr(resource, "display_name", None) or getattr(resource, "name", None)
            if resource is not None
            else None
        )
        resolved_subject = (
            subject_name
            or resource_name
            or (definition.name if definition is not None else None)
            or registration_id
        )
        issues.append(
            ReconciliationIssue(
                code=code,
                severity=effective_severity,
                registration_kind=kind,
                registration_id=registration_id,
                subject_name=resolved_subject,
                resource_id=getattr(resource, "id", None),
                message=message,
                effects=unique_effects,
                details=details or {},
            )
        )

    # Capture direct usages from the raw generation before anything is filtered.
    resource_registration: dict[str, str] = {}
    for plugin_resource in raw.resources.plugins:
        if isinstance(plugin_resource, RegisteredPluginResource):
            resource_registration[plugin_resource.id] = plugin_resource.registration_id
    for hook_resource in raw.resources.hooks:
        resource_registration[hook_resource.id] = hook_resource.registration_id
    for intervention_resource in raw.resources.interventions:
        if isinstance(intervention_resource, RegisteredInterventionResource):
            resource_registration[intervention_resource.id] = intervention_resource.registration_id
    for tool in raw.resources.tools:
        resource_registration[tool.id] = tool.id
    for agent in raw.agents:
        if not agent.enabled:
            continue
        for binding, ref_name in (
            *((binding, "tool_ref") for binding in agent.tool_bindings),
            *((binding, "plugin_ref") for binding in agent.plugin_bindings),
            *((binding, "hook_ref") for binding in agent.hook_bindings),
            *((binding, "intervention_ref") for binding in agent.intervention_bindings),
        ):
            if not binding.enabled:
                continue
            reference = getattr(binding, ref_name)
            registration_id = resource_registration.get(reference, reference)
            direct_agents_by_registration[registration_id].add(agent.id)
            if ref_name == "tool_ref":
                directly_bound_tool_ids.add(registration_id)
            elif ref_name == "intervention_ref":
                intervention_agent_ids[reference].add(agent.id)
    for intervention in raw.resources.interventions:
        if not isinstance(intervention, HumanInTheLoopInterventionResource):
            continue
        for registration_id in intervention.tool_refs:
            directly_bound_tool_ids.add(registration_id)
            direct_agents_by_registration[registration_id].update(
                intervention_agent_ids.get(intervention.id, set())
            )
    for orchestration in raw.orchestrations:
        if not isinstance(orchestration, GraphOrchestration) or not orchestration.enabled:
            continue
        for edge in orchestration.edges:
            if edge.condition_registration_id:
                direct_orchestrations_by_registration[edge.condition_registration_id].add(
                    orchestration.id
                )

    # Tools use registration ID as their one-to-one resource/configuration identity.
    persisted_tools = {item.id: item for item in raw.resources.tools}
    effective_tools: list[ToolResource] = []
    for registration_id, definition in maps["tool"].items():
        existing = persisted_tools.get(registration_id)
        payload = definition.payload
        if existing is None:
            effective_tools.append(
                ToolResource(
                    id=registration_id,
                    name=payload.name,
                    display_name=payload.display_name,
                    description="",
                    python_path=payload.python_path,
                    requires_sandbox=False,
                    registration_digest=definition.digest,
                )
            )
            continue
        if existing.registration_digest and existing.registration_digest != definition.digest:
            add_issue(
                code="REGISTRATION_CHANGED",
                kind="tool",
                registration_id=registration_id,
                resource=existing,
                message=f"Tool {existing.name!r} public contract changed",
                details={
                    "previous_digest": existing.registration_digest,
                    "current_digest": definition.digest,
                },
            )
        valid_parameters = set((payload.input_schema.get("properties") or {}).keys())
        normalized_descriptions = {
            key: value
            for key, value in existing.parameter_descriptions.items()
            if key in valid_parameters
        }
        if normalized_descriptions != existing.parameter_descriptions:
            add_issue(
                code="CONFIG_NORMALIZED",
                kind="tool",
                registration_id=registration_id,
                resource=existing,
                message=f"Tool parameter descriptions for {existing.name!r} were normalized",
                details={
                    "removed_parameter_descriptions": sorted(
                        set(existing.parameter_descriptions) - valid_parameters
                    )
                },
            )
        effective_tools.append(
            existing.model_copy(
                update={
                    "managed_by": ManagedBy.CODE,
                    "python_path": payload.python_path,
                    "registration_digest": definition.digest,
                    "parameter_descriptions": normalized_descriptions,
                }
            )
        )
    for registration_id, tool_resource in persisted_tools.items():
        if registration_id not in maps["tool"]:
            removed_resource_ids.add(tool_resource.id)
            add_issue(
                code="REGISTRATION_MISSING",
                kind="tool",
                registration_id=registration_id,
                resource=tool_resource,
                message=f"Tool registration {registration_id!r} no longer exists",
                severity="critical",
            )
    # A historical ToolBinding can exist without a persisted ToolResource. If its code
    # registration disappeared, it still needs the same deterministic cleanup/report.
    configured_tool_ids = set(persisted_tools)
    for registration_id in directly_bound_tool_ids:
        agent_ids = direct_agents_by_registration[registration_id]
        if registration_id in configured_tool_ids or registration_id in maps["tool"]:
            continue
        removed_resource_ids.add(registration_id)
        add_issue(
            code="REGISTRATION_MISSING",
            kind="tool",
            registration_id=registration_id,
            resource=None,
            message=f"Tool registration {registration_id!r} no longer exists",
            severity="critical",
            details={"agent_ids": sorted(agent_ids)},
        )
    raw.resources.tools = effective_tools

    def reconcile_instances(
        kind: Literal["plugin", "hook", "intervention"], values: list[Any]
    ) -> list[Any]:
        retained: list[Any] = []
        for resource in values:
            registered = isinstance(
                resource,
                (RegisteredPluginResource, HookResource, RegisteredInterventionResource),
            )
            if not registered:
                retained.append(resource)
                continue
            registration_id = resource.registration_id
            definition = maps[kind].get(registration_id)
            if definition is None:
                removed_resource_ids.add(resource.id)
                add_issue(
                    code="REGISTRATION_MISSING",
                    kind=kind,
                    registration_id=registration_id,
                    resource=resource,
                    message=f"{kind.replace('_', ' ').title()} registration "
                    f"{registration_id!r} no longer exists",
                    severity="critical" if kind == "intervention" else "warning",
                )
                continue
            normalized, error = _normalize_config(definition, resource.config)
            if normalized is None:
                removed_resource_ids.add(resource.id)
                add_issue(
                    code="CONFIG_INCOMPATIBLE",
                    kind=kind,
                    registration_id=registration_id,
                    resource=resource,
                    message=f"Configuration for {resource.name!r} no longer matches its code",
                    severity="critical",
                    details={"validation_error": error},
                )
                continue
            if resource.registration_digest and resource.registration_digest != definition.digest:
                add_issue(
                    code="REGISTRATION_CHANGED",
                    kind=kind,
                    registration_id=registration_id,
                    resource=resource,
                    message=f"Code contract for {resource.name!r} changed",
                    details={
                        "previous_digest": resource.registration_digest,
                        "current_digest": definition.digest,
                    },
                )
            if normalized != resource.config:
                delta = _config_delta(resource.config, normalized)
                add_issue(
                    code="CONFIG_NORMALIZED",
                    kind=kind,
                    registration_id=registration_id,
                    resource=resource,
                    message=(
                        f"Configuration for {resource.name!r} was normalized to its current schema"
                    ),
                    details=delta,
                )
            retained.append(
                resource.model_copy(
                    update={
                        "config": normalized,
                        "registration_digest": definition.digest,
                    }
                )
            )
        return retained

    raw.resources.plugins = reconcile_instances("plugin", raw.resources.plugins)
    raw.resources.hooks = reconcile_instances("hook", raw.resources.hooks)
    raw.resources.interventions = reconcile_instances("intervention", raw.resources.interventions)
    available_tool_ids = {item.id for item in raw.resources.tools}
    raw.resources.interventions = [
        item.model_copy(
            update={"tool_refs": [ref for ref in item.tool_refs if ref in available_tool_ids]}
        )
        if isinstance(item, HumanInTheLoopInterventionResource)
        else item
        for item in raw.resources.interventions
    ]

    # Remove bindings whose one-to-one Tool or configured extension instance disappeared.
    for agent in raw.agents:
        agent.tool_bindings = [
            binding
            for binding in agent.tool_bindings
            if binding.tool_ref not in removed_resource_ids
        ]
        agent.plugin_bindings = [
            binding
            for binding in agent.plugin_bindings
            if binding.plugin_ref not in removed_resource_ids
        ]
        agent.hook_bindings = [
            binding
            for binding in agent.hook_bindings
            if binding.hook_ref not in removed_resource_ids
        ]
        agent.intervention_bindings = [
            binding
            for binding in agent.intervention_bindings
            if binding.intervention_ref not in removed_resource_ids
        ]

    # Graph conditions have no resource instance. Missing/incompatible conditions degrade
    # the edge to unconditional, deliberately preserving graph reachability.
    for orchestration in raw.orchestrations:
        if not isinstance(orchestration, GraphOrchestration):
            continue
        for edge in orchestration.edges:
            registration_id = edge.condition_registration_id
            if not registration_id:
                edge.condition_config = {}
                edge.condition_registration_digest = None
                continue
            graph_definition = maps["graph_condition"].get(registration_id)
            if graph_definition is None:
                add_issue(
                    code="REGISTRATION_MISSING",
                    kind="graph_condition",
                    registration_id=registration_id,
                    resource=None,
                    message=(
                        f"Graph condition {registration_id!r} no longer exists; "
                        "edge is unconditional"
                    ),
                    severity="critical",
                    details={"orchestration_id": orchestration.id, "edge_id": edge.id},
                )
                edge.condition_registration_id = None
                edge.condition_registration_digest = None
                edge.condition_config = {}
                continue
            normalized, error = _normalize_config(graph_definition, edge.condition_config)
            if normalized is None:
                add_issue(
                    code="CONFIG_INCOMPATIBLE",
                    kind="graph_condition",
                    registration_id=registration_id,
                    resource=None,
                    message=(
                        f"Graph condition on edge {edge.id!r} is incompatible; "
                        "edge is unconditional"
                    ),
                    severity="critical",
                    details={
                        "orchestration_id": orchestration.id,
                        "edge_id": edge.id,
                        "validation_error": error,
                    },
                )
                edge.condition_registration_id = None
                edge.condition_registration_digest = None
                edge.condition_config = {}
                continue
            if (
                edge.condition_registration_digest
                and edge.condition_registration_digest != graph_definition.digest
            ):
                add_issue(
                    code="REGISTRATION_CHANGED",
                    kind="graph_condition",
                    registration_id=registration_id,
                    resource=None,
                    message=f"Graph condition contract on edge {edge.id!r} changed",
                    details={
                        "orchestration_id": orchestration.id,
                        "edge_id": edge.id,
                        "previous_digest": edge.condition_registration_digest,
                        "current_digest": graph_definition.digest,
                    },
                )
            if normalized != edge.condition_config:
                delta = _config_delta(edge.condition_config, normalized)
                add_issue(
                    code="CONFIG_NORMALIZED",
                    kind="graph_condition",
                    registration_id=registration_id,
                    resource=None,
                    message=f"Graph condition configuration on edge {edge.id!r} was normalized",
                    details={
                        "orchestration_id": orchestration.id,
                        "edge_id": edge.id,
                        **delta,
                    },
                )
            edge.condition_config = normalized
            edge.condition_registration_digest = graph_definition.digest

    effective = WorkspaceConfig.model_validate(raw.model_dump(mode="json"))
    changed = effective.model_dump(mode="json") != raw_config.model_dump(mode="json")
    report = ReconciliationReport(
        config_generation=raw_config.config_generation,
        catalog_digest=digest,
        changed=changed,
        issues=issues,
    )
    return ReconciliationResult(config=effective, report=report, catalog_digest=digest)
