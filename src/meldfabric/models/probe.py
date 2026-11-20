"""Stateless model configuration checks for the authenticated administrator."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from meldfabric.config.models import ModelProvider, ModelResource
from meldfabric.errors import public_run_error
from meldfabric.models.registry import ModelAdapter


async def probe_model(
    adapter: ModelAdapter,
    provider: ModelProvider,
    model: ModelResource,
    *,
    api_key: str | None,
) -> dict[str, Any]:
    tools_enabled = model.capabilities.tool_calling is not False
    try:
        from strands import Agent, tool

        native_model = await asyncio.to_thread(
            adapter.build_strands,
            provider,
            model,
            api_key=api_key,
        )

        @tool(name="configuration_probe")
        def configuration_probe() -> str:
            """A harmless tool exposed only while testing model configuration."""

            return "OK"

        agent = await asyncio.to_thread(
            Agent,
            model=native_model,
            tools=[configuration_probe] if tools_enabled else [],
            system_prompt="Reply briefly with OK. Do not call any tools.",
            callback_handler=None,
        )
        received = False
        try:
            # Consume the stream too: a successful HTTP header alone is insufficient.
            async with asyncio.timeout(model.timeout_seconds or provider.timeout_seconds):
                async for event in agent.stream_async("Reply with OK."):
                    received |= bool(event.get("data") or event.get("result"))
        finally:
            await agent.shutdown_async()
        if not received:
            return {
                "ok": False,
                "error": {
                    "code": "MODEL_EMPTY_RESPONSE",
                    "message": "The model returned no content",
                },
            }
    except Exception as exc:
        error = public_run_error(exc)
        message = error.message
        # Only this admin-only diagnostic exposes a provider's structured error
        # message. Never return raw response bodies, request headers or traces.
        from openai import APIStatusError

        if isinstance(exc, APIStatusError) and isinstance(exc.body, dict):
            body = exc.body.get("error", exc.body)
            if isinstance(body, dict) and isinstance(body.get("message"), str):
                message = body["message"]
        if isinstance(exc, TimeoutError):
            error.code = "MODEL_TIMEOUT"
            message = "The configuration test timed out"
        for value in (api_key, provider.base_url, *provider.headers.values()):
            if value:
                message = message.replace(value, "[REDACTED]")
        message = re.sub(r"(?i)\bBearer\s+\S+|\bsk-[\w-]+", "[REDACTED]", message)
        return {"ok": False, "error": {"code": error.code, "message": message[:2000]}}
    return {
        "ok": True,
        "protocol": "chat_completions",
        "tools": tools_enabled,
    }
