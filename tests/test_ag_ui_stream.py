from __future__ import annotations

import json

import pytest

from meldfabric.agents.native_compiler import NativeExecutionResult
from meldfabric.protocols.ag_ui import transcode_runtime_events, transcode_runtime_stream


def decode(value: str) -> dict:
    return json.loads(value.removeprefix("data: ").strip())


def completed(*, interrupts=None) -> NativeExecutionResult:
    return NativeExecutionResult(
        executable_id="agent-one",
        output="final answer",
        messages=[{"role": "assistant", "content": [{"text": "final answer"}]}],
        state={},
        stop_reason="end_turn",
        interrupts=interrupts or [],
        structured_output={"answer": "final answer"},
        metrics={"usage": {"totalTokens": 2}},
    )


@pytest.mark.asyncio
async def test_tool_delta_uses_content_block_index_when_provider_omits_tool_id() -> None:
    async def stream():
        yield {
            "event": {
                "contentBlockStart": {
                    "contentBlockIndex": 4,
                    "start": {"toolUse": {"toolUseId": "call-1", "name": "lookup"}},
                }
            }
        }
        yield {
            "event": {
                "contentBlockDelta": {
                    "contentBlockIndex": 4,
                    "delta": {"toolUse": {"input": '{"query":"hello"}'}},
                }
            }
        }
        yield completed()

    events = [
        event
        async for event in transcode_runtime_events(
            stream(),
            thread_id="thread",
            run_id="run",
            parent_run_id=None,
            config_generation=1,
        )
    ]

    assert not any(event["type"] == "RUN_ERROR" for event in events)
    assert {
        "type": "TOOL_CALL_ARGS",
        "toolCallId": "call-1",
        "delta": '{"query":"hello"}',
    } in events


@pytest.mark.asyncio
async def test_native_strands_text_and_tool_events_are_transcoded_to_ag_ui() -> None:
    async def stream():
        yield {
            "event": {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "call-1", "name": "lookup"}}
                }
            }
        }
        yield {
            "event": {
                "contentBlockDelta": {
                    "delta": {
                        "toolUse": {
                            "toolUseId": "call-1",
                            "input": '{"query":"hello"}',
                        }
                    }
                }
            }
        }
        yield {
            "message": {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "call-1",
                            "status": "success",
                            "content": [{"text": "found"}],
                        }
                    }
                ],
            }
        }
        yield {"data": "final answer"}
        yield completed()

    events = [
        decode(event)
        async for event in transcode_runtime_stream(
            stream(),
            thread_id="thread",
            run_id="run",
            parent_run_id=None,
            config_generation=19,
        )
    ]

    assert events[0]["type"] == "RUN_STARTED"
    assert any(event["type"] == "TOOL_CALL_START" for event in events)
    assert any(event["type"] == "TOOL_CALL_ARGS" for event in events)
    assert any(event["type"] == "TOOL_CALL_RESULT" for event in events)
    assert any(
        event["type"] == "TEXT_MESSAGE_CONTENT" and event["delta"] == "final answer"
        for event in events
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["result"]["structuredOutput"] == {"answer": "final answer"}
    assert events[-1]["result"]["configGeneration"] == 19


@pytest.mark.asyncio
async def test_multiagent_node_streams_are_transcoded_incrementally() -> None:
    async def stream():
        yield {
            "type": "multiagent_node_stream",
            "node_id": "review",
            "event": {"data": "evidence "},
        }
        yield {
            "type": "multiagent_node_stream",
            "node_id": "review",
            "event": {"data": "checked"},
        }
        yield {
            "type": "multiagent_node_stream",
            "node_id": "plan",
            "event": {"data": "ready"},
        }
        yield completed()

    events = [
        decode(event)
        async for event in transcode_runtime_stream(
            stream(),
            thread_id="thread",
            run_id="run",
            parent_run_id=None,
            config_generation=20,
        )
    ]
    deltas = [event["delta"] for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"]

    assert deltas == ["review: ", "evidence ", "checked", "\nplan: ", "ready"]
    assert sum(event["type"] == "TEXT_MESSAGE_START" for event in events) == 1
    assert sum(event["type"] == "TEXT_MESSAGE_END" for event in events) == 1
    assert events[-1]["type"] == "RUN_FINISHED"


@pytest.mark.asyncio
async def test_strands_interrupt_becomes_ag_ui_input_required_event() -> None:
    async def stream():
        yield completed(
            interrupts=[
                {
                    "id": "interrupt-1",
                    "reason": {
                        "message": "Approve lookup",
                        "tool_call_id": "call-1",
                        "allowed_decisions": ["approve", "reject"],
                    },
                }
            ]
        )

    events = [
        decode(event)
        async for event in transcode_runtime_stream(
            stream(),
            thread_id="thread",
            run_id="run",
            parent_run_id="parent",
            config_generation=3,
        )
    ]

    required = events[-1]
    assert required["type"] == "RUN_INPUT_REQUIRED"
    assert required["interrupt"]["id"] == "interrupt-1"
    assert required["interrupt"]["message"] == "Approve lookup"


@pytest.mark.asyncio
async def test_each_native_interrupt_is_emitted_without_a_fake_plan_protocol() -> None:
    async def stream():
        yield completed(
            interrupts=[
                {"id": "one", "reason": {"message": "First approval"}},
                {"id": "two", "reason": {"message": "Second approval"}},
            ]
        )

    events = [
        decode(event)
        async for event in transcode_runtime_stream(
            stream(),
            thread_id="thread",
            run_id="run",
            parent_run_id=None,
            config_generation=3,
        )
    ]
    required = [event for event in events if event["type"] == "RUN_INPUT_REQUIRED"]
    assert [event["interrupt"]["id"] for event in required] == ["one", "two"]
