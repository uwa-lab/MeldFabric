"""Revision-aware compiled Agent cache and invocation service."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meldfabric.agents.concurrency import KeyedAsyncLock
from meldfabric.agents.types import ActiveRun, PausedRun
from meldfabric.domain.identity import identity_digest
from meldfabric.errors import (
    ConfigurationRevisionUnavailableError,
    OptionalDependencyError,
    RunAlreadyActiveError,
    RunCancelledError,
    RunConflictError,
    RunIdAlreadyUsedError,
    RunLeaseLostError,
    RunNotFoundError,
    ServiceSuspendedError,
    ThreadAwaitingResumeError,
    ThreadBusyError,
    public_run_error,
)
from meldfabric.identity import UserIdentity
from meldfabric.remote.models import RemoteCallContext
from meldfabric.storage import RuntimeLease

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from meldfabric.agents.native_compiler import StrandsCompiledExecutable
    from meldfabric.app import AgentKit
    from meldfabric.storage import SystemRepository


class AgentRuntimeManager:
    lease_ttl_seconds = 45
    lease_renew_interval_seconds = 10
    lease_abort_margin_seconds = 10
    coordination_io_timeout_seconds = 5
    instance_heartbeat_interval_seconds = 3

    def __init__(self, agent_kit: AgentKit, *, config_dir: Path) -> None:
        self.agent_kit = agent_kit
        self.config_dir = config_dir
        self._compiled: dict[tuple[int, str, int, int], StrandsCompiledExecutable] = {}
        self._locks: dict[tuple[int, str, int, int], asyncio.Lock] = {}
        self._active_runs: dict[tuple[str, str, str, str], ActiveRun] = {}
        self._paused_runs: dict[tuple[str, str, str], PausedRun] = {}
        # Runtime state is owned by a logical Agent thread.  A keyed lock keeps
        # start/resume/finish transitions atomic for that thread without serializing
        # unrelated users and conversations in this process.  Cross-process exclusion
        # remains the responsibility of the database-backed thread lease.
        self._run_locks: KeyedAsyncLock[tuple[str, str, str]] = KeyedAsyncLock()
        self.instance_id = f"inst_{uuid.uuid4().hex}"
        self._hostname = socket.gethostname()
        self._process_id = os.getpid()
        self._instance_heartbeat_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._maintenance_interval_override: float | None = None
        self._lifecycle_references = 0

    @property
    def maintenance_interval_seconds(self) -> float:
        if self._maintenance_interval_override is not None:
            return self._maintenance_interval_override
        return float(self.agent_kit.runtime_config.runtime.maintenance_interval_seconds)

    @maintenance_interval_seconds.setter
    def maintenance_interval_seconds(self, value: float) -> None:
        """Allow focused runtime tests to shorten the otherwise configured interval."""

        self._maintenance_interval_override = value

    @property
    def active_run_count(self) -> int:
        return len(self._active_runs)

    @property
    def started(self) -> bool:
        return self._instance_heartbeat_task is not None

    async def start(self) -> None:
        self._lifecycle_references += 1
        if self._instance_heartbeat_task is not None:
            return
        try:
            await self.agent_kit.asynchronize_configuration()
            repository = await self.agent_kit.storage.control_repository(
                self.agent_kit.runtime_config
            )
            await self._register_instance(repository)
            self._instance_heartbeat_task = asyncio.create_task(
                self._instance_heartbeat_loop(repository),
                name=f"meldfabric-runtime-heartbeat-{self.instance_id}",
            )
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(repository),
                name=f"meldfabric-runtime-maintenance-{self.instance_id}",
            )
        except Exception:
            self._lifecycle_references -= 1
            raise

    async def close(self) -> None:
        if self._lifecycle_references > 0:
            self._lifecycle_references -= 1
        if self._lifecycle_references > 0:
            return
        task = self._instance_heartbeat_task
        self._instance_heartbeat_task = None
        maintenance = self._maintenance_task
        self._maintenance_task = None
        tasks = [item for item in (task, maintenance) if item is not None]
        for item in tasks:
            item.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        secrets = self.agent_kit.storage.secrets
        if secrets is not None:
            try:
                await secrets.flush_key_usage()
            except Exception:
                logger.exception("Failed to flush inbound key usage during shutdown")
        repository = self.agent_kit.storage.system
        if repository is not None:
            try:
                await repository.stop_runtime_instance(self.instance_id)
            except Exception:
                logger.exception("Failed to mark runtime instance %s stopped", self.instance_id)

    async def _register_instance(self, repository: SystemRepository) -> None:
        await repository.register_runtime_instance(
            instance_id=self.instance_id,
            workspace_id=self.agent_kit.runtime_config.workspace.id,
            hostname=self._hostname,
            process_id=self._process_id,
            config_generation=self.agent_kit.snapshots.current.revision,
            catalog_digest=self.agent_kit.catalog_digest,
        )

    async def _instance_heartbeat_loop(self, repository: SystemRepository) -> None:
        while True:
            try:
                await asyncio.sleep(self.instance_heartbeat_interval_seconds)
                suspended = await repository.runtime_instance_is_suspended(self.instance_id)
                if suspended:
                    self.agent_kit._runtime_suspended = True
                    await repository.heartbeat_runtime_instance(
                        instance_id=self.instance_id,
                        config_generation=self.agent_kit.snapshots.current.revision,
                        catalog_digest=self.agent_kit.catalog_digest,
                        status="suspended",
                    )
                    continue
                status = (
                    "configuration_error"
                    if self.agent_kit.last_reload_error is not None
                    else "ready"
                )
                present = await repository.heartbeat_runtime_instance(
                    instance_id=self.instance_id,
                    config_generation=self.agent_kit.snapshots.current.revision,
                    catalog_digest=self.agent_kit.catalog_digest,
                    status=status,
                )
                if not present:
                    await self._register_instance(repository)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Runtime instance heartbeat failed for %s", self.instance_id)

    async def _maintenance_loop(self, repository: SystemRepository) -> None:
        """Run eventual-consistency work outside the liveness heartbeat."""

        while True:
            await asyncio.sleep(self.maintenance_interval_seconds)
            try:
                await self.agent_kit.asynchronize_configuration()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.agent_kit._last_reload_error = exc
                logger.exception(
                    "Runtime instance %s failed to synchronize configuration",
                    self.instance_id,
                )
            secrets = self.agent_kit.storage.secrets
            if secrets is not None:
                try:
                    await secrets.flush_key_usage()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Failed to flush inbound key usage")
            try:
                await repository.cleanup_expired_runtime_state()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to clean expired runtime coordination state")

    async def compiled_endpoint(
        self,
        revision: int,
        endpoint_id: str,
    ) -> StrandsCompiledExecutable:
        registry = self.agent_kit.tool_registry
        tool_generation, definitions = registry.snapshot(
            revision,
            refresh=revision == self.agent_kit.snapshots.current.revision,
        )
        key = (
            revision,
            endpoint_id,
            self.agent_kit.registrations.generation,
            tool_generation,
        )
        existing = self._compiled.get(key)
        if existing is not None:
            return existing
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self._compiled.get(key)
            if existing is not None:
                return existing
            try:
                from meldfabric.agents.native_compiler import NativeStrandsCompiler
            except ImportError as exc:
                raise OptionalDependencyError(
                    "Core dependencies are missing; reinstall meldfabric with dependencies"
                ) from exc
            snapshot = self.agent_kit.snapshots.snapshot(revision)
            storage = await self.agent_kit.storage.initialize_revision(
                snapshot.config,
                make_current=False,
            )
            persistence = await self.agent_kit.persistence.initialize_revision(
                snapshot.config,
                make_current=False,
            )
            compiler = NativeStrandsCompiler(
                model_adapters=self.agent_kit.model_adapters,
                secrets=storage.secrets,
                strands_storages=persistence.strands_storages,
                strands_databases=persistence.strands_databases,
                registration_runtime=self.agent_kit.registrations,
                mcp_manager=self.agent_kit.mcp,
                remote_agent_manager=self.agent_kit.remote_agents,
                skill_manager=self.agent_kit.skills,
                sandbox_manager=self.agent_kit.sandboxes,
                tool_definitions=definitions,
            )
            compiled = await compiler.compile_endpoint(snapshot.config, endpoint_id)
            self.agent_kit.features.require_agent(
                snapshot.config,
                compiled.primary_agent_id,
            )
            self._compiled[key] = compiled
            return compiled

    async def invoke(
        self,
        endpoint_id: str,
        messages: list[dict[str, Any]],
        *,
        user_id: str,
        thread_id: str,
        identity: UserIdentity | None = None,
        remote_context: RemoteCallContext | None = None,
    ) -> tuple[dict[str, Any], int, str]:
        revision = self.agent_kit.snapshots.current.revision
        run_id = f"run_{uuid.uuid4().hex}"
        latest_result: dict[str, Any] = {}
        from meldfabric.agents.native_compiler import NativeExecutionResult

        async for event in self.stream(
            endpoint_id,
            {"messages": messages},
            user_id=user_id,
            thread_id=thread_id,
            run_id=run_id,
            identity=identity,
            remote_context=remote_context,
            revision=revision,
        ):
            if isinstance(event, NativeExecutionResult):
                latest_result = event.as_dict()
        return latest_result, revision, run_id

    async def stream(
        self,
        endpoint_id: str,
        runtime_input: Any,
        *,
        user_id: str,
        thread_id: str,
        run_id: str,
        identity: UserIdentity | None = None,
        remote_context: RemoteCallContext | None = None,
        revision: int | None = None,
        resume_entries: list[dict[str, Any]] | None = None,
    ):
        iterator = self._stream_impl(
            endpoint_id,
            runtime_input,
            user_id=user_id,
            thread_id=thread_id,
            run_id=run_id,
            identity=identity,
            remote_context=remote_context,
            revision=revision,
            resume_entries=resume_entries,
        )
        try:
            async for item in iterator:
                yield item
        except Exception as exc:
            public = public_run_error(exc)
            if public is exc:
                raise
            logger.warning("Agent run %s failed (%s)", run_id, type(exc).__name__)
            raise public from exc
        finally:
            await iterator.aclose()

    async def _stream_impl(
        self,
        endpoint_id: str,
        runtime_input: Any,
        *,
        user_id: str,
        thread_id: str,
        run_id: str,
        identity: UserIdentity | None = None,
        remote_context: RemoteCallContext | None = None,
        revision: int | None = None,
        resume_entries: list[dict[str, Any]] | None = None,
    ):
        if self.agent_kit.runtime_suspended:
            raise ServiceSuspendedError(
                "this service is suspended until the runtime process restarts"
            )
        if not self.agent_kit.accepts_new_runs:
            error = self.agent_kit.last_reload_error
            raise ConfigurationRevisionUnavailableError(
                "this runtime instance could not adopt the active configuration generation",
                details={
                    "local_generation": self.agent_kit.snapshots.current.revision,
                    "exception_type": type(error).__name__ if error is not None else None,
                },
            )
        selected_revision = revision or self.agent_kit.snapshots.current.revision
        pause_key = (endpoint_id, user_id, thread_id)
        interrupt_ids: set[str] = set()
        completed = False
        async with self._pinned_snapshot(selected_revision) as snapshot:
            compiled = await self.compiled_endpoint(snapshot.revision, endpoint_id)
            endpoint = next(item for item in snapshot.config.endpoints if item.id == endpoint_id)
            agent = next(
                item for item in snapshot.config.agents if item.id == compiled.primary_agent_id
            )
            session_database_ref = (
                agent.session_manager.database_ref if agent.session_manager is not None else None
            )
            if endpoint.target_type == "orchestration":
                orchestration = next(
                    item
                    for item in snapshot.config.orchestrations
                    if item.id == endpoint.target_ref
                )
                orchestration_session = getattr(orchestration, "session_manager", None)
                session_database_ref = (
                    orchestration_session.database_ref
                    if orchestration_session is not None
                    else None
                )
            observation = await self.agent_kit.observability.for_run(
                snapshot.config,
                agent,
                run_id=run_id,
                user_id=user_id,
                thread_id=thread_id,
                remote_context=(
                    remote_context.model_dump(mode="json") if remote_context is not None else None
                ),
            )
            session_identity = (snapshot.config.workspace.id, endpoint_id, user_id, thread_id)
            internal_thread_id = "meldfabric_" + identity_digest(*session_identity)
            system_repository = self.agent_kit.storage.ready_system_for(snapshot.revision)
            async with self._exclusive_thread(
                snapshot.config.workspace.id,
                endpoint_id,
                user_id,
                thread_id,
                run_id=run_id,
            ) as lease:
                if system_repository is not None:
                    # Preserve earlier checkpoints only when their recorded owner matches
                    # all identity fields; a delimiter collision must never adopt them.
                    existing_thread = await system_repository.find_agent_session(
                        workspace_id=snapshot.config.workspace.id,
                        agent_id=endpoint_id,
                        user_id=user_id,
                        external_thread_id=thread_id,
                    )
                    if existing_thread is not None:
                        internal_thread_id = existing_thread
                    await system_repository.record_agent_session(
                        workspace_id=snapshot.config.workspace.id,
                        agent_id=endpoint_id,
                        user_id=user_id,
                        external_thread_id=thread_id,
                        internal_thread_id=internal_thread_id,
                        session_database_ref=session_database_ref,
                        memory_database_ref=(
                            agent.memory_manager.database_ref
                            if agent.memory_manager is not None
                            else None
                        ),
                    )
                effective_input = runtime_input
                if resume_entries is not None:
                    effective_input = await compiled.resume_input(
                        resume_entries,
                        user_id=user_id,
                        thread_id=internal_thread_id,
                    )
                control = await self._register_run(
                    run_id=run_id,
                    endpoint_id=endpoint_id,
                    user_id=user_id,
                    thread_id=thread_id,
                    resume=resume_entries is not None,
                    revision=selected_revision,
                    workspace_id=snapshot.config.workspace.id,
                    internal_thread_id=internal_thread_id,
                    system_repository=system_repository,
                    lease=lease,
                )
                iterator = compiled.stream(
                    effective_input,
                    user_id=user_id,
                    thread_id=internal_thread_id,
                    remote_context=remote_context,
                    metadata=observation.metadata,
                    trace_attributes=observation.trace_attributes,
                    identity=identity,
                    endpoint_id=endpoint_id,
                    run_id=run_id,
                ).__aiter__()
                events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1)

                async def consume_stream() -> None:
                    """Keep every Strands ``anext`` call in one asyncio context.

                    Strands and OpenTelemetry attach context variables for the lifetime of
                    an async generator.  Calling successive ``anext`` operations from newly
                    created tasks detaches those tokens in a different context.  A dedicated
                    producer task preserves the context while this method independently waits
                    for distributed cancellation requests.
                    """

                    try:
                        async for streamed_item in iterator:
                            await events.put(("item", streamed_item))
                    except asyncio.CancelledError:
                        raise
                    except BaseException as exc:
                        await events.put(("error", exc))
                    else:
                        await events.put(("done", None))
                    finally:
                        close = getattr(iterator, "aclose", None)
                        if close is not None:
                            await close()

                producer = asyncio.create_task(
                    consume_stream(),
                    name=f"meldfabric-strands-stream-{run_id}",
                )
                cancellation = asyncio.create_task(self._wait_for_cancellation(control))
                next_event: asyncio.Task[tuple[str, Any]] | None = None
                try:
                    while True:
                        next_event = asyncio.create_task(events.get())
                        done, _pending = await asyncio.wait(
                            {next_event, cancellation},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if cancellation in done:
                            next_event.cancel()
                            await asyncio.gather(next_event, return_exceptions=True)
                            producer.cancel()
                            await asyncio.gather(producer, return_exceptions=True)
                            if control.abort_error is not None:
                                raise control.abort_error
                            raise RunCancelledError(
                                f"run {run_id!r} was cancelled",
                                details={"run_id": run_id},
                            )
                        event_kind, item = next_event.result()
                        if event_kind == "done":
                            completed = True
                            break
                        if event_kind == "error":
                            raise item
                        from meldfabric.agents.native_compiler import NativeExecutionResult

                        if isinstance(item, NativeExecutionResult) and item.interrupts:
                            interrupt_ids.update(
                                str(interrupt.get("id") or interrupt.get("interruptId") or "")
                                for interrupt in item.interrupts
                                if interrupt.get("id") or interrupt.get("interruptId")
                            )
                        yield item
                except Exception as exc:
                    public = public_run_error(exc)
                    if public is exc:
                        raise
                    logger.warning("Agent run %s failed (%s)", run_id, type(exc).__name__)
                    raise public from exc
                finally:
                    cancellation.cancel()
                    await asyncio.gather(cancellation, return_exceptions=True)
                    if next_event is not None and not next_event.done():
                        next_event.cancel()
                        await asyncio.gather(next_event, return_exceptions=True)
                    if not producer.done():
                        producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
                    await self._finish_run(
                        control,
                        pause_key=pause_key,
                        revision=snapshot.revision,
                        interrupt_ids=interrupt_ids,
                        completed=completed,
                        was_resume=resume_entries is not None,
                    )

    @asynccontextmanager
    async def _exclusive_thread(
        self,
        workspace_id: str,
        endpoint_id: str,
        user_id: str,
        thread_id: str,
        *,
        run_id: str | None = None,
    ):
        repository = await self.agent_kit.storage.control_repository(self.agent_kit.runtime_config)
        await self._register_instance(repository)
        lease = await repository.acquire_thread_lease(
            workspace_id=workspace_id,
            agent_id=endpoint_id,
            user_id=user_id,
            external_thread_id=thread_id,
            owner_instance_id=self.instance_id,
            run_id=run_id or f"lease_{uuid.uuid4().hex}",
            ttl_seconds=self.lease_ttl_seconds,
        )
        try:
            yield lease
        finally:
            try:
                await repository.release_thread_lease(lease)
            except Exception:
                # The lease expires even when cleanup cannot reach the database.
                logger.exception("Failed to release thread lease %s", lease.id)

    async def revision_for_stream(
        self,
        *,
        endpoint_id: str,
        user_id: str,
        thread_id: str,
        resume: bool,
    ) -> int:
        pause_key = (endpoint_id, user_id, thread_id)
        async with self._run_locks.hold(pause_key):
            paused = self._paused_runs.get(pause_key)
            if paused is None:
                stored = None
                for repository in self.agent_kit.storage.all_system_repositories():
                    stored = await repository.get_paused_agent_run(
                        workspace_id=self.agent_kit.runtime_config.workspace.id,
                        agent_id=endpoint_id,
                        user_id=user_id,
                        external_thread_id=thread_id,
                    )
                    if stored is not None:
                        break
                if stored is not None:
                    await self.agent_kit.restore_runtime_snapshot(
                        stored.config_generation,
                        retain=True,
                    )
                    paused = PausedRun(
                        run_id=stored.run_id,
                        revision=stored.config_generation,
                        record_id=stored.id,
                    )
                    self._paused_runs[pause_key] = paused
            if resume:
                if paused is None:
                    raise RunConflictError(
                        "cannot resume a thread without an interrupted run",
                        details={"endpoint_id": endpoint_id, "thread_id": thread_id},
                    )
                return paused.revision
            if paused is not None:
                raise RunConflictError(
                    "the thread has a pending interrupt and must be resumed or abandoned",
                    details={
                        "endpoint_id": endpoint_id,
                        "thread_id": thread_id,
                        "paused_run_id": paused.run_id,
                    },
                )
            return self.agent_kit.snapshots.current.revision

    async def interrupted_run_id(
        self,
        *,
        endpoint_id: str,
        user_id: str,
        thread_id: str,
    ) -> str | None:
        """Return the paused run identity without changing snapshot retention."""

        pause_key = (endpoint_id, user_id, thread_id)
        async with self._run_locks.hold(pause_key):
            paused = self._paused_runs.get(pause_key)
            if paused is not None:
                return paused.run_id
            for repository in self.agent_kit.storage.all_system_repositories():
                stored = await repository.get_paused_agent_run(
                    workspace_id=self.agent_kit.runtime_config.workspace.id,
                    agent_id=endpoint_id,
                    user_id=user_id,
                    external_thread_id=thread_id,
                )
                if stored is not None:
                    return stored.run_id
            return None

    async def cancel_run(
        self,
        run_id: str,
        *,
        endpoint_id: str,
        user_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        lock_key: tuple[str, str, str] | None = None
        if thread_id is not None and user_id is not None:
            lock_key = (endpoint_id, user_id, thread_id)
        else:
            # A caller may omit ``thread_id`` when its run id is unambiguous.  Use the
            # local run index to recover the same session-scoped lock when possible;
            # a run owned by another process is coordinated by the database update.
            matches = self._matching_active_runs(
                run_id,
                endpoint_id=endpoint_id,
                user_id=user_id,
                thread_id=thread_id,
            )
            if len(matches) > 1:
                raise RunConflictError(
                    "run id is ambiguous; provide user identity and thread_id",
                    details={"run_id": run_id, "endpoint_id": endpoint_id},
                )
            if len(matches) == 1:
                matched = matches[0]
                lock_key = (matched.endpoint_id, matched.user_id, matched.thread_id)

        if lock_key is None:
            await self._cancel_run_locked(
                run_id,
                endpoint_id=endpoint_id,
                user_id=user_id,
                thread_id=thread_id,
            )
            return
        async with self._run_locks.hold(lock_key):
            await self._cancel_run_locked(
                run_id,
                endpoint_id=endpoint_id,
                user_id=user_id,
                thread_id=thread_id,
            )

    async def _cancel_run_locked(
        self,
        run_id: str,
        *,
        endpoint_id: str,
        user_id: str | None,
        thread_id: str | None,
    ) -> None:
        matches = self._matching_active_runs(
            run_id,
            endpoint_id=endpoint_id,
            user_id=user_id,
            thread_id=thread_id,
        )
        if len(matches) > 1:
            raise RunConflictError(
                "run id is ambiguous; provide user identity and thread_id",
                details={"run_id": run_id, "endpoint_id": endpoint_id},
            )
        # Notify a run owned by this process before waiting for database I/O.  Its
        # cancellation watcher can stop inference immediately while the durable flag is
        # propagated for other processes.
        for control in matches:
            control.cancel_requested.set()

        persisted = 0
        for repository in self.agent_kit.storage.all_system_repositories():
            persisted += await repository.request_run_cancellation(
                workspace_id=self.agent_kit.runtime_config.workspace.id,
                agent_id=endpoint_id,
                user_id=user_id,
                run_id=run_id,
                external_thread_id=thread_id,
            )
        if not matches and persisted == 0:
            raise RunNotFoundError(
                f"active run {run_id!r} was not found",
                details={"run_id": run_id, "endpoint_id": endpoint_id},
            )

    def _matching_active_runs(
        self,
        run_id: str,
        *,
        endpoint_id: str,
        user_id: str | None,
        thread_id: str | None,
    ) -> list[ActiveRun]:
        return [
            item
            for item in self._active_runs.values()
            if item.run_id == run_id
            and item.endpoint_id == endpoint_id
            and (user_id is None or item.user_id == user_id)
            and (thread_id is None or item.thread_id == thread_id)
        ]

    async def abandon_interrupted_run(
        self,
        run_id: str,
        *,
        endpoint_id: str,
        user_id: str,
        thread_id: str,
    ) -> None:
        """Cancel a persisted input-required run without attempting to resume it."""

        pause_key = (endpoint_id, user_id, thread_id)
        async with self._run_locks.hold(pause_key):
            paused = self._paused_runs.get(pause_key)
            repository = None
            if paused is None:
                stored = None
                for candidate in self.agent_kit.storage.all_system_repositories():
                    stored = await candidate.get_paused_agent_run(
                        workspace_id=self.agent_kit.runtime_config.workspace.id,
                        agent_id=endpoint_id,
                        user_id=user_id,
                        external_thread_id=thread_id,
                    )
                    if stored is not None:
                        repository = candidate
                        break
                if stored is None or stored.run_id != run_id:
                    raise RunNotFoundError(
                        f"interrupted run {run_id!r} was not found",
                        details={"run_id": run_id, "endpoint_id": endpoint_id},
                    )
                paused = PausedRun(
                    run_id=stored.run_id,
                    revision=stored.config_generation,
                    record_id=stored.id,
                )
            elif paused.run_id != run_id:
                raise RunNotFoundError(
                    f"interrupted run {run_id!r} was not found",
                    details={"run_id": run_id, "endpoint_id": endpoint_id},
                )
            else:
                repository = self.agent_kit.storage.ready_system_for(paused.revision)

            was_retained_here = self._paused_runs.pop(pause_key, None) is not None
            if was_retained_here:
                self.agent_kit.snapshots.release(paused.revision)
            if repository is not None and paused.record_id is not None:
                await repository.finish_agent_run(paused.record_id, status="cancelled")
        await self.aprune()

    async def _register_run(
        self,
        *,
        run_id: str,
        endpoint_id: str,
        user_id: str,
        thread_id: str,
        resume: bool,
        revision: int,
        workspace_id: str,
        internal_thread_id: str,
        system_repository: SystemRepository | None,
        lease: RuntimeLease,
    ) -> ActiveRun:
        pause_key = (endpoint_id, user_id, thread_id)
        async with self._run_locks.hold(pause_key):
            if any(
                item.run_id == run_id
                and item.endpoint_id == endpoint_id
                and item.user_id == user_id
                and item.thread_id == thread_id
                for item in self._active_runs.values()
            ):
                raise RunAlreadyActiveError(
                    f"run id {run_id!r} is already active",
                    details={"run_id": run_id},
                )
            if any(
                item.endpoint_id == endpoint_id
                and item.user_id == user_id
                and item.thread_id == thread_id
                for item in self._active_runs.values()
            ):
                raise ThreadBusyError(
                    "another run is already active for this Agent thread",
                    details={"endpoint_id": endpoint_id, "thread_id": thread_id},
                )
            paused = self._paused_runs.get(pause_key)
            if paused is None and system_repository is not None:
                stored_paused = await system_repository.get_paused_agent_run(
                    workspace_id=workspace_id,
                    agent_id=endpoint_id,
                    user_id=user_id,
                    external_thread_id=thread_id,
                )
                if stored_paused is not None:
                    await self.agent_kit.restore_runtime_snapshot(
                        stored_paused.config_generation,
                        retain=True,
                    )
                    paused = PausedRun(
                        run_id=stored_paused.run_id,
                        revision=stored_paused.config_generation,
                        record_id=stored_paused.id,
                    )
                    self._paused_runs[pause_key] = paused
            if resume and (paused is None or paused.revision != revision):
                raise RunConflictError("the interrupted run revision is no longer available")
            if not resume and paused is not None:
                raise ThreadAwaitingResumeError(
                    "the thread has a pending interrupt",
                    details={
                        "endpoint_id": endpoint_id,
                        "thread_id": thread_id,
                        "paused_run_id": paused.run_id,
                    },
                )
            control = ActiveRun(
                run_id=run_id,
                endpoint_id=endpoint_id,
                user_id=user_id,
                thread_id=thread_id,
                cancel_requested=asyncio.Event(),
                lease=lease,
            )
            if system_repository is not None:
                stored = await system_repository.start_agent_run(
                    workspace_id=workspace_id,
                    agent_id=endpoint_id,
                    user_id=user_id,
                    external_thread_id=thread_id,
                    internal_thread_id=internal_thread_id,
                    run_id=run_id,
                    config_generation=revision,
                    parent_run_id=(paused.run_id if resume and paused is not None else None),
                    owner_instance_id=self.instance_id,
                    fencing_token=lease.fencing_token,
                )
                if stored is None:
                    raise RunIdAlreadyUsedError(
                        "run id has already been used on this thread",
                        details={"run_id": run_id, "thread_id": thread_id},
                    )
                control.record_id = stored.id
                control.system_repository = system_repository
            active_key = (endpoint_id, user_id, thread_id, run_id)
            self._active_runs[active_key] = control
            return control

    async def _finish_run(
        self,
        control: ActiveRun,
        *,
        pause_key: tuple[str, str, str],
        revision: int,
        interrupt_ids: set[str],
        completed: bool,
        was_resume: bool,
    ) -> None:
        async with self._run_locks.hold(pause_key):
            active_key = (
                control.endpoint_id,
                control.user_id,
                control.thread_id,
                control.run_id,
            )
            self._active_runs.pop(active_key, None)
            previous = self._paused_runs.get(pause_key)
            if interrupt_ids:
                if previous is None:
                    self.agent_kit.snapshots.retain(revision)
                self._paused_runs[pause_key] = PausedRun(
                    run_id=control.run_id,
                    revision=revision,
                    record_id=control.record_id,
                )
            elif completed and was_resume and previous is not None:
                self._paused_runs.pop(pause_key, None)
                self.agent_kit.snapshots.release(previous.revision)
            repository = control.system_repository
            if repository is not None and control.record_id is not None:
                if interrupt_ids:
                    status = "interrupted"
                elif control.cancel_requested.is_set():
                    status = "cancelled"
                elif completed:
                    status = "completed"
                else:
                    status = "failed"
                await repository.finish_agent_run(
                    control.record_id,
                    status=status,
                    interrupt_ids=sorted(interrupt_ids),
                    terminal_reason=(
                        "RUN_LEASE_LOST"
                        if isinstance(control.abort_error, RunLeaseLostError)
                        else None
                    ),
                    owner_instance_id=self.instance_id,
                    fencing_token=(control.lease.fencing_token if control.lease else None),
                )
                if completed and was_resume and previous and previous.record_id:
                    await repository.finish_agent_run(previous.record_id, status="resumed")

    async def _wait_for_cancellation(self, control: ActiveRun) -> None:
        next_renewal = time.monotonic() + self.lease_renew_interval_seconds
        last_successful_renewal = time.monotonic()
        while True:
            if control.cancel_requested.is_set():
                return
            repository = control.system_repository
            now = time.monotonic()
            safe_lease_age = max(
                self.lease_renew_interval_seconds,
                self.lease_ttl_seconds - self.lease_abort_margin_seconds,
            )
            if (
                repository is not None
                and control.lease is not None
                and now - last_successful_renewal >= safe_lease_age
            ):
                control.abort_error = RunLeaseLostError(
                    "the execution lease for this Agent thread was lost",
                    details={
                        "run_id": control.run_id,
                        "endpoint_id": control.endpoint_id,
                        "thread_id": control.thread_id,
                    },
                )
                control.cancel_requested.set()
                return
            if repository is not None and control.lease is not None and now >= next_renewal:
                lease_rejected = False
                try:
                    renewed = await asyncio.wait_for(
                        repository.renew_thread_lease(
                            control.lease,
                            ttl_seconds=self.lease_ttl_seconds,
                            config_generation=self.agent_kit.snapshots.current.revision,
                        ),
                        timeout=self.coordination_io_timeout_seconds,
                    )
                except Exception:
                    renewed = None
                    logger.exception("Failed to renew thread lease %s", control.lease.id)
                else:
                    lease_rejected = renewed is None
                if renewed is not None:
                    control.lease = renewed
                    last_successful_renewal = time.monotonic()
                elif lease_rejected:
                    # A completed conditional update returning no row is definitive:
                    # this owner was fenced or the lease already expired. Unlike an I/O
                    # failure, there is no safe grace period left.
                    control.abort_error = RunLeaseLostError(
                        "the execution lease for this Agent thread was lost",
                        details={
                            "run_id": control.run_id,
                            "endpoint_id": control.endpoint_id,
                            "thread_id": control.thread_id,
                        },
                    )
                    control.cancel_requested.set()
                    return
                next_renewal = time.monotonic() + self.lease_renew_interval_seconds
            if repository is not None and control.record_id is not None:
                try:
                    cancellation_requested = await asyncio.wait_for(
                        repository.is_run_cancellation_requested(control.record_id),
                        timeout=self.coordination_io_timeout_seconds,
                    )
                except Exception:
                    # Cancellation polling shares the coordination database with lease
                    # renewal. A transient outage must not be mistaken for a user cancel;
                    # the renewal deadline above is the authority for safely aborting.
                    cancellation_requested = False
                if cancellation_requested:
                    control.cancel_requested.set()
                    return
            try:
                await asyncio.wait_for(
                    control.cancel_requested.wait(),
                    timeout=min(0.25, self.lease_renew_interval_seconds),
                )
            except TimeoutError:
                continue
            return

    def prune(self) -> None:
        active = set(self.agent_kit.snapshots.active_revisions())
        for key in list(self._compiled):
            if key[0] not in active:
                self._compiled.pop(key, None)
                self._locks.pop(key, None)

    async def aprune(self) -> None:
        self.prune()
        active = set(self.agent_kit.snapshots.active_revisions())
        await self.agent_kit.storage.prune(active)
        await self.agent_kit.persistence.prune(active)

    @asynccontextmanager
    async def _pinned_snapshot(self, revision: int):
        try:
            with self.agent_kit.snapshots.pin(revision) as snapshot:
                yield snapshot
        finally:
            await self.aprune()
