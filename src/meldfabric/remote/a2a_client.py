"""Official A2A SDK client adapter for protocol-neutral remote Agents."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import (
    DataPart,
    Message,
    Part,
    Role,
    Task,
    TaskIdParams,
    TaskState,
    TextPart,
)

from meldfabric.errors import RemoteAgentUnreachableError
from meldfabric.protocols.a2a import A2A_PROTOCOL
from meldfabric.remote.models import (
    RemoteAgentDescriptor,
    RemoteCallContext,
    RemoteResumeEntry,
    RemoteRunResult,
    RemoteTopology,
    TopologyNode,
)


class A2ARemoteClient:
    """Translate MeldFabric's remote contract through the official A2A 0.3 client SDK."""

    def __init__(
        self,
        base_url: str,
        access_key: str,
        *,
        timeout_seconds: float = 60.0,
        service_id: str | None = None,
        agent_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_key = access_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self._service_id = service_id
        self._agent_id = agent_id
        self._descriptor: RemoteAgentDescriptor | None = None
        self._card: Any | None = None

    async def discover(self) -> RemoteAgentDescriptor:
        try:
            async with self._http_client() as client:
                resolver = A2ACardResolver(httpx_client=client, base_url=self.base_url)
                card = await resolver.get_agent_card()
        except Exception as exc:
            raise self._unreachable("A2A Agent Card discovery failed", exc) from exc
        if not card.url:
            raise self._invalid("A2A Agent Card has no JSON-RPC URL")
        skills = card.skills or []
        first_skill = skills[0] if skills else None
        service_id = self._service_id or self._derived_service_id()
        agent_id = self._agent_id or (
            first_skill.id
            if first_skill is not None
            else f"agent_{hashlib.sha256(card.name.encode()).hexdigest()[:16]}"
        )
        descriptor = RemoteAgentDescriptor(
            service_id=service_id,
            agent_id=agent_id,
            name=card.name or agent_id,
            display_name=card.name or None,
            description=card.description or "",
            status="active",
            config_generation=0,
            protocols=[A2A_PROTOCOL],
            capabilities={
                "invoke": True,
                "topology": False,
                "interrupt_resume": True,
                "streaming": bool(card.capabilities and card.capabilities.streaming),
                "opaque_topology_leaf": True,
                "protocol_version": card.protocol_version,
            },
        )
        self._card = card
        self._service_id = service_id
        self._agent_id = agent_id
        self._descriptor = descriptor
        return descriptor

    async def invoke(
        self,
        *,
        messages: list[dict[str, Any]],
        user_id: str,
        thread_id: str,
        remote_context: RemoteCallContext,
    ) -> RemoteRunResult:
        content = self._last_user_content(messages)
        part = (
            Part(root=TextPart(text=content))
            if isinstance(content, str)
            else Part(root=DataPart(data={"value": content}))
        )
        message = Message(
            message_id=f"message_{uuid.uuid4().hex}",
            context_id=thread_id,
            role=Role.user,
            parts=[part],
            metadata={
                "userId": user_id,
                "meldfabric": {"remoteContext": remote_context.model_dump(mode="json")},
            },
        )
        task = await self._send(message)
        return self._task_result(task, user_id=user_id, thread_id=thread_id)

    async def resume(
        self,
        *,
        run_id: str,
        user_id: str,
        thread_id: str,
        remote_context: RemoteCallContext,
        resume: list[RemoteResumeEntry | dict[str, Any]],
    ) -> RemoteRunResult:
        entries = [
            item if isinstance(item, RemoteResumeEntry) else RemoteResumeEntry.model_validate(item)
            for item in resume
        ]
        parts = [
            Part(
                root=DataPart(
                    data={
                        "interruptResponse": {
                            "interruptId": item.interrupt_id,
                            "response": self._resume_payload(item),
                        }
                    }
                )
            )
            for item in entries
        ]
        message = Message(
            message_id=f"message_{uuid.uuid4().hex}",
            context_id=thread_id,
            task_id=run_id,
            role=Role.user,
            parts=parts,
            metadata={
                "userId": user_id,
                "meldfabric": {"remoteContext": remote_context.model_dump(mode="json")},
            },
        )
        task = await self._send(message)
        return self._task_result(task, user_id=user_id, thread_id=thread_id)

    async def cancel(self, *, run_id: str, user_id: str, thread_id: str) -> None:
        del user_id, thread_id
        try:
            async with self._client() as client:
                await client.cancel_task(TaskIdParams(id=run_id))
        except Exception as exc:
            raise self._unreachable("A2A task cancellation failed", exc) from exc

    async def topology(self, *, max_depth: int = 10) -> RemoteTopology:
        del max_depth
        descriptor = self._descriptor or await self.discover()
        return RemoteTopology(
            nodes=[
                TopologyNode(
                    service_id=descriptor.service_id,
                    agent_id=descriptor.agent_id,
                    name=descriptor.name,
                    status=descriptor.status,
                    config_generation=None,
                )
            ],
            truncated=False,
        )

    async def _send(self, message: Message) -> Task:
        try:
            task: Task | None = None
            async with self._client() as client:
                async for event in client.send_message(message):
                    if isinstance(event, tuple) and isinstance(event[0], Task):
                        task = event[0]
                    elif isinstance(event, Task):
                        task = event
                    elif isinstance(event, Message):
                        raise self._invalid("A2A Agent returned a direct Message instead of a Task")
            if task is None:
                raise self._invalid("A2A Agent returned no Task")
            return task
        except RemoteAgentUnreachableError:
            raise
        except Exception as exc:
            raise self._unreachable("A2A message delivery failed", exc) from exc

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        card = self._card
        if card is None:
            await self.discover()
            card = self._card
        if card is None:
            raise self._invalid("A2A Agent Card is unavailable")
        async with self._http_client() as http_client:
            # MeldFabric's remote-interaction state machine consumes one aggregated Task.
            # The server still supports native A2A streaming for direct clients.
            config = ClientConfig(httpx_client=http_client, streaming=False)
            yield ClientFactory(config).create(card)

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
            headers={"Authorization": f"Bearer {self.access_key}"},
        )

    def _task_result(
        self,
        task: Task,
        *,
        user_id: str,
        thread_id: str,
    ) -> RemoteRunResult:
        meldfabric = (task.metadata or {}).get("meldfabric", {})
        revision = int(meldfabric.get("configGeneration") or 0)
        if task.status.state in {TaskState.input_required, TaskState.auth_required}:
            interrupts: list[Any] = []
            if task.status.message:
                for part in task.status.message.parts:
                    root = part.root
                    if isinstance(root, DataPart) and isinstance(root.data.get("interrupts"), list):
                        interrupts.extend(root.data["interrupts"])
            return RemoteRunResult(
                run_id=task.id,
                agent_id=self._agent_id or "a2a-agent",
                user_id=user_id,
                thread_id=thread_id,
                config_generation=revision,
                status="interrupted",
                result={"__interrupt__": interrupts},
            )
        if task.status.state != TaskState.completed:
            raise RemoteAgentUnreachableError(
                f"A2A task ended in state {task.status.state.value!r}",
                details={"task_id": task.id, "state": task.status.state.value},
            )
        content = self._task_content(task)
        return RemoteRunResult(
            run_id=task.id,
            agent_id=self._agent_id or "a2a-agent",
            user_id=user_id,
            thread_id=thread_id,
            config_generation=revision,
            result={"messages": [{"role": "assistant", "content": content}]},
        )

    @staticmethod
    def _task_content(task: Task) -> Any:
        artifact = task.artifacts[-1] if task.artifacts else None
        values: list[Any] = []
        for part in artifact.parts if artifact else []:
            root = part.root
            if isinstance(root, TextPart) and root.text:
                values.append(root.text)
            elif isinstance(root, DataPart):
                values.append(root.data)
        if len(values) == 1:
            return values[0]
        if values:
            return values
        return json.dumps(
            task.status.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
        )

    @staticmethod
    def _resume_payload(item: RemoteResumeEntry) -> Any:
        if item.status == "cancelled":
            return {"decisions": [{"type": "reject", "message": "Cancelled by the user"}]}
        return item.payload

    @staticmethod
    def _last_user_content(messages: list[dict[str, Any]]) -> Any:
        for message in reversed(messages):
            if message.get("role") in {"user", "human"}:
                return message.get("content", "")
        return {"messages": messages}

    def _derived_service_id(self) -> str:
        return f"a2a_{hashlib.sha256(self.base_url.encode()).hexdigest()[:20]}"

    def _invalid(self, message: str) -> RemoteAgentUnreachableError:
        return RemoteAgentUnreachableError(message, details={"base_url": self.base_url})

    def _unreachable(self, message: str, exc: Exception) -> RemoteAgentUnreachableError:
        return RemoteAgentUnreachableError(
            f"{message} for {self.base_url!r}",
            details={"base_url": self.base_url, "reason": str(exc)},
        )
