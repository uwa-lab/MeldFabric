"""HTTP client for the MeldFabric Remote Protocol."""

from __future__ import annotations

from typing import Any

import httpx

from meldfabric.errors import (
    AgentDisabledError,
    AgentNotFoundError,
    InterruptPendingError,
    RemoteAgentCycleError,
    RemoteAgentUnreachableError,
    RemoteDelegationDepthError,
    RemoteDelegationLimitError,
    RemoteInteractionNotSupportedError,
)
from meldfabric.remote.models import (
    RemoteAgentDescriptor,
    RemoteCallContext,
    RemoteCancelRequest,
    RemoteInvokeRequest,
    RemoteResumeEntry,
    RemoteResumeRequest,
    RemoteRunResult,
    RemoteTopology,
)


class MeldFabricRemoteClient:
    def __init__(
        self,
        base_url: str,
        access_key: str,
        *,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_key = access_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def discover(self) -> RemoteAgentDescriptor:
        payload = await self._request("GET", "/.well-known/meldfabric-agent.json")
        return RemoteAgentDescriptor.model_validate(payload)

    async def invoke(
        self,
        *,
        messages: list[dict[str, Any]],
        user_id: str,
        thread_id: str,
        remote_context: RemoteCallContext,
    ) -> RemoteRunResult:
        request = RemoteInvokeRequest(
            messages=messages,
            user_id=user_id,
            thread_id=thread_id,
            remote_context=remote_context,
        )
        payload = await self._request(
            "POST",
            "/api/remote/invoke",
            json=request.model_dump(mode="json"),
        )
        return RemoteRunResult.model_validate(payload)

    async def topology(self, *, max_depth: int = 10) -> RemoteTopology:
        payload = await self._request(
            "GET",
            "/api/remote-topology",
            params={"max_depth": max_depth},
        )
        return RemoteTopology.model_validate(payload)

    async def resume(
        self,
        *,
        run_id: str,
        user_id: str,
        thread_id: str,
        remote_context: RemoteCallContext,
        resume: list[RemoteResumeEntry | dict[str, Any]],
    ) -> RemoteRunResult:
        request = RemoteResumeRequest(
            run_id=run_id,
            user_id=user_id,
            thread_id=thread_id,
            remote_context=remote_context,
            resume=[
                item
                if isinstance(item, RemoteResumeEntry)
                else RemoteResumeEntry.model_validate(item)
                for item in resume
            ],
        )
        payload = await self._request(
            "POST",
            "/api/remote/resume",
            json=request.model_dump(mode="json"),
        )
        return RemoteRunResult.model_validate(payload)

    async def cancel(
        self,
        *,
        run_id: str,
        user_id: str,
        thread_id: str,
    ) -> None:
        request = RemoteCancelRequest(
            run_id=run_id,
            user_id=user_id,
            thread_id=thread_id,
        )
        await self._request(
            "POST",
            "/api/remote/cancel",
            json=request.model_dump(mode="json"),
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
                headers={"Authorization": f"Bearer {self.access_key}"},
            ) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RemoteAgentUnreachableError(
                f"remote agent at {self.base_url!r} is unreachable",
                details={"base_url": self.base_url, "reason": str(exc)},
            ) from exc

        if response.is_error:
            self._raise_remote_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RemoteAgentUnreachableError(
                "remote agent returned a non-JSON response",
                details={"base_url": self.base_url, "status_code": response.status_code},
            ) from exc
        if not isinstance(payload, dict):
            raise RemoteAgentUnreachableError(
                "remote agent returned an invalid response object",
                details={"base_url": self.base_url},
            )
        return payload

    def _raise_remote_error(self, response: httpx.Response) -> None:
        code = "REMOTE_AGENT_UNREACHABLE"
        message = f"remote agent request failed with HTTP {response.status_code}"
        details: dict[str, Any] = {
            "base_url": self.base_url,
            "status_code": response.status_code,
        }
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error") or payload.get("detail")
            if isinstance(error, dict):
                code = str(error.get("code", code))
                message = str(error.get("message", message))
                details["remote_details"] = error.get("details", {})
        error_types = {
            "AGENT_NOT_FOUND": AgentNotFoundError,
            "AGENT_DISABLED": AgentDisabledError,
            "REMOTE_AGENT_CYCLE_DETECTED": RemoteAgentCycleError,
            "REMOTE_DELEGATION_DEPTH_EXCEEDED": RemoteDelegationDepthError,
            "REMOTE_DELEGATION_LIMIT_EXCEEDED": RemoteDelegationLimitError,
            "REMOTE_INTERACTION_NOT_SUPPORTED": RemoteInteractionNotSupportedError,
            "INTERRUPT_PENDING": InterruptPendingError,
        }
        error_type = error_types.get(code, RemoteAgentUnreachableError)
        raise error_type(
            message,
            details={**details, "remote_code": code},
        )
