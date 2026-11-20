"""AG-UI 1.0 models and direct Strands event transcoding."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from meldfabric.agents.types import NativeExecutionResult
from meldfabric.domain.serialization import jsonable
from meldfabric.errors import RunCancelledError, public_run_error


class AGUIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class AGUIResumeEntry(AGUIModel):
    interrupt_id: str = Field(alias="interruptId")
    status: Literal["resolved", "cancelled"]
    payload: Any | None = None
    metadata: dict[str, Any] | None = None


class AGUIRunInput(AGUIModel):
    thread_id: str = Field(alias="threadId", min_length=1)
    run_id: str = Field(alias="runId", min_length=1)
    protocol_version: str | None = Field(default=None, alias="protocolVersion")
    parent_run_id: str | None = Field(default=None, alias="parentRunId")
    state: Any | None = None
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    context: list[dict[str, str]] | None = None
    forwarded_props: Any | None = Field(default=None, alias="forwardedProps")
    resume: list[AGUIResumeEntry] | None = None

    def user_id(self, header_user_id: str | None) -> str | None:
        if header_user_id:
            return header_user_id
        if isinstance(self.forwarded_props, dict):
            value = self.forwarded_props.get("userId") or self.forwarded_props.get("user_id")
            return str(value) if value else None
        return None

    def runtime_input(self) -> dict[str, Any]:
        return {"messages": self.messages}

    def resume_entries(self) -> list[dict[str, Any]] | None:
        if self.resume is None:
            return None
        return [item.model_dump(mode="json") for item in self.resume]


def encode_sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _interrupt_event(interrupt: dict[str, Any]) -> dict[str, Any]:
    interrupt_id = str(
        interrupt.get("id") or interrupt.get("interruptId") or f"interrupt_{uuid.uuid4().hex}"
    )
    reason = interrupt.get("reason") or interrupt.get("value") or interrupt
    message = "The Agent is waiting for external input."
    if isinstance(reason, dict):
        message = str(reason.get("message") or reason.get("description") or message)
    return {
        "type": "RUN_INPUT_REQUIRED",
        "threadId": None,
        "runId": None,
        "interrupt": {
            "id": interrupt_id,
            "reason": "human_in_the_loop",
            "message": message,
            "metadata": {"strands": jsonable(reason)},
        },
    }


def _tool_result_content(tool_result: dict[str, Any]) -> str:
    content = tool_result.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            str(item.get("text")) for item in content if isinstance(item, dict) and "text" in item
        ]
        if texts:
            return "\n".join(texts)
    return json.dumps(jsonable(content), ensure_ascii=False)


async def transcode_runtime_events(
    stream: AsyncIterator[Any],
    *,
    thread_id: str,
    run_id: str,
    parent_run_id: str | None,
    config_generation: int,
) -> AsyncIterator[dict[str, Any]]:
    """Map dictionaries from Strands ``stream_async`` onto AG-UI event objects."""

    started: dict[str, Any] = {
        "type": "RUN_STARTED",
        "threadId": thread_id,
        "runId": run_id,
        "protocolVersion": "1.0",
    }
    if parent_run_id:
        started["parentRunId"] = parent_run_id
    yield started

    message_id: str | None = None
    active_node_id: str | None = None
    message_has_content = False
    # Python dictionaries preserve insertion order.  Keep the open calls ordered so
    # providers that omit both the optional toolUseId and contentBlockIndex can still
    # associate a delta with the most recently opened call.  Prefer the Strands block
    # index whenever it is present because multiple tool blocks may be streamed at once.
    open_tools: dict[str, None] = {}
    tool_blocks: dict[int, str] = {}
    result: NativeExecutionResult | None = None
    try:
        async for item in stream:
            if isinstance(item, NativeExecutionResult):
                result = item
                continue
            if not isinstance(item, dict):
                continue
            # Strands wraps every event produced by a Graph, Workflow or Swarm node
            # in ``multiagent_node_stream``.  Direct Agents expose ``data``,
            # ``event`` and ``message`` at the top level, while multi-Agent roots put
            # the exact same payload under ``event``.  Normalize both shapes before
            # translating them; otherwise orchestration endpoints appear non-streaming
            # and only surface their output in RUN_FINISHED.
            node_id: str | None = None
            source = item
            if item.get("type") == "multiagent_node_stream" and isinstance(item.get("event"), dict):
                node_id = str(item.get("node_id") or "") or None
                source = item["event"]

            text = source.get("data")
            if isinstance(text, str) and text:
                if message_id is None:
                    message_id = f"message_{uuid.uuid4().hex}"
                    yield {
                        "type": "TEXT_MESSAGE_START",
                        "messageId": message_id,
                        "role": "assistant",
                    }
                if node_id is not None and node_id != active_node_id:
                    yield {
                        "type": "TEXT_MESSAGE_CONTENT",
                        "messageId": message_id,
                        "delta": ("\n" if message_has_content else "") + f"{node_id}: ",
                    }
                    message_has_content = True
                    active_node_id = node_id
                yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": text}
                message_has_content = True

            raw_event = source.get("event")
            if isinstance(raw_event, dict):
                start = raw_event.get("contentBlockStart")
                start_fields = dict(start) if isinstance(start, dict) else {}
                start_payload = start_fields.get("start")
                tool_use = start_payload.get("toolUse") if isinstance(start_payload, dict) else None
                if isinstance(tool_use, dict):
                    tool_id = str(tool_use.get("toolUseId") or "")
                    tool_name = str(tool_use.get("name") or "")
                    if tool_id and tool_name:
                        open_tools[tool_id] = None
                        block_index = start_fields.get("contentBlockIndex")
                        if isinstance(block_index, int):
                            tool_blocks[block_index] = tool_id
                        yield {
                            "type": "TOOL_CALL_START",
                            "toolCallId": tool_id,
                            "toolCallName": tool_name,
                            **({"parentMessageId": message_id} if message_id else {}),
                        }
                delta = raw_event.get("contentBlockDelta")
                delta_fields = dict(delta) if isinstance(delta, dict) else {}
                delta_payload = delta_fields.get("delta")
                tool_delta = (
                    delta_payload.get("toolUse") if isinstance(delta_payload, dict) else None
                )
                if isinstance(tool_delta, dict):
                    block_index = delta_fields.get("contentBlockIndex")
                    explicit_tool_id = str(tool_delta.get("toolUseId") or "")
                    tool_id = explicit_tool_id
                    if not tool_id and isinstance(block_index, int):
                        tool_id = tool_blocks.get(block_index, "")
                    if not tool_id and open_tools:
                        tool_id = next(reversed(open_tools))
                    tool_name = str(tool_delta.get("name") or "")
                    if explicit_tool_id and explicit_tool_id not in open_tools and tool_name:
                        open_tools[explicit_tool_id] = None
                        if isinstance(block_index, int):
                            tool_blocks[block_index] = explicit_tool_id
                        yield {
                            "type": "TOOL_CALL_START",
                            "toolCallId": explicit_tool_id,
                            "toolCallName": tool_name,
                            **({"parentMessageId": message_id} if message_id else {}),
                        }
                    arguments = tool_delta.get("input")
                    if tool_id and arguments is not None:
                        yield {
                            "type": "TOOL_CALL_ARGS",
                            "toolCallId": tool_id,
                            "delta": (
                                arguments
                                if isinstance(arguments, str)
                                else json.dumps(arguments, ensure_ascii=False)
                            ),
                        }

            message = source.get("message")
            if isinstance(message, dict) and message.get("role") == "user":
                for block in message.get("content", []):
                    tool_result = block.get("toolResult") if isinstance(block, dict) else None
                    if not isinstance(tool_result, dict):
                        continue
                    tool_id = str(tool_result.get("toolUseId") or "unknown_tool_call")
                    if tool_id in open_tools:
                        yield {"type": "TOOL_CALL_END", "toolCallId": tool_id}
                        open_tools.pop(tool_id, None)
                        tool_blocks = {
                            index: mapped_id
                            for index, mapped_id in tool_blocks.items()
                            if mapped_id != tool_id
                        }
                    yield {
                        "type": "TOOL_CALL_RESULT",
                        "messageId": f"message_{uuid.uuid4().hex}",
                        "toolCallId": tool_id,
                        "content": _tool_result_content(tool_result),
                        "role": "tool",
                    }

        if message_id is not None:
            yield {"type": "TEXT_MESSAGE_END", "messageId": message_id}
        for tool_id in tuple(open_tools):
            yield {"type": "TOOL_CALL_END", "toolCallId": tool_id}
        if result is None:
            raise RuntimeError("Strands stream completed without a native execution result")
        if result.interrupts:
            for interrupt in result.interrupts:
                event = _interrupt_event(interrupt)
                event["threadId"] = thread_id
                event["runId"] = run_id
                yield event
            return
        yield {
            "type": "RUN_FINISHED",
            "threadId": thread_id,
            "runId": run_id,
            "result": {
                "output": result.output,
                "structuredOutput": jsonable(result.structured_output),
                "stopReason": result.stop_reason,
                "configGeneration": config_generation,
            },
            "outcome": {"type": "success"},
        }
    except RunCancelledError as exc:
        yield {
            "type": "RUN_FINISHED",
            "threadId": thread_id,
            "runId": run_id,
            "result": {"configGeneration": config_generation},
            "outcome": {"type": "cancelled", "message": exc.message},
        }
    except Exception as exc:
        public = public_run_error(exc)
        yield {"type": "RUN_ERROR", "message": public.message, "code": public.code}


async def transcode_runtime_stream(
    stream: AsyncIterator[Any],
    *,
    thread_id: str,
    run_id: str,
    parent_run_id: str | None,
    config_generation: int,
) -> AsyncIterator[str]:
    """Encode the shared AG-UI event mapping as an SSE response stream."""

    async for event in transcode_runtime_events(
        stream,
        thread_id=thread_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        config_generation=config_generation,
    ):
        yield encode_sse(event)
