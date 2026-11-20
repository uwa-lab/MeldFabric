"""Native Strands/A2A transport backed by the MeldFabric runtime and control database."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar, cast

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import TaskStore, TaskUpdater
from a2a.types import (
    AgentSkill,
    DataPart,
    FilePart,
    HTTPAuthSecurityScheme,
    InvalidParamsError,
    Message,
    Part,
    Role,
    SecurityScheme,
    Task,
    TaskState,
    TextPart,
)
from a2a.utils import new_task
from a2a.utils.errors import ServerError
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send
from strands import Agent
from strands.agent import AgentMetadata
from strands.models import Model
from strands.multiagent.a2a import A2AServer
from strands.types.content import Message as StrandsMessage
from strands.types.content import SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

from meldfabric._version import __version__
from meldfabric.agents.types import NativeExecutionResult
from meldfabric.domain.serialization import jsonable
from meldfabric.errors import RunNotFoundError
from meldfabric.remote.models import RemoteCallContext

if TYPE_CHECKING:
    from meldfabric.app import AgentKit
    from meldfabric.storage import StoredA2ATask, SystemRepository

logger = logging.getLogger(__name__)

# Strands Agents 1.64 uses the official A2A 0.3 SDK. Keep this value tied to
# the protocol actually implemented by the installed SDK.
A2A_VERSION = "0.3.0"
A2A_PROTOCOL = "a2a/0.3"
StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)


class _AgentCardModel(Model):
    """Non-invokable model used only for Strands' native Agent Card metadata."""

    def __init__(self) -> None:
        self._config: dict[str, Any] = {"model_id": "meldfabric-agent-card-only"}

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)

    async def stream(
        self,
        messages: list[StrandsMessage],
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        cancel_signal: threading.Event | None = None,
        agent_metadata: AgentMetadata | None = None,
        structured_output_model: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        del (
            messages,
            tool_specs,
            system_prompt,
            tool_choice,
            system_prompt_content,
            invocation_state,
            cancel_signal,
            agent_metadata,
            structured_output_model,
            kwargs,
        )
        if False:  # pragma: no cover - makes this method an async generator
            yield cast(StreamEvent, {})
        raise RuntimeError("the Agent Card model cannot be invoked")

    async def structured_output(
        self,
        output_model: type[StructuredOutput],
        prompt: list[StrandsMessage],
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, StructuredOutput | Any], None]:
        del output_model, prompt, system_prompt, kwargs
        if False:  # pragma: no cover - makes this method an async generator
            yield {}
        raise RuntimeError("the Agent Card model cannot be invoked")


class MeldFabricTaskStore(TaskStore):
    """Persist official A2A ``Task`` objects in MeldFabric's key-scoped SQL table."""

    def __init__(self, agent_kit: AgentKit, key_record: Any) -> None:
        self.agent_kit = agent_kit
        self.key_record = key_record

    async def save(self, task: Task, context: Any | None = None) -> None:
        del context
        metadata = task.metadata or {}
        raw_meldfabric = metadata.get("meldfabric")
        meldfabric = dict(raw_meldfabric) if isinstance(raw_meldfabric, dict) else {}
        user_id = str(meldfabric.get("userId") or f"a2a-key:{self.key_record.id}")
        thread_id = str(meldfabric.get("threadId") or f"a2a:{self.key_record.id}:{task.context_id}")
        run_id = str(meldfabric.get("runId") or task.id)
        generation = int(
            meldfabric.get("configGeneration") or self.agent_kit.snapshots.current.revision
        )
        state = task.status.state.value
        owner = (
            self.agent_kit.agent_runtime.instance_id
            if task.status.state in {TaskState.submitted, TaskState.working}
            else None
        )
        await self._repository().put_a2a_task(
            task_id=task.id,
            workspace_id=self.agent_kit.runtime_config.workspace.id,
            agent_id=self.key_record.agent_id,
            access_key_id=self.key_record.id,
            user_id=user_id,
            context_id=task.context_id,
            thread_id=thread_id,
            run_id=run_id,
            config_generation=generation,
            state=state,
            task=task.model_dump(mode="json", by_alias=True, exclude_none=True),
            owner_instance_id=owner,
        )

    async def get(self, task_id: str, context: Any | None = None) -> Task | None:
        del context
        stored = await self._find(task_id)
        if stored is None:
            return None
        return Task.model_validate(stored.task)

    async def delete(self, task_id: str, context: Any | None = None) -> None:
        del context
        await self._repository().delete_a2a_task(
            task_id,
            agent_id=self.key_record.agent_id,
            access_key_id=self.key_record.id,
        )

    async def _find(self, task_id: str) -> StoredA2ATask | None:
        current = self._repository()
        checked: set[str] = set()
        for repository in (current, *self.agent_kit.storage.all_system_repositories()):
            if repository.database.url in checked:
                continue
            checked.add(repository.database.url)
            stored = await repository.get_a2a_task(
                task_id,
                agent_id=self.key_record.agent_id,
                access_key_id=self.key_record.id,
            )
            if stored is not None:
                return stored
        return None

    def _repository(self) -> SystemRepository:
        repository = self.agent_kit.storage.system
        if repository is None:
            raise RuntimeError("the MeldFabric system database is not available")
        return repository


class MeldFabricRuntimeA2AExecutor(AgentExecutor):
    """Run official A2A tasks through MeldFabric's revision-aware Strands runtime."""

    def __init__(self, agent_kit: AgentKit, key_record: Any) -> None:
        self.agent_kit = agent_kit
        self.key_record = key_record

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        logger.debug(
            "Starting MeldFabric A2A execution with queue=%s closed=%s",
            id(event_queue),
            event_queue.is_closed(),
        )
        message = context.message
        if message is None or not message.parts:
            raise ServerError(
                error=InvalidParamsError(message="A2A message contains no content parts")
            ) from None
        if self.key_record.agent_id is None:
            raise ServerError(
                error=InvalidParamsError(message="remote access key has no target Agent")
            ) from None
        endpoint = self._target_endpoint()
        if not self.agent_kit.accepts_new_runs:
            raise ServerError(
                error=InvalidParamsError(
                    message="runtime has not adopted the active configuration generation"
                )
            ) from None

        is_existing_task = context.current_task is not None
        task = context.current_task or new_task(message)
        context.current_task = task
        metadata = self._request_metadata(context)
        user_id = str(metadata.get("userId") or f"a2a-key:{self.key_record.id}")
        thread_id = f"a2a:{self.key_record.id}:{task.context_id}"
        resume_entries = self._resume_entries(message)
        is_resume = resume_entries is not None
        if (
            task.status.state in {TaskState.input_required, TaskState.auth_required}
            and not is_resume
        ):
            raise ServerError(
                error=InvalidParamsError(
                    message="task is waiting for interrupt responses, not a new message"
                )
            ) from None
        if is_resume and task.status.state not in {
            TaskState.input_required,
            TaskState.auth_required,
        }:
            raise ServerError(
                error=InvalidParamsError(message="task has no pending interrupt")
            ) from None

        revision = await self.agent_kit.agent_runtime.revision_for_stream(
            endpoint_id=endpoint.id,
            user_id=user_id,
            thread_id=thread_id,
            resume=is_resume,
        )
        run_id = f"run_{uuid.uuid4().hex}"
        remote_context = self._remote_context(metadata, endpoint.id)
        raw_meldfabric = (task.metadata or {}).get("meldfabric")
        old_meldfabric = dict(raw_meldfabric) if isinstance(raw_meldfabric, dict) else {}
        task.metadata = {
            **(task.metadata or {}),
            "meldfabric": {
                **old_meldfabric,
                "userId": user_id,
                "threadId": thread_id,
                "runId": run_id,
                "configGeneration": revision,
                "remoteContext": remote_context.model_dump(mode="json"),
            },
        }
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        # Re-enqueuing an existing input-required Task would be interpreted by the
        # official SDK consumer as the final event for this request.  Move it to
        # working first so an interrupt response can actually resume execution.
        if not is_existing_task:
            await event_queue.enqueue_event(task)
        await updater.update_status(TaskState.working)

        artifact_id = f"artifact_{uuid.uuid4().hex}"
        streamed = False
        completed: NativeExecutionResult | None = None
        try:
            runtime_input = (
                {"messages": []} if is_resume else {"messages": [self._runtime_message(message)]}
            )
            async for event in self.agent_kit.agent_runtime.stream(
                endpoint.id,
                runtime_input,
                user_id=user_id,
                thread_id=thread_id,
                run_id=run_id,
                remote_context=remote_context,
                revision=revision,
                resume_entries=resume_entries,
            ):
                if isinstance(event, dict) and event.get("data"):
                    await updater.add_artifact(
                        [Part(root=TextPart(text=str(event["data"])))],
                        artifact_id=artifact_id,
                        name="agent_response",
                        append=streamed,
                        last_chunk=False,
                    )
                    streamed = True
                elif isinstance(event, NativeExecutionResult):
                    completed = event

            if completed is None:
                raise RuntimeError("Strands A2A execution completed without a result")
            if completed.interrupts:
                await updater.requires_input(
                    message=updater.new_agent_message(
                        [
                            Part(
                                root=TextPart(text="Agent requires input before it can continue.")
                            ),
                            Part(root=DataPart(data={"interrupts": completed.interrupts})),
                        ],
                        metadata={"meldfabric": {"configGeneration": revision}},
                    ),
                    # The task remains resumable, but this is the final event of
                    # the current streaming interaction cycle (A2A 0.3 semantics).
                    final=True,
                )
                return

            if streamed:
                await updater.add_artifact(
                    [Part(root=TextPart(text=""))],
                    artifact_id=artifact_id,
                    name="agent_response",
                    append=True,
                    last_chunk=True,
                )
            else:
                await updater.add_artifact(
                    self._result_parts(completed.as_dict()),
                    artifact_id=artifact_id,
                    name="agent_response",
                    last_chunk=True,
                )
            await updater.complete()
        except asyncio.CancelledError:
            with suppress(RuntimeError):
                await updater.cancel()
            raise
        except Exception:
            logger.exception("A2A task %s failed", task.id)
            with suppress(RuntimeError):
                await updater.failed(
                    message=updater.new_agent_message(
                        [Part(root=TextPart(text="Agent execution failed."))]
                    )
                )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            raise ServerError(error=InvalidParamsError(message="A2A task was not found"))
        raw_meldfabric = (task.metadata or {}).get("meldfabric")
        meldfabric = dict(raw_meldfabric) if isinstance(raw_meldfabric, dict) else {}
        run_id = str(meldfabric.get("runId") or task.id)
        user_id = str(meldfabric.get("userId") or f"a2a-key:{self.key_record.id}")
        thread_id = str(meldfabric.get("threadId") or f"a2a:{self.key_record.id}:{task.context_id}")
        try:
            if task.status.state in {TaskState.input_required, TaskState.auth_required}:
                await self.agent_kit.agent_runtime.abandon_interrupted_run(
                    run_id,
                    endpoint_id=self.key_record.agent_id,
                    user_id=user_id,
                    thread_id=thread_id,
                )
            else:
                await self.agent_kit.agent_runtime.cancel_run(
                    run_id,
                    endpoint_id=self.key_record.agent_id,
                    user_id=user_id,
                    thread_id=thread_id,
                )
        except RunNotFoundError:
            logger.debug("A2A run %s had already stopped before cancellation", run_id)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.cancel(
            message=updater.new_agent_message(
                [Part(root=TextPart(text="Task cancelled by the caller."))]
            )
        )

    def _target_endpoint(self) -> Any:
        endpoint = next(
            (
                item
                for item in self.agent_kit.runtime_config.endpoints
                if item.id == self.key_record.agent_id
            ),
            None,
        )
        if endpoint is None or not endpoint.enabled or "a2a" not in endpoint.protocols:
            raise ServerError(
                error=InvalidParamsError(message="target A2A endpoint is unavailable")
            ) from None
        return endpoint

    @staticmethod
    def _request_metadata(context: RequestContext) -> dict[str, Any]:
        result = dict(context.metadata or {})
        if context.message and isinstance(context.message.metadata, dict):
            result = {**result, **context.message.metadata}
        meldfabric = result.get("meldfabric")
        if isinstance(meldfabric, dict) and meldfabric.get("userId") and not result.get("userId"):
            result["userId"] = meldfabric["userId"]
        return result

    def _remote_context(self, metadata: dict[str, Any], agent_id: str) -> RemoteCallContext:
        raw_meldfabric = metadata.get("meldfabric")
        meldfabric = dict(raw_meldfabric) if isinstance(raw_meldfabric, dict) else {}
        raw = meldfabric.get("remoteContext") or meldfabric.get("remote_context")
        node = f"{self.agent_kit.runtime_config.workspace.id}/{agent_id}"
        if isinstance(raw, dict):
            return RemoteCallContext.model_validate(raw).enter(node)
        return RemoteCallContext.root(
            service_id=self.agent_kit.runtime_config.workspace.id,
            agent_id=agent_id,
        )

    @staticmethod
    def _resume_entries(message: Message) -> list[dict[str, Any]] | None:
        entries: list[dict[str, Any]] = []
        unrelated = 0
        seen: set[str] = set()
        for part in message.parts:
            root = part.root
            response = (
                root.data.get("interruptResponse")
                if isinstance(root, DataPart) and isinstance(root.data, dict)
                else None
            )
            if response is None:
                unrelated += 1
                continue
            if not isinstance(response, dict):
                raise ServerError(
                    error=InvalidParamsError(message="interruptResponse must be an object")
                ) from None
            interrupt_id = response.get("interruptId")
            if not isinstance(interrupt_id, str) or not interrupt_id:
                raise ServerError(
                    error=InvalidParamsError(message="interruptResponse requires interruptId")
                ) from None
            if interrupt_id in seen or response.get("response") is None:
                raise ServerError(
                    error=InvalidParamsError(
                        message="interrupt responses must be unique and non-null"
                    )
                ) from None
            seen.add(interrupt_id)
            entries.append(
                {
                    "interrupt_id": interrupt_id,
                    "status": "resolved",
                    "payload": response["response"],
                }
            )
        if not entries:
            return None
        if unrelated:
            raise ServerError(
                error=InvalidParamsError(
                    message="interrupt responses cannot be mixed with other content"
                )
            ) from None
        return entries

    @staticmethod
    def _runtime_message(message: Message) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for part in message.parts:
            root = part.root
            if isinstance(root, TextPart):
                content.append({"type": "text", "text": root.text})
            elif isinstance(root, DataPart):
                content.append({"type": "text", "text": json.dumps(root.data, ensure_ascii=False)})
            elif isinstance(root, FilePart):
                file = root.file
                raw = getattr(file, "bytes", None)
                uri = getattr(file, "uri", None)
                mime = getattr(file, "mime_type", None) or "application/octet-stream"
                name = getattr(file, "name", None) or "file"
                if raw and str(mime).startswith("image/"):
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{raw}"},
                        }
                    )
                elif raw:
                    content.append(
                        {
                            "type": "input_file",
                            "file": {
                                "filename": name,
                                "file_data": f"data:{mime};base64,{raw}",
                            },
                        }
                    )
                else:
                    content.append(
                        {
                            "type": "text",
                            "text": f"[File: {name} ({mime})] {uri or ''}".strip(),
                        }
                    )
        return {
            "id": message.message_id,
            "role": "user" if message.role == Role.user else "assistant",
            "content": content,
        }

    @staticmethod
    def _result_parts(result: dict[str, Any]) -> list[Part]:
        messages = result.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                role = message.get("role") if isinstance(message, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if role != "assistant" or not isinstance(content, list):
                    continue
                parts: list[Part] = []
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        parts.append(Part(root=TextPart(text=block["text"])))
                    elif isinstance(block, dict):
                        parts.append(Part(root=DataPart(data=block)))
                if parts:
                    return parts
        return [Part(root=DataPart(data={"result": jsonable(result)}))]


class MeldFabricRequestHandler(DefaultRequestHandler):
    """Finish an interrupted request's queue cleanup before it can be resumed.

    The upstream handler intentionally schedules interrupt cleanup in the
    background.  A caller can therefore submit its interrupt answer while the
    old task queue is still registered and receive a child of the queue that is
    about to be closed.  Waiting only at the interrupt boundary keeps normal and
    non-blocking execution unchanged while making immediate resume deterministic.
    """

    async def on_message_send(self, params: Any, context: Any | None = None) -> Any:
        result = await super().on_message_send(params, context)
        if self._is_input_required(result):
            task_id = getattr(result, "id", None) or getattr(result, "task_id", None)
            if task_id:
                await self._wait_for_queue_cleanup(str(task_id))
        return result

    async def on_message_send_stream(self, params: Any, context: Any | None = None):
        interrupted_task_id: str | None = None
        source = super().on_message_send_stream(params, context)
        try:
            async for event in source:
                if self._is_input_required(event):
                    interrupted_task_id = str(
                        getattr(event, "id", None) or getattr(event, "task_id", "")
                    )
                yield event
        finally:
            await source.aclose()
            if interrupted_task_id:
                await self._wait_for_queue_cleanup(interrupted_task_id)

    async def _wait_for_queue_cleanup(self, task_id: str) -> None:
        async with asyncio.timeout(5):
            while await self._queue_manager.get(task_id) is not None:
                await asyncio.sleep(0)

    @staticmethod
    def _is_input_required(event: Any) -> bool:
        status = getattr(event, "status", None)
        state = getattr(status, "state", None)
        return state == TaskState.input_required


@dataclass(slots=True)
class _ServerBundle:
    server: A2AServer
    app: Any
    card: Any
    metadata_agent: Agent


class NativeA2AGateway:
    """Key-aware ASGI gateway around per-Agent native Strands A2A servers."""

    def __init__(self, agent_kit: AgentKit) -> None:
        self.agent_kit = agent_kit
        self._bundles: dict[tuple[int, str, str], _ServerBundle] = {}
        # Bundle creation is control-plane work, not a per-token hot path.  A single
        # lock makes generation rollover atomic and prevents an old-generation
        # builder from repopulating the cache after it has been retired.
        self._bundle_lock = asyncio.Lock()

    async def agent_card(self, key_record: Any, *, endpoint: str) -> dict[str, Any]:
        bundle = await self._bundle(key_record, endpoint=endpoint)
        return bundle.card.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._send_json(send, 404, {"detail": "Not Found"})
            return
        path = str(scope.get("path") or "/")
        root_path = str(scope.get("root_path") or "").rstrip("/")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path) :]
        segments = [item for item in path.split("/") if item]
        if not segments:
            await self._send_json(send, 404, {"detail": "Not Found"})
            return
        agent_id = segments[0]
        subpath = "/" + "/".join(segments[1:]) if len(segments) > 1 else "/"
        required_scope = "discover" if scope.get("method") == "GET" else "invoke"
        key_record = await self._verify_key(scope, required_scope=required_scope)
        if key_record is None:
            await self._send_json(send, 401, {"detail": {"code": "AGENT_KEY_INVALID"}})
            return
        if key_record.agent_id != agent_id:
            await self._send_json(
                send,
                403,
                {"detail": {"code": "AGENT_KEY_SCOPE_MISMATCH"}},
            )
            return
        endpoint = self._endpoint(scope, agent_id)
        try:
            bundle = await self._bundle(key_record, endpoint=endpoint)
        except ServerError as exc:
            await self._send_json(send, 404, {"detail": str(exc)})
            return
        child_scope = dict(scope)
        child_scope["path"] = subpath
        child_scope["raw_path"] = subpath.encode()
        child_scope["root_path"] = f"{scope.get('root_path', '').rstrip('/')}/{agent_id}"
        await bundle.app(child_scope, receive, send)

    async def aclose(self) -> None:
        async with self._bundle_lock:
            bundles = list(self._bundles.values())
            self._bundles.clear()
        for bundle in bundles:
            await bundle.metadata_agent.shutdown_async()

    async def _bundle(self, key_record: Any, *, endpoint: str) -> _ServerBundle:
        if key_record.agent_id is None:
            raise ServerError(
                error=InvalidParamsError(message="remote access key has no target Agent")
            ) from None
        generation = self.agent_kit.snapshots.current.revision
        key = (generation, key_record.id, key_record.agent_id)
        existing = self._bundles.get(key)
        if existing is not None:
            logger.debug("Reusing native A2A server bundle %s", key)
            return existing
        retired: list[_ServerBundle] = []
        async with self._bundle_lock:
            existing = self._bundles.get(key)
            if existing is not None:
                logger.debug("Reusing native A2A server bundle %s after lock", key)
                return existing
            endpoint_definition = next(
                (
                    item
                    for item in self.agent_kit.runtime_config.endpoints
                    if item.id == key_record.agent_id
                ),
                None,
            )
            if (
                endpoint_definition is None
                or not endpoint_definition.enabled
                or "a2a" not in endpoint_definition.protocols
                or endpoint_definition.target_type != "agent"
            ):
                raise ServerError(
                    error=InvalidParamsError(message="target A2A endpoint is unavailable")
                ) from None
            agent = next(
                item
                for item in self.agent_kit.runtime_config.agents
                if item.id == endpoint_definition.target_ref
            )
            description = agent.description or agent.display_name or agent.name
            metadata_agent = await asyncio.to_thread(
                lambda: Agent(
                    model=_AgentCardModel(),
                    name=agent.display_name or agent.name,
                    description=description,
                    callback_handler=None,
                )
            )
            task_store = MeldFabricTaskStore(self.agent_kit, key_record)
            skills = [
                AgentSkill(
                    id=agent.id,
                    name=agent.display_name or agent.name,
                    description=description,
                    tags=["agent", "meldfabric", "strands"],
                    input_modes=["text", "application/json"],
                    output_modes=["text", "application/json"],
                )
            ]
            server = A2AServer(
                agent_factory=lambda _context_id: metadata_agent,
                http_url=endpoint,
                serve_at_root=True,
                version=__version__,
                skills=skills,
                task_store=task_store,
                enable_a2a_compliant_streaming=True,
            )
            handler = MeldFabricRequestHandler(
                agent_executor=MeldFabricRuntimeA2AExecutor(self.agent_kit, key_record),
                task_store=task_store,
            )
            server.request_handler = handler
            card = server.public_agent_card.model_copy(
                update={
                    "security_schemes": {
                        "remoteAccessKey": SecurityScheme(
                            root=HTTPAuthSecurityScheme(
                                scheme="bearer",
                                bearer_format="MeldFabric Remote Access Key",
                                description="A remote-access key generated for this endpoint.",
                            )
                        )
                    },
                    "security": [{"remoteAccessKey": []}],
                }
            )
            app = A2AFastAPIApplication(agent_card=card, http_handler=handler).build()
            bundle = _ServerBundle(
                server=server,
                app=app,
                card=card,
                metadata_agent=metadata_agent,
            )
            self._bundles[key] = bundle
            for stale_key in tuple(self._bundles):
                if stale_key[0] != generation:
                    retired.append(self._bundles.pop(stale_key))
            logger.debug("Created native A2A server bundle %s", key)
        for stale in retired:
            await stale.metadata_agent.shutdown_async()
        return bundle

    async def _verify_key(self, scope: Scope, *, required_scope: str) -> Any | None:
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw = headers.get(b"authorization", b"").decode("latin-1")
        if not raw.lower().startswith("bearer "):
            return None
        repository = self.agent_kit.storage.secrets
        if repository is None:
            return None
        return await repository.verify_key(raw[7:].strip(), required_scope=required_scope)

    @staticmethod
    def _endpoint(scope: Scope, agent_id: str) -> str:
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("latin-1")
        if not host:
            server = scope.get("server") or ("127.0.0.1", 80)
            host = f"{server[0]}:{server[1]}"
        root = str(scope.get("root_path") or "").rstrip("/")
        return f"{scope.get('scheme', 'http')}://{host}{root}/{agent_id}"

    @staticmethod
    async def _send_json(send: Send, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
