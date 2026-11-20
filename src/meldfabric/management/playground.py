"""Presentation helpers for the management endpoint Playground."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from meldfabric.domain.identity import identity_digest


def playground_user_id(workspace_id: str) -> str:
    """Stable tester identity shared by conversations in one workspace."""

    return f"playground:{workspace_id}"


def serialize_conversation(record: Any, endpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.id,
        "endpoint_id": record.endpoint_id,
        "endpoint_name": record.endpoint_name,
        "user_id": record.user_id,
        "thread_id": record.thread_id,
        "title": record.title or "新会话",
        "created_generation": record.created_generation,
        "message_count": record.message_count,
        "last_message_preview": record.last_message_preview,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "endpoint": endpoint,
    }


def serialize_message(record: Any) -> dict[str, Any]:
    return {
        "sequence": record.sequence,
        "id": record.id,
        "run_id": record.run_id,
        "kind": record.kind,
        "role": record.role,
        "content": record.content,
        "payload": dict(record.payload or {}),
        "created_at": record.created_at,
    }


def runtime_messages(records: list[Any]) -> list[dict[str, Any]]:
    """Return only protocol messages; tools and notices are display-only entries."""

    return [
        {"id": item.id, "role": item.role, "content": item.content}
        for item in records
        if item.kind == "message" and item.role in {"user", "assistant"} and item.content.strip()
    ]


@dataclass(slots=True)
class PlaygroundTranscriptCollector:
    """Collect display records while forwarding the shared AG-UI event stream."""

    run_id: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    terminal: bool = False
    _messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    _tools: dict[str, dict[str, Any]] = field(default_factory=dict)

    def accept(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "TEXT_MESSAGE_START":
            message_id = str(event["messageId"])
            entry = {
                "id": message_id,
                "run_id": self.run_id,
                "kind": "message",
                "role": "assistant",
                "content": "",
                "payload": {},
            }
            self.entries.append(entry)
            self._messages[message_id] = entry
            return
        if event_type == "TEXT_MESSAGE_CONTENT":
            message = self._messages.get(str(event.get("messageId") or ""))
            if message is not None:
                message["content"] += str(event.get("delta") or "")
            return
        if event_type == "TOOL_CALL_START":
            tool_call_id = str(event.get("toolCallId") or uuid.uuid4().hex)
            entry = {
                "id": "tool_" + identity_digest(self.run_id, tool_call_id)[:48],
                "run_id": self.run_id,
                "kind": "tool",
                "role": "tool",
                "content": "",
                "payload": {
                    "tool_call_id": tool_call_id,
                    "name": str(event.get("toolCallName") or "工具"),
                    "arguments": "",
                    "result": None,
                    "status": "running",
                },
            }
            self.entries.append(entry)
            self._tools[tool_call_id] = entry
            return
        if event_type == "TOOL_CALL_ARGS":
            tool = self._tools.get(str(event.get("toolCallId") or ""))
            if tool is not None:
                tool["payload"]["arguments"] += str(event.get("delta") or "")
            return
        if event_type == "TOOL_CALL_END":
            tool = self._tools.get(str(event.get("toolCallId") or ""))
            if tool is not None:
                tool["payload"]["status"] = "completed"
            return
        if event_type == "TOOL_CALL_RESULT":
            tool_call_id = str(event.get("toolCallId") or "")
            tool = self._tools.get(tool_call_id)
            if tool is None:
                tool = {
                    "id": "tool_" + identity_digest(self.run_id, tool_call_id)[:48],
                    "run_id": self.run_id,
                    "kind": "tool",
                    "role": "tool",
                    "content": "",
                    "payload": {
                        "tool_call_id": tool_call_id,
                        "name": "工具",
                        "arguments": "",
                        "result": None,
                        "status": "completed",
                    },
                }
                self.entries.append(tool)
                self._tools[tool_call_id] = tool
            tool["content"] = str(event.get("content") or "")
            tool["payload"]["result"] = tool["content"]
            tool["payload"]["status"] = "completed"
            return
        if event_type == "RUN_INPUT_REQUIRED":
            interrupt = dict(event.get("interrupt") or {})
            interrupt_id = str(interrupt.get("id") or uuid.uuid4().hex)
            self.entries.append(
                {
                    "id": "interrupt_" + identity_digest(self.run_id, interrupt_id)[:48],
                    "run_id": self.run_id,
                    "kind": "interrupt",
                    "role": "system",
                    "content": str(interrupt.get("message") or "Agent 正在等待外部输入。"),
                    "payload": interrupt,
                }
            )
            self.terminal = True
            return
        if event_type == "RUN_ERROR":
            self.entries.append(
                {
                    "id": f"error_{uuid.uuid4().hex}",
                    "run_id": self.run_id,
                    "kind": "error",
                    "role": "system",
                    "content": str(event.get("message") or "运行失败"),
                    "payload": {"code": event.get("code")},
                }
            )
            self.terminal = True
            return
        if event_type == "RUN_FINISHED":
            self.terminal = True
            result = dict(event.get("result") or {})
            output = str(result.get("output") or "").strip()
            assistant = next(
                (
                    item
                    for item in reversed(self.entries)
                    if item["kind"] == "message" and item["role"] == "assistant"
                ),
                None,
            )
            if output and (assistant is None or not assistant["content"].strip()):
                if assistant is None:
                    assistant = {
                        "id": f"message_{uuid.uuid4().hex}",
                        "run_id": self.run_id,
                        "kind": "message",
                        "role": "assistant",
                        "content": output,
                        "payload": {},
                    }
                    self.entries.append(assistant)
                else:
                    assistant["content"] = output
            if assistant is not None:
                assistant["payload"].update(
                    {
                        "structured_output": result.get("structuredOutput"),
                        "stop_reason": result.get("stopReason"),
                        "config_generation": result.get("configGeneration"),
                    }
                )
            outcome = dict(event.get("outcome") or {})
            if outcome.get("type") == "cancelled":
                self.entries.append(
                    {
                        "id": f"status_{uuid.uuid4().hex}",
                        "run_id": self.run_id,
                        "kind": "status",
                        "role": "system",
                        "content": str(outcome.get("message") or "生成已停止"),
                        "payload": {"status": "cancelled"},
                    }
                )

    def disconnected_entry(self) -> dict[str, Any] | None:
        if self.terminal:
            return None
        return {
            "id": f"status_{uuid.uuid4().hex}",
            "run_id": self.run_id,
            "kind": "status",
            "role": "system",
            "content": "响应已中止",
            "payload": {"status": "disconnected"},
        }
