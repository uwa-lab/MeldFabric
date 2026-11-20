"""Strands-native OpenTelemetry routing for optional Langfuse providers."""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meldfabric.config.models import AgentDefinition, ObservabilityProvider, WorkspaceConfig
from meldfabric.errors import AgentNotReadyError, OptionalDependencyError

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan, Span
    from opentelemetry.sdk.trace.export import SpanProcessor

    from meldfabric.storage import SecretReader

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunObservation:
    """Per-run trace attributes consumed by a fresh Strands Agent instance."""

    trace_attributes: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(slots=True)
class _Route:
    processor: SpanProcessor
    sample_rate: float


class _RoutingSpanProcessor:
    """Route a whole trace to the provider selected on its root Agent span."""

    route_attribute = "meldfabric.observability.route"

    def __init__(self) -> None:
        self._routes: dict[str, _Route] = {}
        self._trace_routes: dict[int, str | None] = {}
        self._root_spans: set[tuple[int, int]] = set()
        self._lock = threading.RLock()

    def register(self, key: str, route: _Route) -> None:
        with self._lock:
            previous = self._routes.get(key)
            if previous is not None:
                previous.processor.shutdown()
            self._routes[key] = route

    def unregister(self, key: str) -> None:
        with self._lock:
            route = self._routes.pop(key, None)
        if route is not None:
            route.processor.shutdown()

    def on_start(self, span: Span, parent_context: Any = None) -> None:
        context = span.get_span_context()
        trace_id = context.trace_id
        span_id = context.span_id
        attributes = getattr(span, "attributes", {}) or {}
        selected = attributes.get(self.route_attribute)
        with self._lock:
            if isinstance(selected, str):
                route = self._routes.get(selected)
                keep = route is not None and self._sampled(trace_id, route.sample_rate)
                self._trace_routes[trace_id] = selected if keep else None
                self._root_spans.add((trace_id, span_id))
            route_key = self._trace_routes.get(trace_id)
            route = self._routes.get(route_key) if route_key is not None else None
        if route is not None:
            route.processor.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        context = span.get_span_context()
        if context is None:
            return
        trace_id = context.trace_id
        span_id = context.span_id
        with self._lock:
            route_key = self._trace_routes.get(trace_id)
            route = self._routes.get(route_key) if route_key is not None else None
            is_root = (trace_id, span_id) in self._root_spans
        if route is not None:
            route.processor.on_end(span)
        if is_root:
            with self._lock:
                self._root_spans.discard((trace_id, span_id))
                self._trace_routes.pop(trace_id, None)

    def shutdown(self) -> None:
        with self._lock:
            routes = list(self._routes.values())
            self._routes.clear()
            self._trace_routes.clear()
            self._root_spans.clear()
        for route in routes:
            route.processor.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        with self._lock:
            routes = list(self._routes.values())
        return all(route.processor.force_flush(timeout_millis) for route in routes)

    @staticmethod
    def _sampled(trace_id: int, rate: float) -> bool:
        if rate <= 0:
            return False
        if rate >= 1:
            return True
        return (trace_id & ((1 << 64) - 1)) / float(1 << 64) < rate


_PROCESS_ROUTER = _RoutingSpanProcessor()
_PROCESS_ROUTER_ATTACHED_TO: set[int] = set()
_PROCESS_ROUTER_LOCK = threading.RLock()


def _attach_process_router() -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    with _PROCESS_ROUTER_LOCK:
        provider = trace.get_tracer_provider()
        add_processor = getattr(provider, "add_span_processor", None)
        if add_processor is None:
            provider = TracerProvider(resource=Resource.create({"service.name": "meldfabric"}))
            trace.set_tracer_provider(provider)
            provider = trace.get_tracer_provider()
            add_processor = getattr(provider, "add_span_processor", None)
        if add_processor is None:
            raise AgentNotReadyError(
                "the process OpenTelemetry provider cannot install a span processor"
            )
        identity = id(provider)
        if identity not in _PROCESS_ROUTER_ATTACHED_TO:
            add_processor(_PROCESS_ROUTER)
            _PROCESS_ROUTER_ATTACHED_TO.add(identity)


class ObservabilityManager:
    def __init__(
        self,
        *,
        secrets_getter: Callable[[int], SecretReader | None],
    ) -> None:
        self._secrets_getter = secrets_getter
        self._instance_id = uuid.uuid4().hex
        self._routes: dict[tuple[int, str], str] = {}
        self._route_locks: dict[tuple[int, str], asyncio.Lock] = {}
        self.issues: deque[dict[str, Any]] = deque(maxlen=200)

    async def for_run(
        self,
        config: WorkspaceConfig,
        agent: AgentDefinition,
        *,
        run_id: str,
        user_id: str,
        thread_id: str,
        remote_context: dict[str, Any] | None = None,
    ) -> RunObservation:
        metadata: dict[str, Any] = {
            "workspace_id": config.workspace.id,
            "agent_id": agent.id,
            "config_generation": config.config_generation,
            "run_id": run_id,
            "user_id": user_id,
            "thread_id": thread_id,
        }
        if remote_context:
            metadata.update(
                {
                    "remote_call_chain_id": remote_context.get("call_chain_id"),
                    "remote_delegation_id": remote_context.get("delegation_id"),
                    "remote_depth": remote_context.get("depth"),
                }
            )
        attributes: dict[str, Any] = {
            "meldfabric.workspace.id": config.workspace.id,
            "meldfabric.agent.id": agent.id,
            "meldfabric.config.generation": config.config_generation,
            "meldfabric.run.id": run_id,
        }
        if agent.observability_ref is None:
            return RunObservation(trace_attributes=attributes, metadata=metadata)

        provider = next(
            (
                item
                for item in config.resources.observability_providers
                if item.id == agent.observability_ref
            ),
            None,
        )
        if provider is None or not provider.enabled:
            return await self._handle_failure(
                provider,
                agent,
                attributes,
                metadata,
                AgentNotReadyError(
                    f"observability provider {agent.observability_ref!r} is unavailable"
                ),
            )
        try:
            route_key = await self._ensure_route(config.config_generation, provider)
        except Exception as exc:
            return await self._handle_failure(
                provider,
                agent,
                attributes,
                metadata,
                exc,
            )
        attributes.update(
            {
                _PROCESS_ROUTER.route_attribute: route_key,
                "langfuse.user.id": user_id,
                "langfuse.session.id": thread_id,
                "langfuse.trace.name": f"meldfabric:{agent.id}",
                "langfuse.tags": ("meldfabric", agent.id),
                "langfuse.trace.metadata.workspace_id": config.workspace.id,
                "langfuse.trace.metadata.config_generation": str(config.config_generation),
                "langfuse.trace.metadata.run_id": run_id,
            }
        )
        return RunObservation(trace_attributes=attributes, metadata=metadata)

    async def _ensure_route(
        self,
        revision: int,
        provider: ObservabilityProvider,
    ) -> str:
        cache_key = (revision, provider.id)
        existing = self._routes.get(cache_key)
        if existing is not None:
            return existing
        async with self._route_locks.setdefault(cache_key, asyncio.Lock()):
            existing = self._routes.get(cache_key)
            if existing is not None:
                return existing
            secret_key = await self._secret(revision, provider)
            await asyncio.to_thread(self._auth_check, provider, secret_key)
            route_key = f"{self._instance_id}:{revision}:{provider.id}"
            route = await asyncio.to_thread(
                self._build_route,
                provider,
                secret_key,
            )
            _attach_process_router()
            _PROCESS_ROUTER.register(route_key, route)
            self._routes[cache_key] = route_key
            return route_key

    async def _secret(
        self,
        revision: int,
        provider: ObservabilityProvider,
    ) -> str:
        repository = self._secrets_getter(revision)
        if repository is None:
            raise AgentNotReadyError("the control secret store is unavailable for Langfuse")
        if provider.secret_key_ref is None:
            raise AgentNotReadyError(
                f"Langfuse provider {provider.id!r} has no secret key configured",
                details={"observability_ref": provider.id},
            )
        secret_key = await repository.get_secret(provider.secret_key_ref)
        if secret_key is None:
            raise AgentNotReadyError(
                f"Langfuse secret {provider.secret_key_ref!r} was not found",
                details={"observability_ref": provider.id},
            )
        return secret_key

    @staticmethod
    def _auth_check(provider: ObservabilityProvider, secret_key: str) -> None:
        try:
            from langfuse import Langfuse
        except ImportError as exc:
            raise OptionalDependencyError(
                "Langfuse observability requires meldfabric[langfuse]"
            ) from exc
        client = Langfuse(
            public_key=provider.public_key,
            secret_key=secret_key,
            host=provider.host,
            environment=provider.environment,
        )
        try:
            if not client.auth_check():
                raise AgentNotReadyError(
                    "Langfuse rejected the configured credentials",
                    details={"observability_ref": provider.id},
                )
        finally:
            client.shutdown()

    @staticmethod
    def _build_route(
        provider: ObservabilityProvider,
        secret_key: str,
    ) -> _Route:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError as exc:
            raise OptionalDependencyError(
                "Langfuse observability requires meldfabric[langfuse]"
            ) from exc
        credential = base64.b64encode(f"{provider.public_key}:{secret_key}".encode()).decode()
        host = provider.host.rstrip("/")
        endpoint = (
            host
            if host.endswith("/api/public/otel/v1/traces")
            else f"{host}/api/public/otel/v1/traces"
        )
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers={"Authorization": f"Basic {credential}"},
            timeout=10,
        )
        return _Route(
            processor=BatchSpanProcessor(exporter),
            sample_rate=provider.sample_rate,
        )

    async def _handle_failure(
        self,
        provider: ObservabilityProvider | None,
        agent: AgentDefinition,
        attributes: dict[str, Any],
        metadata: dict[str, Any],
        error: Exception,
    ) -> RunObservation:
        issue = {
            "code": getattr(error, "code", "OBSERVABILITY_UNAVAILABLE"),
            "message": str(error),
            "agent_id": agent.id,
            "provider_id": provider.id if provider else agent.observability_ref,
        }
        self.issues.append(issue)
        logger.warning("Observability disabled for this run: %s", issue)
        if provider is not None and provider.fail_closed:
            if isinstance(error, AgentNotReadyError):
                raise error
            raise AgentNotReadyError(
                "configured observability provider failed closed",
                details=issue,
            ) from error
        return RunObservation(trace_attributes=attributes, metadata=metadata)

    def prune(self, active_revisions: set[int]) -> None:
        for cache_key, route_key in list(self._routes.items()):
            if cache_key[0] in active_revisions:
                continue
            self._routes.pop(cache_key, None)
            self._route_locks.pop(cache_key, None)
            _PROCESS_ROUTER.unregister(route_key)

    async def test_provider(
        self,
        provider: ObservabilityProvider,
        *,
        revision: int,
    ) -> bool:
        secret_key = await self._secret(revision, provider)
        await asyncio.to_thread(self._auth_check, provider, secret_key)
        return True

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        for route_key in list(self._routes.values()):
            _PROCESS_ROUTER.unregister(route_key)
        self._routes.clear()
        self._route_locks.clear()
