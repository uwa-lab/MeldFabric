"""Strands-native management, health, and publication APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from meldfabric._version import __version__
from meldfabric.agents.types import NativeExecutionResult
from meldfabric.config.backup import ControlBackupService
from meldfabric.config.database import ControlDatabaseSpec
from meldfabric.config.models import (
    AgentDefinition,
    PublishedEndpoint,
    ToolResource,
    WorkspaceConfig,
)
from meldfabric.config.structured import (
    structured_schema_catalog,
)
from meldfabric.domain.serialization import jsonable
from meldfabric.errors import AgentKitError, ConfigurationError, OptionalDependencyError
from meldfabric.identity import UserIdentity
from meldfabric.management.entities import (
    AGENT_ADAPTER,
    ENDPOINT_ADAPTER,
    ORCHESTRATION_ADAPTER,
    RESOURCE_ADAPTERS,
    entity_patch,
    new_entity_id,
    replace_entity,
    validate_graph_conditions,
    validate_registered_resource,
)
from meldfabric.management.mutations import deletion_patch
from meldfabric.management.playground import (
    PlaygroundTranscriptCollector,
    playground_user_id,
    runtime_messages,
    serialize_conversation,
    serialize_message,
)
from meldfabric.management.references import resource_references
from meldfabric.management.schemas import (
    AdminKeyUpdateRequest,
    BackupRequest,
    CatalogDigest,
    ConfigApplyRequest,
    ConfigPatchRequest,
    CreateEndpointKeyRequest,
    EntityMutationRequest,
    InvokeRequest,
    KeyStatusRequest,
    ModelPreviewRequest,
    PlaygroundConversationCreateRequest,
    PlaygroundResumeRequest,
    PlaygroundRunRequest,
    ProviderPreviewRequest,
    RemoteDiscoveryRequest,
    RuntimeSuspendRequest,
    SecretRequest,
    SnapshotCreateRequest,
    SnapshotRestoreRequest,
    ToolOverrideRequest,
)
from meldfabric.management.web import admin_asset, admin_html
from meldfabric.models.probe import probe_model
from meldfabric.protocols.a2a import A2A_PROTOCOL, NativeA2AGateway
from meldfabric.protocols.ag_ui import (
    AGUIRunInput,
    encode_sse,
    transcode_runtime_events,
    transcode_runtime_stream,
)
from meldfabric.remote import (
    A2ARemoteClient,
    MeldFabricRemoteClient,
    RemoteCallContext,
    RemoteCancelRequest,
    RemoteInvokeRequest,
    RemoteResumeRequest,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from meldfabric.app import AgentKit

logger = logging.getLogger(__name__)


def _primary_agent(config: WorkspaceConfig, endpoint: PublishedEndpoint) -> AgentDefinition:
    if endpoint.target_type == "agent":
        reference = endpoint.target_ref
    else:
        orchestration = next(
            item for item in config.orchestrations if item.id == endpoint.target_ref
        )
        if orchestration.kind == "agent_as_tool":
            reference = orchestration.coordinator_ref
        elif orchestration.kind == "swarm":
            reference = orchestration.entry_agent_ref or orchestration.agent_refs[0]
        elif orchestration.kind == "graph":
            entry = next(
                node for node in orchestration.nodes if node.id == orchestration.primary_entry_point
            )
            if entry.target_type != "agent":
                raise ConfigurationError("Graph primary entry is not a local Agent")
            reference = entry.target_ref
        else:
            reference = next(
                task.agent_ref
                for task in orchestration.tasks
                if task.id == orchestration.primary_task_ref
            )
    return next(item for item in config.agents if item.id == reference)


async def _discover_remote(payload: RemoteDiscoveryRequest):
    arguments = (
        payload.base_url,
        payload.access_key.get_secret_value(),
    )
    if payload.protocol in {"auto", "meldfabric_remote"}:
        try:
            return await MeldFabricRemoteClient(
                *arguments, timeout_seconds=payload.timeout_seconds
            ).discover()
        except AgentKitError:
            if payload.protocol == "meldfabric_remote":
                raise
    return await A2ARemoteClient(*arguments, timeout_seconds=payload.timeout_seconds).discover()


def create_management_app(agent_kit: AgentKit) -> FastAPI:
    """Build the combined Strands control and publication service."""
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException, Request
        from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
    except ImportError as exc:
        raise OptionalDependencyError("FastAPI is required to create the service") from exc
    # FastAPI resolves postponed route annotations from module globals.
    globals()["Request"] = Request

    management_path = agent_kit.runtime_config.runtime.management.path
    background_tasks: set[asyncio.Task[Any]] = set()
    a2a_gateway = NativeA2AGateway(agent_kit)

    @asynccontextmanager
    async def lifespan(_app):
        agent_kit.tool_registry.capture(agent_kit.runtime_config.config_generation)
        await agent_kit.storage.initialize(agent_kit.runtime_config)
        await agent_kit.persistence.initialize(agent_kit.runtime_config)
        await agent_kit.agent_runtime.start()
        try:
            yield
        finally:
            for task in tuple(background_tasks):
                task.cancel()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            await a2a_gateway.aclose()
            await agent_kit.agent_runtime.close()
            await agent_kit.mcp.aclose()
            await agent_kit.observability.aclose()
            await agent_kit.sandboxes.close()
            await agent_kit.persistence.close()
            await agent_kit.storage.close()

    app = FastAPI(
        title="MeldFabric",
        version=__version__,
        description="Strands-native Agent runtime and control plane",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def route_management_prefix(request: Request, call_next):
        path = request.scope.get("path", "")
        if agent_kit.runtime_suspended and path not in {"/health", "/health/live"}:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "SERVICE_SUSPENDED",
                        "message": "Service access is suspended until the process restarts",
                        "details": {"action": "restart_service"},
                    }
                },
            )
        canonical = path == "/admin" or path.startswith("/admin/")
        configured = path == management_path or path.startswith(management_path + "/")
        if management_path != "/admin" and configured:
            suffix = path[len(management_path) :]
            request.scope["path"] = "/admin" + suffix
            request.scope["raw_path"] = request.scope["path"].encode()
        elif management_path != "/admin" and canonical:
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return await call_next(request)

    async def require_admin_key(
        authorization: str | None = Header(default=None),
        x_admin_key: str | None = Header(default=None),
    ) -> str:
        provided = x_admin_key
        if authorization and authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        if not provided or not await agent_kit.verify_admin_key(provided):
            raise HTTPException(status_code=401, detail={"code": "ADMIN_UNAUTHORIZED"})
        await agent_kit.asynchronize_management_configuration()
        return provided

    @app.exception_handler(AgentKitError)
    async def handle_agent_error(_request: Request, exc: AgentKitError):
        status = {
            "AGENT_NOT_FOUND": 404,
            "AGENT_DISABLED": 403,
            "AGENT_NOT_READY": 503,
            "SERVICE_SUSPENDED": 503,
            "CONFIG_GENERATION_CONFLICT": 409,
            "CODE_CATALOG_CONFLICT": 409,
            "RUN_CONFLICT": 409,
            "THREAD_BUSY": 409,
            "RUN_ALREADY_ACTIVE": 409,
            "RUN_ID_ALREADY_USED": 409,
            "RUN_NOT_FOUND": 404,
            "MODEL_SERVICE_ERROR": 502,
            "MODEL_TIMEOUT": 504,
            "SANDBOX_UNAVAILABLE": 503,
            "CONTROL_DATABASE_UNAVAILABLE": 503,
        }.get(exc.code, 422)
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": jsonable(exc.details),
                }
            },
        )

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/live", tags=["system"])
    def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/management", tags=["system"])
    async def management_health():
        state = await asyncio.to_thread(agent_kit.repository.generation_state)
        return {
            "status": "ready",
            "latest_generation": state.latest_generation,
            "active_generation": state.active_generation,
        }

    @app.get("/health/business", tags=["system"])
    def business_health():
        if agent_kit.last_reload_error is not None:
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {"status": "ready", "config_generation": agent_kit.snapshots.current.revision}

    async def configuration_status() -> dict[str, Any]:
        state = await asyncio.to_thread(agent_kit.repository.generation_state)
        runtime = (
            await agent_kit.storage.system.runtime_coordination_status()
            if agent_kit.storage.system is not None
            else {"instances": [], "active_leases": []}
        )
        instances = [
            item for item in runtime["instances"] if item["status"] not in {"lost", "stopped"}
        ]
        catalog_consistent = all(
            item.get("catalog_digest") == agent_kit.catalog_digest
            for item in instances
            if item["status"] == "ready"
        )
        return {
            "latest_generation": state.latest_generation,
            "active_generation": state.active_generation,
            "local_generation": agent_kit.snapshots.current.revision,
            "has_unapplied_changes": state.latest_generation != state.active_generation,
            "code_catalog_digest": agent_kit.catalog_digest,
            "catalog_consistent": catalog_consistent,
            "fully_applied": catalog_consistent
            and bool(instances)
            and all(
                item["status"] == "ready" and item["config_generation"] == state.active_generation
                for item in instances
            ),
            "instances": instances,
        }

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/admin/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/admin/ui", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/admin/ui/{page:path}", response_class=HTMLResponse, include_in_schema=False)
    def admin_web(request: Request):
        return HTMLResponse(
            admin_html(management_path, request.scope.get("root_path", "")),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/admin/static/{path:path}", include_in_schema=False)
    def admin_static(path: str):
        asset = admin_asset(path)
        if asset is None:
            raise HTTPException(status_code=404, detail="Not Found")
        content, media_type = asset
        return Response(
            content,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    admin = [Depends(require_admin_key)]

    @app.get("/admin/config", dependencies=admin, tags=["configuration"])
    def get_config():
        return agent_kit.config.model_dump(mode="json")

    @app.get("/admin/config-schema", dependencies=admin, tags=["configuration"])
    def config_schema():
        return WorkspaceConfig.model_json_schema()

    @app.post("/admin/config/preview", dependencies=admin, tags=["configuration"])
    async def preview_config(payload: ConfigPatchRequest):
        agent_kit._assert_catalog_digest(payload.expected_catalog_digest)
        preview = await asyncio.to_thread(
            agent_kit.config_store.preview_patch,
            payload.patch,
            expected_generation=payload.expected_generation,
            base_config=agent_kit.config,
        )
        return preview.model_dump(mode="json")

    @app.patch("/admin/config", dependencies=admin, tags=["configuration"])
    async def patch_config(payload: ConfigPatchRequest):
        loaded = await agent_kit.apatch_config(
            payload.patch,
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
        )
        return loaded.config.model_dump(mode="json")

    @app.get("/admin/runtime/config-status", dependencies=admin, tags=["runtime"])
    async def get_config_status():
        return await configuration_status()

    @app.post("/admin/runtime/apply", dependencies=admin, tags=["runtime"])
    async def apply_config(payload: ConfigApplyRequest):
        await agent_kit.aapply_latest_config(
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
        )
        return await configuration_status()

    @app.get("/admin/runtime/reconciliation", dependencies=admin, tags=["runtime"])
    def reconciliation_status():
        return {
            plane: report.summary() for plane, report in agent_kit.reconciliation_reports.items()
        }

    @app.post("/admin/runtime/suspend", dependencies=admin, tags=["runtime"])
    async def suspend_runtime(payload: RuntimeSuspendRequest):
        if not payload.confirm:
            raise ConfigurationError("runtime suspension requires explicit confirmation")
        await agent_kit.asuspend_runtime()
        return {
            "suspended": True,
            "message": "service access remains suspended until the process restarts",
        }

    @app.get("/admin/runtime/capabilities", dependencies=admin, tags=["runtime"])
    def capabilities():
        return {
            **agent_kit.features.catalog(),
            "code_registrations": agent_kit.registrations.catalog(),
            "code_catalog_digest": agent_kit.registrations.digest(),
            "structured_schemas": structured_schema_catalog(
                sandbox_schemas=agent_kit.sandboxes.provider_schemas(),
                model_parameter_schemas=agent_kit.model_adapters.parameter_schemas(),
            ),
            "issues": agent_kit.features.issues(agent_kit.config),
            "agent_runtime": {
                "framework": "strands_agents",
                "executables": ["agent", "agent_as_tool", "swarm", "graph", "workflow"],
                "components": [
                    "conversation_manager",
                    "context_manager",
                    "session_manager",
                    "memory_manager",
                    "plugins",
                    "hooks",
                    "interventions",
                    "retry_strategy",
                    "structured_output",
                    "sandbox",
                    "background_tasks",
                ],
            },
        }

    @app.get("/admin/overview", dependencies=admin, tags=["management"])
    def overview():
        config = agent_kit.config
        return {
            "workspace": config.workspace.model_dump(mode="json"),
            "counts": {
                "agents": len(config.agents),
                "orchestrations": len(config.orchestrations),
                "endpoints": len(config.endpoints),
                "models": len(config.resources.models),
                "tools": len(agent_kit.tool_catalog()),
                "databases": len(config.resources.databases),
            },
            "config_generation": config.config_generation,
        }

    def playground_repository():
        repository = agent_kit.storage.playground
        if repository is None:
            raise ConfigurationError("Playground storage is not initialized")
        return repository

    def playground_endpoint_status(endpoint_id: str) -> dict[str, Any]:
        latest = next(
            (item for item in agent_kit.config.endpoints if item.id == endpoint_id),
            None,
        )
        runtime = next(
            (item for item in agent_kit.runtime_config.endpoints if item.id == endpoint_id),
            None,
        )
        source = latest or runtime
        changed = bool(
            latest is not None
            and runtime is not None
            and latest.model_dump(mode="json") != runtime.model_dump(mode="json")
        )
        return {
            "id": endpoint_id,
            "name": source.name if source is not None else endpoint_id,
            "display_name": source.display_name if source is not None else None,
            "description": source.description if source is not None else "",
            "target_type": source.target_type if source is not None else None,
            "target_ref": source.target_ref if source is not None else None,
            "protocols": list(source.protocols) if source is not None else [],
            "exists_in_latest": latest is not None,
            "latest_enabled": bool(latest and latest.enabled),
            "exists_in_runtime": runtime is not None,
            "runtime_enabled": bool(runtime and runtime.enabled),
            "configuration_changed": changed,
            "runnable": bool(runtime and runtime.enabled),
        }

    async def require_playground_conversation(conversation_id: str):
        conversation = await playground_repository().get_conversation(
            agent_kit.config.workspace.id,
            conversation_id,
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
        return conversation

    def require_playground_runtime_endpoint(conversation):
        endpoint = next(
            (
                item
                for item in agent_kit.runtime_config.endpoints
                if item.id == conversation.endpoint_id
            ),
            None,
        )
        if endpoint is None or not endpoint.enabled:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ENDPOINT_NOT_APPLIED",
                    "message": "该端点不在当前运行配置中。请先应用可用的配置",
                },
            )
        return endpoint

    def playground_streaming_response(
        *,
        conversation,
        endpoint,
        runtime_input: dict[str, Any],
        revision: int,
        run_id: str,
        parent_run_id: str | None = None,
        resume_entries: list[dict[str, Any]] | None = None,
    ):
        repository = playground_repository()
        workspace_id = agent_kit.config.workspace.id
        stream = agent_kit.agent_runtime.stream(
            endpoint.id,
            runtime_input,
            user_id=conversation.user_id,
            thread_id=conversation.thread_id,
            run_id=run_id,
            identity=UserIdentity(
                user_id=conversation.user_id,
                attributes={"source": "management_playground"},
            ),
            remote_context=RemoteCallContext.root(
                service_id=workspace_id,
                agent_id=endpoint.id,
            ),
            revision=revision,
            resume_entries=resume_entries,
        )
        collector = PlaygroundTranscriptCollector(run_id)

        async def response_stream():
            try:
                async for event in transcode_runtime_events(
                    stream,
                    thread_id=conversation.thread_id,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    config_generation=revision,
                ):
                    collector.accept(event)
                    yield encode_sse(event)
            finally:
                disconnected = collector.disconnected_entry()
                if disconnected is not None:
                    collector.entries.append(disconnected)
                entries = [
                    item
                    for item in collector.entries
                    if item["kind"] != "message" or item["content"].strip()
                ]
                if entries:
                    try:
                        await repository.append_messages(
                            workspace_id=workspace_id,
                            conversation_id=conversation.id,
                            messages=entries,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to persist Playground transcript for run %s",
                            run_id,
                        )

        return StreamingResponse(
            response_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "X-MeldFabric-Playground-Run-ID": run_id,
            },
        )

    @app.get("/admin/playground/endpoints", dependencies=admin, tags=["playground"])
    def playground_endpoints():
        return {
            "items": [
                playground_endpoint_status(endpoint.id) for endpoint in agent_kit.config.endpoints
            ],
            "latest_generation": agent_kit.config.config_generation,
            "runtime_generation": agent_kit.runtime_config.config_generation,
        }

    @app.get("/admin/playground/conversations", dependencies=admin, tags=["playground"])
    async def playground_conversations(limit: int = 200):
        records = await playground_repository().list_conversations(
            agent_kit.config.workspace.id,
            limit=limit,
        )
        return {
            "items": [
                serialize_conversation(
                    record,
                    playground_endpoint_status(record.endpoint_id),
                )
                for record in records
            ]
        }

    @app.post("/admin/playground/conversations", dependencies=admin, tags=["playground"])
    async def create_playground_conversation(payload: PlaygroundConversationCreateRequest):
        endpoint = next(
            (item for item in agent_kit.config.endpoints if item.id == payload.endpoint_id),
            None,
        )
        if endpoint is None:
            raise HTTPException(status_code=404, detail={"code": "ENDPOINT_NOT_FOUND"})
        status = playground_endpoint_status(endpoint.id)
        if not endpoint.enabled:
            raise HTTPException(status_code=409, detail={"code": "ENDPOINT_DISABLED"})
        if not status["runnable"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ENDPOINT_NOT_APPLIED",
                    "message": "该端点尚未应用。请先应用最新的配置更新",
                },
            )
        record = await playground_repository().create_conversation(
            workspace_id=agent_kit.config.workspace.id,
            endpoint_id=endpoint.id,
            endpoint_name=endpoint.display_name or endpoint.name,
            user_id=playground_user_id(agent_kit.config.workspace.id),
            generation=agent_kit.runtime_config.config_generation,
        )
        return serialize_conversation(record, status)

    @app.get(
        "/admin/playground/conversations/{conversation_id}",
        dependencies=admin,
        tags=["playground"],
    )
    async def get_playground_conversation(conversation_id: str):
        repository = playground_repository()
        workspace_id = agent_kit.config.workspace.id
        record = await repository.get_conversation(workspace_id, conversation_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
        messages = await repository.list_messages(workspace_id, conversation_id)
        return {
            "conversation": serialize_conversation(
                record,
                playground_endpoint_status(record.endpoint_id),
            ),
            "messages": [serialize_message(item) for item in messages],
        }

    @app.delete(
        "/admin/playground/conversations/{conversation_id}",
        dependencies=admin,
        tags=["playground"],
    )
    async def delete_playground_conversation(conversation_id: str):
        conversation = await require_playground_conversation(conversation_id)
        interrupted_run = await agent_kit.agent_runtime.interrupted_run_id(
            endpoint_id=conversation.endpoint_id,
            user_id=conversation.user_id,
            thread_id=conversation.thread_id,
        )
        if interrupted_run is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CONVERSATION_WAITING_FOR_INPUT",
                    "message": "请先响应或结束当前等待。完成后再删除调试历史",
                },
            )
        deleted = await playground_repository().delete_conversation(
            agent_kit.config.workspace.id,
            conversation_id,
        )
        if not deleted:
            raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
        return {
            "deleted_id": conversation_id,
            "agent_session_and_memory_retained": True,
        }

    @app.post(
        "/admin/playground/conversations/{conversation_id}/runs",
        dependencies=admin,
        tags=["playground"],
    )
    async def run_playground_conversation(
        conversation_id: str,
        payload: PlaygroundRunRequest,
    ):
        content = payload.content.strip()
        if not content:
            raise ConfigurationError("Playground message cannot be blank")
        repository = playground_repository()
        workspace_id = agent_kit.config.workspace.id
        conversation = await require_playground_conversation(conversation_id)
        endpoint = require_playground_runtime_endpoint(conversation)
        revision = await agent_kit.agent_runtime.revision_for_stream(
            endpoint_id=endpoint.id,
            user_id=conversation.user_id,
            thread_id=conversation.thread_id,
            resume=False,
        )
        run_id = f"playground_run_{uuid.uuid4().hex}"
        message_id = payload.message_id or f"message_{uuid.uuid4().hex}"
        appended = await repository.append_messages(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            messages=[
                {
                    "id": message_id,
                    "run_id": run_id,
                    "kind": "message",
                    "role": "user",
                    "content": content,
                }
            ],
        )
        if not appended:
            raise HTTPException(
                status_code=409,
                detail={"code": "MESSAGE_ALREADY_EXISTS"},
            )
        stored_messages = await repository.list_messages(workspace_id, conversation.id)
        return playground_streaming_response(
            conversation=conversation,
            endpoint=endpoint,
            runtime_input={"messages": runtime_messages(stored_messages)},
            revision=revision,
            run_id=run_id,
        )

    @app.post(
        "/admin/playground/conversations/{conversation_id}/resume",
        dependencies=admin,
        tags=["playground"],
    )
    async def resume_playground_conversation(
        conversation_id: str,
        payload: PlaygroundResumeRequest,
    ):
        repository = playground_repository()
        workspace_id = agent_kit.config.workspace.id
        conversation = await require_playground_conversation(conversation_id)
        endpoint = require_playground_runtime_endpoint(conversation)
        parent_run_id = await agent_kit.agent_runtime.interrupted_run_id(
            endpoint_id=endpoint.id,
            user_id=conversation.user_id,
            thread_id=conversation.thread_id,
        )
        revision = await agent_kit.agent_runtime.revision_for_stream(
            endpoint_id=endpoint.id,
            user_id=conversation.user_id,
            thread_id=conversation.thread_id,
            resume=True,
        )
        run_id = f"playground_run_{uuid.uuid4().hex}"
        response_value = False if payload.status == "cancelled" else payload.payload
        label = "已拒绝" if payload.status == "cancelled" else str(response_value)
        appended = await repository.append_messages(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            messages=[
                {
                    "id": f"response_{uuid.uuid4().hex}",
                    "run_id": run_id,
                    "kind": "interrupt_response",
                    "role": "user",
                    "content": label,
                    "payload": {
                        "interrupt_id": payload.interrupt_id,
                        "status": payload.status,
                        "response": response_value,
                    },
                }
            ],
        )
        if not appended:
            raise HTTPException(status_code=409, detail={"code": "MESSAGE_ALREADY_EXISTS"})
        return playground_streaming_response(
            conversation=conversation,
            endpoint=endpoint,
            runtime_input={"messages": []},
            revision=revision,
            run_id=run_id,
            parent_run_id=parent_run_id,
            resume_entries=[
                {
                    "interrupt_id": payload.interrupt_id,
                    "status": payload.status,
                    "payload": response_value,
                }
            ],
        )

    @app.post(
        "/admin/playground/conversations/{conversation_id}/interrupt/abandon",
        dependencies=admin,
        tags=["playground"],
    )
    async def abandon_playground_interrupt(conversation_id: str):
        conversation = await require_playground_conversation(conversation_id)
        run_id = await agent_kit.agent_runtime.interrupted_run_id(
            endpoint_id=conversation.endpoint_id,
            user_id=conversation.user_id,
            thread_id=conversation.thread_id,
        )
        if run_id is None:
            raise HTTPException(status_code=409, detail={"code": "INTERRUPT_NOT_FOUND"})
        await agent_kit.agent_runtime.abandon_interrupted_run(
            run_id,
            endpoint_id=conversation.endpoint_id,
            user_id=conversation.user_id,
            thread_id=conversation.thread_id,
        )
        await playground_repository().append_messages(
            workspace_id=agent_kit.config.workspace.id,
            conversation_id=conversation.id,
            messages=[
                {
                    "id": f"status_{uuid.uuid4().hex}",
                    "run_id": run_id,
                    "kind": "status",
                    "role": "system",
                    "content": "已结束等待",
                    "payload": {"status": "abandoned_interrupt", "run_id": run_id},
                }
            ],
        )
        return {"run_id": run_id, "status": "abandoned"}

    @app.post(
        "/admin/playground/conversations/{conversation_id}/runs/{run_id}/cancel",
        dependencies=admin,
        tags=["playground"],
    )
    async def cancel_playground_run(conversation_id: str, run_id: str):
        conversation = await playground_repository().get_conversation(
            agent_kit.config.workspace.id,
            conversation_id,
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail={"code": "CONVERSATION_NOT_FOUND"})
        await agent_kit.agent_runtime.cancel_run(
            run_id,
            endpoint_id=conversation.endpoint_id,
            user_id=conversation.user_id,
            thread_id=conversation.thread_id,
        )
        return {"run_id": run_id, "status": "cancellation_requested"}

    @app.get("/admin/agents", dependencies=admin, tags=["agents"])
    def list_agents():
        return {"items": [item.model_dump(mode="json") for item in agent_kit.config.agents]}

    @app.post("/admin/agents", dependencies=admin, tags=["agents"])
    async def create_agent(payload: EntityMutationRequest):
        raw = {key: value for key, value in payload.resource.items() if key != "id"}
        raw["id"] = new_entity_id("agents")
        try:
            entity = AGENT_ADAPTER.validate_python(raw)
        except ValidationError as exc:
            raise ConfigurationError(
                "Agent configuration is invalid",
                details={"errors": exc.errors(include_url=False)},
            ) from exc
        loaded = await agent_kit.apatch_config(
            entity_patch("agents", [*agent_kit.config.agents, entity]),
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
        )
        return entity.model_dump(mode="json") | {
            "config_generation": loaded.config.config_generation
        }

    @app.put("/admin/agents/{entity_id}", dependencies=admin, tags=["agents"])
    async def put_agent(entity_id: str, payload: EntityMutationRequest):
        raw = {**payload.resource, "id": entity_id}
        try:
            entity = AGENT_ADAPTER.validate_python(raw)
        except ValidationError as exc:
            raise ConfigurationError(
                "Agent configuration is invalid",
                details={"errors": exc.errors(include_url=False)},
            ) from exc
        loaded = await agent_kit.apatch_config(
            entity_patch("agents", replace_entity(agent_kit.config.agents, entity)),
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
        )
        return entity.model_dump(mode="json") | {
            "config_generation": loaded.config.config_generation
        }

    @app.delete("/admin/agents/{entity_id}", dependencies=admin, tags=["agents"])
    async def delete_agent(
        entity_id: str,
        expected_catalog_digest: CatalogDigest,
        expected_generation: int | None = None,
    ):
        patch, removed_endpoints = deletion_patch(agent_kit.config, "agents", entity_id)
        loaded = await agent_kit.apatch_config(
            patch,
            expected_generation=expected_generation,
            expected_catalog_digest=expected_catalog_digest,
        )
        repository = await agent_kit.storage.require_secrets(agent_kit.config)
        for endpoint_id in removed_endpoints:
            await repository.revoke_keys_for_agent(endpoint_id)
        return {
            "deleted_id": entity_id,
            "config_generation": loaded.config.config_generation,
            "removed_endpoint_ids": sorted(removed_endpoints),
            "durable_state_retained": True,
        }

    @app.get("/admin/orchestrations", dependencies=admin, tags=["orchestrations"])
    def list_orchestrations():
        return {"items": [item.model_dump(mode="json") for item in agent_kit.config.orchestrations]}

    @app.post("/admin/orchestrations", dependencies=admin, tags=["orchestrations"])
    async def create_orchestration(payload: EntityMutationRequest):
        raw = {key: value for key, value in payload.resource.items() if key != "id"}
        raw["id"] = new_entity_id("orchestrations")
        try:
            entity = ORCHESTRATION_ADAPTER.validate_python(raw)
        except ValidationError as exc:
            raise ConfigurationError(
                "orchestration configuration is invalid",
                details={"errors": exc.errors(include_url=False)},
            ) from exc
        validate_graph_conditions(agent_kit, entity)
        loaded = await agent_kit.apatch_config(
            entity_patch("orchestrations", [*agent_kit.config.orchestrations, entity]),
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
        )
        return entity.model_dump(mode="json") | {
            "config_generation": loaded.config.config_generation
        }

    @app.put("/admin/orchestrations/{entity_id}", dependencies=admin, tags=["orchestrations"])
    async def put_orchestration(entity_id: str, payload: EntityMutationRequest):
        try:
            entity = ORCHESTRATION_ADAPTER.validate_python({**payload.resource, "id": entity_id})
        except ValidationError as exc:
            raise ConfigurationError(
                "orchestration configuration is invalid",
                details={"errors": exc.errors(include_url=False)},
            ) from exc
        validate_graph_conditions(agent_kit, entity, allow_unavailable=entity.enabled is False)
        loaded = await agent_kit.apatch_config(
            entity_patch("orchestrations", replace_entity(agent_kit.config.orchestrations, entity)),
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
        )
        return entity.model_dump(mode="json") | {
            "config_generation": loaded.config.config_generation
        }

    @app.delete("/admin/orchestrations/{entity_id}", dependencies=admin, tags=["orchestrations"])
    async def delete_orchestration(
        entity_id: str,
        expected_catalog_digest: CatalogDigest,
        expected_generation: int | None = None,
    ):
        patch, removed_endpoints = deletion_patch(agent_kit.config, "orchestrations", entity_id)
        loaded = await agent_kit.apatch_config(
            patch,
            expected_generation=expected_generation,
            expected_catalog_digest=expected_catalog_digest,
        )
        repository = await agent_kit.storage.require_secrets(agent_kit.config)
        for endpoint_id in removed_endpoints:
            await repository.revoke_keys_for_agent(endpoint_id)
        return {
            "deleted_id": entity_id,
            "config_generation": loaded.config.config_generation,
            "removed_endpoint_ids": sorted(removed_endpoints),
        }

    @app.get("/admin/endpoints", dependencies=admin, tags=["endpoints"])
    def list_endpoints():
        return {"items": [item.model_dump(mode="json") for item in agent_kit.config.endpoints]}

    @app.post("/admin/endpoints", dependencies=admin, tags=["endpoints"])
    async def create_endpoint(payload: EntityMutationRequest):
        raw = {key: value for key, value in payload.resource.items() if key != "id"}
        raw["id"] = new_entity_id("endpoints")
        try:
            entity = ENDPOINT_ADAPTER.validate_python(raw)
        except ValidationError as exc:
            raise ConfigurationError(
                "endpoint configuration is invalid",
                details={"errors": exc.errors(include_url=False)},
            ) from exc
        loaded = await agent_kit.apatch_config(
            entity_patch("endpoints", [*agent_kit.config.endpoints, entity]),
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
        )
        return entity.model_dump(mode="json") | {
            "config_generation": loaded.config.config_generation
        }

    @app.put("/admin/endpoints/{entity_id}", dependencies=admin, tags=["endpoints"])
    async def put_endpoint(entity_id: str, payload: EntityMutationRequest):
        try:
            entity = ENDPOINT_ADAPTER.validate_python({**payload.resource, "id": entity_id})
        except ValidationError as exc:
            raise ConfigurationError(
                "endpoint configuration is invalid",
                details={"errors": exc.errors(include_url=False)},
            ) from exc
        loaded = await agent_kit.apatch_config(
            entity_patch("endpoints", replace_entity(agent_kit.config.endpoints, entity)),
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
        )
        return entity.model_dump(mode="json") | {
            "config_generation": loaded.config.config_generation
        }

    @app.delete("/admin/endpoints/{entity_id}", dependencies=admin, tags=["endpoints"])
    async def delete_endpoint(
        entity_id: str,
        expected_catalog_digest: CatalogDigest,
        expected_generation: int | None = None,
    ):
        patch, removed_endpoints = deletion_patch(agent_kit.config, "endpoints", entity_id)
        loaded = await agent_kit.apatch_config(
            patch,
            expected_generation=expected_generation,
            expected_catalog_digest=expected_catalog_digest,
        )
        repository = await agent_kit.storage.require_secrets(agent_kit.config)
        for endpoint_id in removed_endpoints:
            await repository.revoke_keys_for_agent(endpoint_id)
        return {
            "deleted_id": entity_id,
            "config_generation": loaded.config.config_generation,
        }

    @app.get("/admin/resources/{collection}", dependencies=admin, tags=["resources"])
    def list_resources(collection: str):
        if collection == "tools":
            return {"items": agent_kit.tool_catalog()}
        if collection not in RESOURCE_ADAPTERS:
            raise HTTPException(status_code=404, detail={"code": "RESOURCE_TYPE_NOT_FOUND"})
        return {
            "items": [
                item.model_dump(mode="json")
                for item in getattr(agent_kit.config.resources, collection)
            ]
        }

    @app.put("/admin/resources/{collection}/{entity_id}", dependencies=admin, tags=["resources"])
    async def put_resource(collection: str, entity_id: str, payload: EntityMutationRequest):
        adapter = RESOURCE_ADAPTERS.get(collection)
        if adapter is None:
            raise HTTPException(status_code=404, detail={"code": "RESOURCE_TYPE_NOT_FOUND"})
        try:
            entity = adapter.validate_python({**payload.resource, "id": entity_id})
        except ValidationError as exc:
            raise ConfigurationError(
                "resource configuration is invalid",
                details={"errors": exc.errors(include_url=False)},
            ) from exc
        validate_registered_resource(agent_kit, entity, allow_unavailable=entity.enabled is False)
        values = list(getattr(agent_kit.config.resources, collection))
        loaded = await agent_kit.apatch_config(
            entity_patch(collection, replace_entity(values, entity)),
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
        )
        return entity.model_dump(mode="json") | {
            "config_generation": loaded.config.config_generation
        }

    @app.post("/admin/resources/{collection}", dependencies=admin, tags=["resources"])
    async def create_resource(collection: str, payload: EntityMutationRequest):
        if collection == "tools":
            raise HTTPException(status_code=405, detail={"code": "CODE_MANAGED_RESOURCE"})
        adapter = RESOURCE_ADAPTERS.get(collection)
        if adapter is None:
            raise HTTPException(status_code=404, detail={"code": "RESOURCE_TYPE_NOT_FOUND"})
        raw = {key: value for key, value in payload.resource.items() if key != "id"}
        raw["id"] = new_entity_id(collection)
        try:
            entity = adapter.validate_python(raw)
        except ValidationError as exc:
            raise ConfigurationError(
                "resource configuration is invalid",
                details={"errors": exc.errors(include_url=False)},
            ) from exc
        validate_registered_resource(agent_kit, entity)
        values = list(getattr(agent_kit.config.resources, collection))
        loaded = await agent_kit.apatch_config(
            entity_patch(collection, [*values, entity]),
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
        )
        return entity.model_dump(mode="json") | {
            "config_generation": loaded.config.config_generation
        }

    @app.delete(
        "/admin/resources/{collection}/{entity_id}",
        dependencies=admin,
        tags=["resources"],
    )
    async def delete_resource(
        collection: str,
        entity_id: str,
        expected_catalog_digest: CatalogDigest,
        expected_generation: int | None = None,
    ):
        patch, removed_endpoints = deletion_patch(agent_kit.config, collection, entity_id)
        loaded = await agent_kit.apatch_config(
            patch,
            expected_generation=expected_generation,
            expected_catalog_digest=expected_catalog_digest,
        )
        repository = await agent_kit.storage.require_secrets(agent_kit.config)
        for endpoint_id in removed_endpoints:
            await repository.revoke_keys_for_agent(endpoint_id)
        return {
            "deleted_id": entity_id,
            "config_generation": loaded.config.config_generation,
            "removed_endpoint_ids": sorted(removed_endpoints),
        }

    @app.patch("/admin/resources/tools/{tool_id}", dependencies=admin, tags=["resources"])
    async def update_tool(tool_id: str, payload: ToolOverrideRequest):
        catalog = {item["id"]: item for item in agent_kit.tool_catalog()}
        if tool_id not in catalog:
            raise HTTPException(status_code=404, detail={"code": "TOOL_NOT_FOUND"})
        current = next(
            (item for item in agent_kit.config.resources.tools if item.id == tool_id), None
        )
        raw = (
            current.model_dump(mode="json")
            if current
            else {
                "id": tool_id,
                "name": catalog[tool_id]["name"],
                "python_path": catalog[tool_id]["python_path"],
            }
        )
        for field in (
            "enabled",
            "display_name",
            "description",
            "requires_sandbox",
            "parameter_descriptions",
        ):
            value = getattr(payload, field)
            if value is not None:
                raw[field] = value
        entity = ToolResource.model_validate(raw)
        loaded = await agent_kit.apatch_config(
            entity_patch("tools", replace_entity(agent_kit.config.resources.tools, entity)),
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
        )
        return entity.model_dump(mode="json") | {
            "config_generation": loaded.config.config_generation
        }

    @app.get("/admin/resource-references/{target_id}", dependencies=admin, tags=["management"])
    def references(target_id: str):
        return resource_references(agent_kit.config.model_dump(mode="json"), target_id)

    @app.get(
        "/admin/agents/{agent_id}/remote-topology",
        dependencies=admin,
        tags=["agents"],
    )
    async def agent_remote_topology(agent_id: str, max_depth: int = 10):
        agent = next((item for item in agent_kit.config.agents if item.id == agent_id), None)
        if agent is None:
            raise HTTPException(status_code=404, detail={"code": "AGENT_NOT_FOUND"})
        return jsonable(
            await agent_kit.remote_agents.topology(
                agent_kit.config,
                agent,
                max_depth=max(0, min(max_depth, 100)),
            )
        )

    @app.post("/admin/endpoints/{endpoint_id}/keys", dependencies=admin, tags=["endpoints"])
    async def create_endpoint_key(endpoint_id: str, payload: CreateEndpointKeyRequest):
        endpoint = next(
            (item for item in agent_kit.config.endpoints if item.id == endpoint_id), None
        )
        if endpoint is None:
            raise HTTPException(status_code=404, detail={"code": "ENDPOINT_NOT_FOUND"})
        repository = await agent_kit.storage.require_secrets(agent_kit.config)
        scopes = payload.scopes or (
            ["invoke"] if payload.kind == "agent_api" else ["discover", "invoke", "topology:read"]
        )
        created = await repository.create_inbound_key(
            kind=payload.kind,
            agent_id=endpoint_id,
            scopes=scopes,
            expires_at=payload.expires_at,
        )
        return {
            "id": created.id,
            "value": created.value,
            "prefix": created.prefix,
            "endpoint_id": endpoint_id,
            "kind": payload.kind,
            "scopes": scopes,
        }

    @app.get("/admin/endpoints/{endpoint_id}/keys", dependencies=admin, tags=["endpoints"])
    async def list_endpoint_keys(endpoint_id: str, include_inactive: bool = False):
        repository = await agent_kit.storage.require_secrets(agent_kit.config)
        records = await repository.list_keys(
            agent_id=endpoint_id,
            statuses=None if include_inactive else {"active"},
        )
        return {
            "items": [
                {
                    "id": item.id,
                    "prefix": item.key_prefix,
                    "kind": item.kind,
                    "scopes": item.scopes,
                    "status": item.status,
                    "created_at": item.created_at,
                    "expires_at": item.expires_at,
                    "last_used_at": item.last_used_at,
                }
                for item in records
            ]
        }

    @app.patch("/admin/keys/{key_id}", dependencies=admin, tags=["endpoints"])
    async def update_key(key_id: str, payload: KeyStatusRequest):
        repository = await agent_kit.storage.require_secrets(agent_kit.config)
        if not await repository.set_key_status(key_id, payload.status):
            raise HTTPException(status_code=404, detail={"code": "KEY_NOT_FOUND"})
        return {"id": key_id, "status": payload.status}

    @app.put(
        "/admin/resources/model-providers/{provider_id}/secret",
        dependencies=admin,
        tags=["models"],
    )
    async def provider_secret(provider_id: str, payload: SecretRequest):
        provider = next(
            (item for item in agent_kit.config.resources.model_providers if item.id == provider_id),
            None,
        )
        if provider is None:
            raise HTTPException(status_code=404, detail={"code": "MODEL_PROVIDER_NOT_FOUND"})
        secret_ref = provider.api_key_secret_ref or f"secret_model_{uuid.uuid4().hex}"
        repository = await agent_kit.storage.require_secrets(agent_kit.config)
        secret_write = repository.prepare_secret_write(
            secret_ref,
            kind="model_provider_api_key",
            value=payload.value.get_secret_value(),
        )
        updated = provider.model_copy(update={"api_key_secret_ref": secret_ref})
        loaded = await agent_kit.apatch_config(
            entity_patch(
                "model_providers",
                replace_entity(agent_kit.config.resources.model_providers, updated),
            ),
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
            secret_writes=(secret_write,),
        )
        return {
            "provider_id": provider_id,
            "configured": True,
            "config_generation": loaded.config.config_generation,
        }

    @app.put(
        "/admin/resources/{collection}/{resource_id}/secret",
        dependencies=admin,
        tags=["security"],
    )
    async def resource_secret(
        collection: str,
        resource_id: str,
        payload: SecretRequest,
    ):
        secret_fields = {
            "mcp_servers": ("credential_secret_ref", "mcp_credential"),
            "remote_agents": ("credential_secret_ref", "remote_agent_key"),
            "observability_providers": ("secret_key_ref", "observability_secret"),
        }
        if collection not in secret_fields:
            raise HTTPException(status_code=404, detail={"code": "SECRET_RESOURCE_TYPE_NOT_FOUND"})
        values = list(getattr(agent_kit.config.resources, collection))
        resource = next((item for item in values if item.id == resource_id), None)
        if resource is None:
            raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
        field, kind = secret_fields[collection]
        secret_ref = getattr(resource, field) or f"secret_{kind}_{uuid.uuid4().hex}"
        repository = await agent_kit.storage.require_secrets(agent_kit.config)
        secret_write = repository.prepare_secret_write(
            secret_ref,
            kind=kind,
            value=payload.value.get_secret_value(),
        )
        updated = resource.model_copy(update={field: secret_ref})
        loaded = await agent_kit.apatch_config(
            entity_patch(collection, replace_entity(values, updated)),
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
            secret_writes=(secret_write,),
        )
        return {
            "resource_id": resource_id,
            "configured": True,
            "config_generation": loaded.config.config_generation,
        }

    @app.get(
        "/admin/resources/databases/{database_id}/capabilities",
        dependencies=admin,
        tags=["resources"],
    )
    def database_capabilities(database_id: str):
        database = next(
            (item for item in agent_kit.config.resources.databases if item.id == database_id),
            None,
        )
        if database is None:
            raise HTTPException(status_code=404, detail={"code": "DATABASE_NOT_FOUND"})
        capabilities = agent_kit.capabilities.database_capabilities(database)
        return {
            "database_id": database_id,
            "capabilities": sorted(item.value for item in capabilities),
        }

    @app.post(
        "/admin/resources/databases/{database_id}/test",
        dependencies=admin,
        tags=["resources"],
    )
    async def test_database(database_id: str):
        resource = next(
            (item for item in agent_kit.config.resources.databases if item.id == database_id),
            None,
        )
        if resource is None:
            raise HTTPException(status_code=404, detail={"code": "DATABASE_NOT_FOUND"})
        result = await agent_kit.storage.test_database(resource)
        return {
            **result,
            "database_id": database_id,
            "capabilities": sorted(
                item.value for item in agent_kit.capabilities.database_capabilities(resource)
            ),
        }

    @app.post(
        "/admin/resources/model-providers/discover-preview", dependencies=admin, tags=["models"]
    )
    async def discover_models(payload: ProviderPreviewRequest):
        adapter = agent_kit.model_adapters.get(payload.resource.adapter)
        api_key = payload.api_key.get_secret_value() if payload.api_key else None
        if api_key is None and payload.resource.api_key_secret_ref:
            repository = await agent_kit.storage.require_secrets(agent_kit.config)
            api_key = await repository.get_secret(payload.resource.api_key_secret_ref)
        items = await adapter.discover(payload.resource, api_key=api_key)
        return {"items": [jsonable(item) for item in items]}

    @app.post("/admin/resources/model-providers/test-preview", dependencies=admin, tags=["models"])
    async def test_model(payload: ModelPreviewRequest):
        adapter = agent_kit.model_adapters.get(payload.resource.adapter)
        api_key = payload.api_key.get_secret_value() if payload.api_key else None
        if api_key is None and payload.resource.api_key_secret_ref:
            repository = await agent_kit.storage.require_secrets(agent_kit.config)
            api_key = await repository.get_secret(payload.resource.api_key_secret_ref)
        return await probe_model(adapter, payload.resource, payload.model, api_key=api_key)

    @app.post(
        "/admin/resources/skill-sources/{source_id}/scan",
        dependencies=admin,
        tags=["extensions"],
    )
    def scan_skill_source(source_id: str):
        source = next(
            (item for item in agent_kit.config.resources.skill_sources if item.id == source_id),
            None,
        )
        if source is None:
            raise HTTPException(status_code=404, detail={"code": "SKILL_SOURCE_NOT_FOUND"})
        return agent_kit.skills.scan_source(source).as_dict()

    @app.post(
        "/admin/resources/mcp-servers/{server_id}/test", dependencies=admin, tags=["extensions"]
    )
    async def test_mcp(server_id: str):
        server = next(
            (item for item in agent_kit.config.resources.mcp_servers if item.id == server_id),
            None,
        )
        if server is None:
            raise HTTPException(status_code=404, detail={"code": "MCP_SERVER_NOT_FOUND"})
        return jsonable(await agent_kit.mcp.test_server(agent_kit.config.config_generation, server))

    @app.post("/admin/resources/remote-agents/discover", dependencies=admin, tags=["extensions"])
    async def discover_remote(payload: RemoteDiscoveryRequest):
        return (await _discover_remote(payload)).model_dump(mode="json")

    @app.post(
        "/admin/resources/remote-agents/{resource_id}/discover",
        dependencies=admin,
        tags=["extensions"],
    )
    async def discover_saved_remote(resource_id: str):
        resource = next(
            (item for item in agent_kit.config.resources.remote_agents if item.id == resource_id),
            None,
        )
        if resource is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "REMOTE_AGENT_NOT_FOUND"},
            )
        descriptor = await agent_kit.remote_agents.discover_resource(
            agent_kit.config.config_generation,
            resource,
            refresh=True,
        )
        return descriptor.model_dump(mode="json")

    @app.get("/admin/config/snapshots", dependencies=admin, tags=["configuration"])
    async def snapshots():
        return {
            "items": [
                jsonable(item)
                for item in await asyncio.to_thread(agent_kit.repository.list_snapshots)
            ]
        }

    @app.post("/admin/config/snapshots", dependencies=admin, tags=["configuration"])
    async def create_snapshot(payload: SnapshotCreateRequest):
        return jsonable(
            await agent_kit.acreate_config_snapshot(
                label=payload.label,
                note=payload.note,
                expected_generation=payload.expected_generation,
            )
        )

    @app.post(
        "/admin/config/snapshots/{version}/restore", dependencies=admin, tags=["configuration"]
    )
    async def restore_snapshot(version: int, payload: SnapshotRestoreRequest):
        if not payload.confirm:
            raise ConfigurationError("snapshot restore requires explicit confirmation")
        loaded = await agent_kit.arestore_config_snapshot(
            version,
            expected_generation=payload.expected_generation,
            expected_catalog_digest=payload.expected_catalog_digest,
        )
        return loaded.config.model_dump(mode="json")

    @app.delete("/admin/config/snapshots/{version}", dependencies=admin, tags=["configuration"])
    async def delete_snapshot(version: int):
        await asyncio.to_thread(agent_kit.repository.delete_snapshot, version)
        return {"deleted_version": version}

    @app.put("/admin/security/admin-key", dependencies=admin, tags=["security"])
    async def update_admin_key(
        payload: AdminKeyUpdateRequest, current_key: str = Depends(require_admin_key)
    ):
        if not payload.confirm:
            raise ConfigurationError("administrator key update requires confirmation")
        await asyncio.to_thread(
            agent_kit.repository.set_admin_key,
            current_key=current_key,
            new_key=payload.new_key.get_secret_value(),
        )
        return {"updated": True}

    @app.post("/admin/system/backups", dependencies=admin, tags=["configuration"])
    async def backup_control_database(payload: BackupRequest):
        if not await agent_kit.verify_admin_key(payload.admin_key.get_secret_value()):
            raise HTTPException(status_code=401, detail={"code": "ADMIN_UNAUTHORIZED"})
        target_spec = ControlDatabaseSpec.resolve(
            payload.url.get_secret_value(),
            state_dir=agent_kit.repository.state_dir,
            options=payload.options,
        )
        if target_spec.engine != payload.engine:
            raise ConfigurationError(
                "backup database type does not match the connection URL",
                details={
                    "selected_engine": payload.engine,
                    "url_engine": target_spec.engine,
                },
            )
        service = ControlBackupService(agent_kit.repository.engine, agent_kit.repository.database)
        result = await asyncio.to_thread(
            service.backup,
            payload.url.get_secret_value(),
            target_options=payload.options,
            overwrite=payload.overwrite,
        )
        return jsonable(result)

    async def verify_endpoint_key(
        authorization: str | None,
        *,
        scope: str,
        endpoint_id: str | None = None,
        kind: str | None = None,
        protocol: str | None = None,
    ):
        raw = (
            authorization[7:].strip()
            if authorization and authorization.lower().startswith("bearer ")
            else None
        )
        repository = agent_kit.storage.secrets
        record = (
            await repository.verify_key(raw, required_scope=scope) if raw and repository else None
        )
        if record is None:
            raise HTTPException(status_code=401, detail={"code": "ENDPOINT_KEY_INVALID"})
        if endpoint_id is not None and record.agent_id != endpoint_id:
            raise HTTPException(status_code=403, detail={"code": "ENDPOINT_KEY_SCOPE_MISMATCH"})
        if kind is not None and record.kind != kind:
            raise HTTPException(status_code=403, detail={"code": "REMOTE_KEY_REQUIRED"})
        endpoint = next(
            (item for item in agent_kit.runtime_config.endpoints if item.id == record.agent_id),
            None,
        )
        if endpoint is None or not endpoint.enabled:
            raise HTTPException(status_code=404, detail={"code": "ENDPOINT_NOT_FOUND"})
        if protocol is not None and protocol not in endpoint.protocols:
            raise HTTPException(status_code=403, detail={"code": "PROTOCOL_NOT_PUBLISHED"})
        return record, endpoint

    async def invoke_endpoint(
        payload: InvokeRequest,
        request: Request,
        authorization: str | None,
        x_user_id: str | None,
        expected_endpoint_id: str | None = None,
        remote_context: RemoteCallContext | None = None,
        remote_only: bool = False,
    ):
        _record, endpoint = await verify_endpoint_key(
            authorization,
            scope="invoke",
            endpoint_id=expected_endpoint_id,
            kind="remote_access" if remote_only else None,
            protocol="meldfabric_remote" if remote_only else "rest",
        )
        identity = await agent_kit.identity_resolver.resolve(
            request, claimed_user_id=x_user_id or payload.user_id
        )
        context = remote_context or RemoteCallContext.root(
            service_id=agent_kit.runtime_config.workspace.id, agent_id=endpoint.id
        )
        result, revision, run_id = await agent_kit.agent_runtime.invoke(
            endpoint.id,
            payload.messages,
            user_id=identity.user_id,
            thread_id=payload.thread_id,
            identity=identity,
            remote_context=context,
        )
        return {
            "run_id": run_id,
            "endpoint_id": endpoint.id,
            "user_id": identity.user_id,
            "thread_id": payload.thread_id,
            "config_generation": revision,
            "status": "interrupted" if result.get("interrupts") else "completed",
            "result": result,
        }

    @app.post("/api/invoke", tags=["execution"])
    async def invoke_by_key(
        payload: InvokeRequest,
        request: Request,
        authorization: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
    ):
        return await invoke_endpoint(payload, request, authorization, x_user_id)

    @app.post("/api/endpoints/{endpoint_id}/runs", tags=["execution"])
    async def invoke_named(
        endpoint_id: str,
        payload: InvokeRequest,
        request: Request,
        authorization: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
    ):
        return await invoke_endpoint(
            payload, request, authorization, x_user_id, expected_endpoint_id=endpoint_id
        )

    async def agui_endpoint(
        payload: AGUIRunInput,
        request: Request,
        authorization: str | None,
        x_user_id: str | None,
        expected_endpoint_id: str | None = None,
    ):
        _record, endpoint = await verify_endpoint_key(
            authorization, scope="invoke", endpoint_id=expected_endpoint_id, protocol="ag_ui"
        )
        identity = await agent_kit.identity_resolver.resolve(
            request, claimed_user_id=payload.user_id(x_user_id)
        )
        resume = payload.resume_entries()
        revision = await agent_kit.agent_runtime.revision_for_stream(
            endpoint_id=endpoint.id,
            user_id=identity.user_id,
            thread_id=payload.thread_id,
            resume=resume is not None,
        )
        stream = agent_kit.agent_runtime.stream(
            endpoint.id,
            payload.runtime_input(),
            user_id=identity.user_id,
            thread_id=payload.thread_id,
            run_id=payload.run_id,
            identity=identity,
            remote_context=RemoteCallContext.root(
                service_id=agent_kit.runtime_config.workspace.id, agent_id=endpoint.id
            ),
            revision=revision,
            resume_entries=resume,
        )
        return StreamingResponse(
            transcode_runtime_stream(
                stream,
                thread_id=payload.thread_id,
                run_id=payload.run_id,
                parent_run_id=payload.parent_run_id,
                config_generation=revision,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/ag-ui", tags=["ag-ui"])
    async def agui(
        payload: AGUIRunInput,
        request: Request,
        authorization: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
    ):
        return await agui_endpoint(payload, request, authorization, x_user_id)

    @app.post("/api/endpoints/{endpoint_id}/ag-ui", tags=["ag-ui"])
    async def agui_named(
        endpoint_id: str,
        payload: AGUIRunInput,
        request: Request,
        authorization: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
    ):
        return await agui_endpoint(payload, request, authorization, x_user_id, endpoint_id)

    @app.post("/api/runs/{run_id}/cancel", tags=["execution"])
    async def cancel_run(
        run_id: str,
        request: Request,
        thread_id: str | None = None,
        authorization: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
    ):
        _record, endpoint = await verify_endpoint_key(authorization, scope="invoke")
        identity = await agent_kit.identity_resolver.resolve(request, claimed_user_id=x_user_id)
        await agent_kit.agent_runtime.cancel_run(
            run_id, endpoint_id=endpoint.id, user_id=identity.user_id, thread_id=thread_id
        )
        return {"run_id": run_id, "status": "cancellation_requested"}

    async def run_remote(
        payload: RemoteInvokeRequest | RemoteResumeRequest, authorization: str | None
    ):
        _record, endpoint = await verify_endpoint_key(
            authorization, scope="invoke", kind="remote_access", protocol="meldfabric_remote"
        )
        resume = (
            [item.model_dump(mode="json") for item in payload.resume]
            if isinstance(payload, RemoteResumeRequest)
            else None
        )
        revision = await agent_kit.agent_runtime.revision_for_stream(
            endpoint_id=endpoint.id,
            user_id=payload.user_id,
            thread_id=payload.thread_id,
            resume=resume is not None,
        )
        run_id = f"run_{uuid.uuid4().hex}"
        completed: NativeExecutionResult | None = None
        async for event in agent_kit.agent_runtime.stream(
            endpoint.id,
            {"messages": payload.messages if isinstance(payload, RemoteInvokeRequest) else []},
            user_id=payload.user_id,
            thread_id=payload.thread_id,
            run_id=run_id,
            remote_context=payload.remote_context.enter(
                f"{agent_kit.runtime_config.workspace.id}/{endpoint.id}"
            ),
            revision=revision,
            resume_entries=resume,
        ):
            if isinstance(event, NativeExecutionResult):
                completed = event
        if completed is None:
            raise ConfigurationError("remote execution finished without a result")
        return {
            "run_id": run_id,
            "agent_id": endpoint.id,
            "user_id": payload.user_id,
            "thread_id": payload.thread_id,
            "config_generation": revision,
            "status": "interrupted" if completed.interrupts else "completed",
            "result": completed.as_dict(),
        }

    @app.post("/api/remote/invoke", tags=["remote"])
    async def remote_invoke(
        payload: RemoteInvokeRequest, authorization: str | None = Header(default=None)
    ):
        return await run_remote(payload, authorization)

    @app.post("/api/remote/resume", tags=["remote"])
    async def remote_resume(
        payload: RemoteResumeRequest, authorization: str | None = Header(default=None)
    ):
        return await run_remote(payload, authorization)

    @app.post("/api/remote/cancel", tags=["remote"])
    async def remote_cancel(
        payload: RemoteCancelRequest, authorization: str | None = Header(default=None)
    ):
        _record, endpoint = await verify_endpoint_key(
            authorization, scope="invoke", kind="remote_access", protocol="meldfabric_remote"
        )
        await agent_kit.agent_runtime.abandon_interrupted_run(
            payload.run_id,
            endpoint_id=endpoint.id,
            user_id=payload.user_id,
            thread_id=payload.thread_id,
        )
        return {"run_id": payload.run_id, "status": "cancelled"}

    @app.get("/api/remote-topology", tags=["remote"])
    async def remote_topology(
        max_depth: int = 10,
        authorization: str | None = Header(default=None),
    ):
        _record, endpoint = await verify_endpoint_key(
            authorization,
            scope="topology:read",
            kind="remote_access",
            protocol="meldfabric_remote",
        )
        agent = _primary_agent(agent_kit.runtime_config, endpoint)
        return jsonable(
            await agent_kit.remote_agents.topology(
                agent_kit.runtime_config,
                agent,
                max_depth=max(0, min(max_depth, 100)),
                root_agent_id=endpoint.id,
                root_name=endpoint.name,
            )
        )

    @app.get("/.well-known/meldfabric-agent.json", tags=["discovery"])
    async def discover(authorization: str | None = Header(default=None)):
        _record, endpoint = await verify_endpoint_key(
            authorization, scope="discover", kind="remote_access", protocol="meldfabric_remote"
        )
        agent = _primary_agent(agent_kit.runtime_config, endpoint)
        return {
            "schema_version": "2",
            "service_id": agent_kit.runtime_config.workspace.id,
            "agent_id": endpoint.id,
            "name": endpoint.name,
            "display_name": endpoint.display_name,
            "description": endpoint.description or agent.description,
            "status": "active",
            "config_generation": agent_kit.runtime_config.config_generation,
            "protocols": [
                "meldfabric_remote/2",
                *([A2A_PROTOCOL] if "a2a" in endpoint.protocols else []),
            ],
            "capabilities": {"invoke": True, "topology": True},
        }

    @app.get("/.well-known/agent-card.json", tags=["a2a"])
    async def a2a_card(
        request: Request,
        authorization: str | None = Header(default=None),
        if_none_match: str | None = Header(default=None),
    ):
        record, endpoint = await verify_endpoint_key(
            authorization, scope="discover", kind="remote_access", protocol="a2a"
        )
        card = await a2a_gateway.agent_card(
            record, endpoint=f"{str(request.base_url).rstrip('/')}/a2a/{endpoint.id}"
        )
        canonical = json.dumps(card, sort_keys=True, separators=(",", ":"))
        etag = f'"{hashlib.sha256(canonical.encode()).hexdigest()}"'
        headers = {
            "Cache-Control": "private, max-age=60",
            "ETag": etag,
            "Vary": "Authorization",
        }
        if if_none_match == etag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(card, headers=headers)

    app.mount("/a2a", a2a_gateway, name="a2a")

    @app.get("/ready", tags=["system"])
    async def ready():
        snapshot = agent_kit.snapshots.current
        endpoints = []
        for endpoint in snapshot.config.endpoints:
            reasons: list[dict[str, Any]] = []
            if not endpoint.enabled:
                reasons.append({"code": "ENDPOINT_DISABLED", "message": "endpoint is disabled"})
            elif endpoint.enabled:
                try:
                    await agent_kit.agent_runtime.compiled_endpoint(snapshot.revision, endpoint.id)
                except Exception as exc:
                    reasons.append(
                        {
                            "code": getattr(exc, "code", "ENDPOINT_NOT_READY"),
                            "message": getattr(exc, "message", str(exc)),
                        }
                    )
            endpoints.append(
                {
                    "id": endpoint.id,
                    "name": endpoint.display_name or endpoint.name,
                    "enabled": endpoint.enabled,
                    "ready": endpoint.enabled and not reasons,
                    "reasons": reasons,
                }
            )
        return {
            "status": "degraded"
            if any(item["enabled"] and not item["ready"] for item in endpoints)
            or agent_kit.storage.issues
            else "ready",
            "config_generation": snapshot.revision,
            "storage": agent_kit.storage.status(),
            "persistence": agent_kit.persistence.status(),
            "tool_registration_issues": [
                jsonable(item) for item in agent_kit.tool_registry.issues(snapshot.revision)
            ],
            "endpoints": endpoints,
        }

    return app
