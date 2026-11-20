"""Deterministic Strands model fixtures shared by runtime integration tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from strands.models import Model

from meldfabric.models import DiscoveredModel

Response = str | dict[str, Any]
Responder = Callable[[list[dict[str, Any]], list[dict[str, Any]], int], Response]


class ScriptedStrandsModel(Model):
    """Small provider-faithful model that emits the native Strands stream protocol."""

    def __init__(self, responder: Responder | None = None) -> None:
        self.config: dict[str, Any] = {
            "model_id": "scripted-model",
            "context_window_limit": 32_000,
        }
        self.responder = responder or (lambda _messages, _tools, _index: "scripted response")
        self.calls: list[dict[str, Any]] = []

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self.config)

    async def structured_output(
        self,
        output_model,
        prompt,
        system_prompt: str | None = None,
        **_kwargs: Any,
    ):
        value = output_model.model_validate({})
        yield {"output": value}

    async def stream(
        self,
        messages,
        tool_specs=None,
        system_prompt: str | None = None,
        *,
        tool_choice=None,
        system_prompt_content=None,
        invocation_state=None,
        cancel_signal=None,
        agent_metadata=None,
        structured_output_model=None,
        **_kwargs: Any,
    ):
        del (
            tool_choice,
            system_prompt_content,
            cancel_signal,
            agent_metadata,
            structured_output_model,
        )
        copied_messages = list(messages)
        copied_tools = list(tool_specs or [])
        self.calls.append(
            {
                "messages": copied_messages,
                "tools": copied_tools,
                "system_prompt": system_prompt,
                "invocation_state": invocation_state,
            }
        )
        response = self.responder(copied_messages, copied_tools, len(self.calls))
        yield {"messageStart": {"role": "assistant"}}
        if isinstance(response, dict) and "tool" in response:
            tool = response["tool"]
            yield {
                "contentBlockStart": {
                    "start": {
                        "toolUse": {
                            "name": str(tool["name"]),
                            "toolUseId": str(tool.get("id") or f"call-{len(self.calls)}"),
                        }
                    }
                }
            }
            yield {
                "contentBlockDelta": {
                    "delta": {
                        "toolUse": {"input": json.dumps(tool.get("input", {}), ensure_ascii=False)}
                    }
                }
            }
            yield {"contentBlockStop": {}}
            stop_reason = "tool_use"
        else:
            text = str(response.get("text", "") if isinstance(response, dict) else response)
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": text}}}
            yield {"contentBlockStop": {}}
            stop_reason = "end_turn"
        yield {"messageStop": {"stopReason": stop_reason}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        }


class ScriptedModelAdapter:
    name = "scripted"
    requires_api_key = False

    def __init__(self, model: ScriptedStrandsModel) -> None:
        self.model = model

    def build_strands(self, _provider, _model, *, api_key: str | None):
        assert api_key is None
        return self.model

    async def discover(self, _provider, *, api_key: str | None):
        assert api_key is None
        return [DiscoveredModel(id="scripted-model", metadata={})]


def final_text(state: dict[str, Any]) -> str:
    """Extract the final assistant text from a MeldFabric runtime state."""

    for message in reversed(state.get("messages", [])):
        if message.get("role") != "assistant":
            continue
        return "".join(
            str(block.get("text", ""))
            for block in message.get("content", [])
            if isinstance(block, dict)
        )
    return ""
