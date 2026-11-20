"""MeldFabric system metadata repository."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from meldfabric.domain.identity import identity_digest
from meldfabric.errors import (
    RunAlreadyActiveError,
    RunConflictError,
    ThreadBusyError,
)
from meldfabric.storage.database import AsyncDatabase
from meldfabric.storage.migrations import SystemSchemaMigrator
from meldfabric.storage.schema import (
    A2ATaskRecord,
    AgentRunRecord,
    AgentSessionRecord,
    DeletedAgentResidualRecord,
    RemoteInteractionRecord,
    RemoteSessionMappingRecord,
    RuntimeInstanceRecord,
    ThreadLeaseRecord,
    ToolExecutionRecord,
)
from meldfabric.storage.sql import affected_rows
from meldfabric.storage.types import (
    RemoteSessionMapping,
    RuntimeLease,
    StoredA2ATask,
    StoredAgentRun,
    StoredRemoteInteraction,
    StoredToolExecution,
)


class SystemRepository:
    def __init__(self, database: AsyncDatabase) -> None:
        self.database = database
        self.schema_version = 0

    async def initialize(self) -> None:
        self.schema_version = await SystemSchemaMigrator(self.database.engine).migrate()
        await self.cleanup_expired_runtime_state()

    @staticmethod
    def thread_lease_id(
        workspace_id: str,
        agent_id: str,
        user_id: str,
        external_thread_id: str,
    ) -> str:
        return identity_digest(workspace_id, agent_id, user_id, external_thread_id)

    @staticmethod
    async def _database_now(session) -> datetime:
        value = await session.scalar(select(func.current_timestamp()))
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime):
            value = datetime.now(UTC)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def register_runtime_instance(
        self,
        *,
        instance_id: str,
        workspace_id: str,
        hostname: str,
        process_id: int,
        config_generation: int,
        catalog_digest: str,
    ) -> None:
        async with self.database.session() as session, session.begin():
            now = await self._database_now(session)
            record = await session.get(RuntimeInstanceRecord, instance_id)
            if record is None:
                session.add(
                    RuntimeInstanceRecord(
                        id=instance_id,
                        workspace_id=workspace_id,
                        hostname=hostname,
                        process_id=process_id,
                        status="ready",
                        config_generation=config_generation,
                        catalog_digest=catalog_digest,
                        started_at=now,
                        last_heartbeat_at=now,
                    )
                )
            else:
                record.workspace_id = workspace_id
                record.hostname = hostname
                record.process_id = process_id
                record.status = "ready"
                record.config_generation = config_generation
                record.catalog_digest = catalog_digest
                record.last_heartbeat_at = now
                record.stopped_at = None

    async def heartbeat_runtime_instance(
        self,
        *,
        instance_id: str,
        config_generation: int,
        catalog_digest: str,
        status: str = "ready",
    ) -> bool:
        async with self.database.session() as session, session.begin():
            now = await self._database_now(session)
            result = await session.execute(
                update(RuntimeInstanceRecord)
                .where(RuntimeInstanceRecord.id == instance_id)
                .values(
                    # Suspension is a process-lifetime control flag. A heartbeat that
                    # began just before the suspend transaction committed must not
                    # revive the instance to ready after the fact.
                    status=case(
                        (RuntimeInstanceRecord.status == "suspended", "suspended"),
                        else_=status,
                    ),
                    config_generation=config_generation,
                    catalog_digest=catalog_digest,
                    last_heartbeat_at=now,
                    stopped_at=None,
                )
            )
            return bool(affected_rows(result))

    async def runtime_instance_is_suspended(self, instance_id: str) -> bool:
        async with self.database.session() as session:
            status = await session.scalar(
                select(RuntimeInstanceRecord.status).where(RuntimeInstanceRecord.id == instance_id)
            )
            return status == "suspended"

    async def suspend_runtime_instances(self, workspace_id: str) -> int:
        """Suspend instances that belong to the current service run.

        Instance identifiers are regenerated on process restart, so this operational flag
        deliberately does not survive a restart and never becomes configuration state.
        """

        async with self.database.session() as session, session.begin():
            result = await session.execute(
                update(RuntimeInstanceRecord)
                .where(
                    RuntimeInstanceRecord.workspace_id == workspace_id,
                    RuntimeInstanceRecord.status.notin_(["lost", "stopped"]),
                )
                .values(status="suspended")
            )
            return affected_rows(result)

    async def stop_runtime_instance(self, instance_id: str) -> None:
        async with self.database.session() as session, session.begin():
            now = await self._database_now(session)
            await session.execute(
                update(RuntimeInstanceRecord)
                .where(RuntimeInstanceRecord.id == instance_id)
                .values(status="stopped", last_heartbeat_at=now, stopped_at=now)
            )

    async def acquire_thread_lease(
        self,
        *,
        workspace_id: str,
        agent_id: str,
        user_id: str,
        external_thread_id: str,
        owner_instance_id: str,
        run_id: str,
        ttl_seconds: int,
    ) -> RuntimeLease:
        """Atomically acquire or take over one expired logical-thread lease."""

        lease_id = self.thread_lease_id(workspace_id, agent_id, user_id, external_thread_id)
        for _attempt in range(2):
            try:
                async with self.database.session() as session, session.begin():
                    now = await self._database_now(session)
                    expires_at = now + timedelta(seconds=ttl_seconds)
                    takeover = await session.execute(
                        update(ThreadLeaseRecord)
                        .where(
                            ThreadLeaseRecord.id == lease_id,
                            ThreadLeaseRecord.expires_at <= now,
                        )
                        .values(
                            owner_instance_id=owner_instance_id,
                            run_id=run_id,
                            fencing_token=ThreadLeaseRecord.fencing_token + 1,
                            expires_at=expires_at,
                            updated_at=now,
                        )
                    )
                    if affected_rows(takeover):
                        await session.execute(
                            update(AgentRunRecord)
                            .where(
                                AgentRunRecord.workspace_id == workspace_id,
                                AgentRunRecord.agent_id == agent_id,
                                AgentRunRecord.user_id == user_id,
                                AgentRunRecord.external_thread_id == external_thread_id,
                                AgentRunRecord.status == "running",
                            )
                            .values(
                                status="abandoned",
                                terminal_reason="THREAD_LEASE_EXPIRED",
                                updated_at=now,
                            )
                        )
                        record = await session.get(ThreadLeaseRecord, lease_id)
                        assert record is not None
                        return self._runtime_lease(record)

                    current = await session.get(ThreadLeaseRecord, lease_id)
                    if current is not None:
                        if current.run_id == run_id:
                            raise RunAlreadyActiveError(
                                f"run id {run_id!r} is already active",
                                details={
                                    "run_id": run_id,
                                    "agent_id": agent_id,
                                    "thread_id": external_thread_id,
                                },
                            )
                        raise ThreadBusyError(
                            "another run is already active for this Agent thread",
                            details={
                                "agent_id": agent_id,
                                "thread_id": external_thread_id,
                                "active_run_id": current.run_id,
                            },
                        )

                    record = ThreadLeaseRecord(
                        id=lease_id,
                        workspace_id=workspace_id,
                        agent_id=agent_id,
                        user_id=user_id,
                        external_thread_id=external_thread_id,
                        owner_instance_id=owner_instance_id,
                        run_id=run_id,
                        fencing_token=1,
                        expires_at=expires_at,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(record)
                    await session.flush()
                    return self._runtime_lease(record)
            except IntegrityError:
                # Two empty-database contenders can both observe no row. The unique
                # key decides the winner; the retry reports the stable business error.
                continue

        async with self.database.session() as session:
            current = await session.get(ThreadLeaseRecord, lease_id)
            if current is not None and current.run_id == run_id:
                raise RunAlreadyActiveError(
                    f"run id {run_id!r} is already active",
                    details={"run_id": run_id, "thread_id": external_thread_id},
                )
            raise ThreadBusyError(
                "another run is already active for this Agent thread",
                details={
                    "agent_id": agent_id,
                    "thread_id": external_thread_id,
                    "active_run_id": current.run_id if current is not None else None,
                },
            )

    async def renew_thread_lease(
        self,
        lease: RuntimeLease,
        *,
        ttl_seconds: int,
        config_generation: int,
    ) -> RuntimeLease | None:
        async with self.database.session() as session, session.begin():
            now = await self._database_now(session)
            expires_at = now + timedelta(seconds=ttl_seconds)
            result = await session.execute(
                update(ThreadLeaseRecord)
                .where(
                    ThreadLeaseRecord.id == lease.id,
                    ThreadLeaseRecord.owner_instance_id == lease.owner_instance_id,
                    ThreadLeaseRecord.fencing_token == lease.fencing_token,
                    ThreadLeaseRecord.expires_at > now,
                )
                .values(expires_at=expires_at, updated_at=now)
            )
            await session.execute(
                update(RuntimeInstanceRecord)
                .where(RuntimeInstanceRecord.id == lease.owner_instance_id)
                .values(
                    config_generation=config_generation,
                    last_heartbeat_at=now,
                )
            )
            if not affected_rows(result):
                return None
            return RuntimeLease(
                id=lease.id,
                workspace_id=lease.workspace_id,
                agent_id=lease.agent_id,
                user_id=lease.user_id,
                thread_id=lease.thread_id,
                owner_instance_id=lease.owner_instance_id,
                run_id=lease.run_id,
                fencing_token=lease.fencing_token,
                expires_at=expires_at,
            )

    async def release_thread_lease(self, lease: RuntimeLease) -> bool:
        async with self.database.session() as session, session.begin():
            result = await session.execute(
                delete(ThreadLeaseRecord).where(
                    ThreadLeaseRecord.id == lease.id,
                    ThreadLeaseRecord.owner_instance_id == lease.owner_instance_id,
                    ThreadLeaseRecord.fencing_token == lease.fencing_token,
                )
            )
            return bool(affected_rows(result))

    async def cleanup_expired_runtime_state(
        self,
        *,
        instance_timeout_seconds: int = 60,
    ) -> dict[str, int]:
        """Fence abandoned work without attempting unsafe automatic replay."""

        async with self.database.session() as session, session.begin():
            now = await self._database_now(session)
            expired = list(
                (
                    await session.scalars(
                        select(ThreadLeaseRecord).where(ThreadLeaseRecord.expires_at <= now)
                    )
                ).all()
            )
            abandoned_runs = 0
            for lease in expired:
                result = await session.execute(
                    update(AgentRunRecord)
                    .where(
                        AgentRunRecord.workspace_id == lease.workspace_id,
                        AgentRunRecord.agent_id == lease.agent_id,
                        AgentRunRecord.user_id == lease.user_id,
                        AgentRunRecord.external_thread_id == lease.external_thread_id,
                        AgentRunRecord.owner_instance_id == lease.owner_instance_id,
                        AgentRunRecord.fencing_token == lease.fencing_token,
                        AgentRunRecord.status == "running",
                    )
                    .values(
                        status="abandoned",
                        terminal_reason="THREAD_LEASE_EXPIRED",
                        updated_at=now,
                    )
                )
                abandoned_runs += affected_rows(result)
                await session.delete(lease)

            cutoff = now - timedelta(seconds=instance_timeout_seconds)
            stale_instances = list(
                (
                    await session.scalars(
                        select(RuntimeInstanceRecord).where(
                            RuntimeInstanceRecord.status.in_(
                                ["ready", "draining", "suspended", "configuration_error"]
                            ),
                            RuntimeInstanceRecord.last_heartbeat_at <= cutoff,
                        )
                    )
                ).all()
            )
            stale_ids = {item.id for item in stale_instances}
            for instance in stale_instances:
                instance.status = "lost"
                instance.stopped_at = now

            abandoned_tasks = 0
            if stale_ids:
                tasks = list(
                    (
                        await session.scalars(
                            select(A2ATaskRecord).where(
                                A2ATaskRecord.owner_instance_id.in_(stale_ids),
                                A2ATaskRecord.state.in_(["submitted", "working"]),
                            )
                        )
                    ).all()
                )
                for task_record in tasks:
                    payload = dict(task_record.task)
                    payload["status"] = {
                        "state": "failed",
                        "message": {
                            "kind": "message",
                            "messageId": f"message_{uuid.uuid4().hex}",
                            "role": "agent",
                            "parts": [
                                {
                                    "kind": "text",
                                    "text": (
                                        "The runtime instance executing this task "
                                        "became unavailable."
                                    ),
                                }
                            ],
                            "taskId": task_record.id,
                            "contextId": task_record.context_id,
                        },
                        "timestamp": now.isoformat(),
                    }
                    payload.setdefault("metadata", {}).setdefault("meldfabric", {})[
                        "failureReason"
                    ] = "INSTANCE_LEASE_EXPIRED"
                    task_record.task = payload
                    task_record.state = "failed"
                    task_record.owner_instance_id = None
                    task_record.updated_at = now
                    abandoned_tasks += 1

            return {
                "leases": len(expired),
                "runs": abandoned_runs,
                "instances": len(stale_instances),
                "a2a_tasks": abandoned_tasks,
            }

    async def runtime_coordination_status(self) -> dict[str, Any]:
        async with self.database.session() as session:
            instances = list(
                (
                    await session.scalars(
                        select(RuntimeInstanceRecord).order_by(
                            RuntimeInstanceRecord.last_heartbeat_at.desc()
                        )
                    )
                ).all()
            )
            leases = list((await session.scalars(select(ThreadLeaseRecord))).all())
        return {
            "instances": [
                {
                    "id": item.id,
                    "workspace_id": item.workspace_id,
                    "hostname": item.hostname,
                    "process_id": item.process_id,
                    "status": item.status,
                    "config_generation": item.config_generation,
                    "catalog_digest": item.catalog_digest,
                    "started_at": item.started_at,
                    "last_heartbeat_at": item.last_heartbeat_at,
                    "stopped_at": item.stopped_at,
                }
                for item in instances
            ],
            "active_leases": [
                {
                    "workspace_id": item.workspace_id,
                    "agent_id": item.agent_id,
                    "user_id": item.user_id,
                    "thread_id": item.external_thread_id,
                    "owner_instance_id": item.owner_instance_id,
                    "run_id": item.run_id,
                    "fencing_token": item.fencing_token,
                    "expires_at": item.expires_at,
                }
                for item in leases
            ],
        }

    @staticmethod
    def _runtime_lease(record: ThreadLeaseRecord) -> RuntimeLease:
        return RuntimeLease(
            id=record.id,
            workspace_id=record.workspace_id,
            agent_id=record.agent_id,
            user_id=record.user_id,
            thread_id=record.external_thread_id,
            owner_instance_id=record.owner_instance_id,
            run_id=record.run_id,
            fencing_token=record.fencing_token,
            expires_at=record.expires_at,
        )

    async def get_remote_mapping(self, mapping_key: str) -> RemoteSessionMapping | None:
        statement = select(RemoteSessionMappingRecord).where(
            RemoteSessionMappingRecord.mapping_key == mapping_key
        )
        async with self.database.session() as session:
            record = await session.scalar(statement)
            if record is None:
                return None
            return RemoteSessionMapping(id=record.id, remote_thread_id=record.remote_thread_id)

    async def put_remote_mapping(
        self,
        *,
        mapping_key: str,
        local_agent_id: str,
        remote_agent_id: str,
        remote_thread_id: str,
        policy: str,
        user_id: str | None = None,
        parent_thread_id: str | None = None,
    ) -> RemoteSessionMapping:
        record_id = hashlib.sha256(mapping_key.encode("utf-8")).hexdigest()
        try:
            async with self.database.session() as session, session.begin():
                session.add(
                    RemoteSessionMappingRecord(
                        id=record_id,
                        mapping_key=mapping_key,
                        local_agent_id=local_agent_id,
                        remote_agent_id=remote_agent_id,
                        remote_thread_id=remote_thread_id,
                        policy=policy,
                        user_id=user_id,
                        parent_thread_id=parent_thread_id,
                    )
                )
                await session.flush()
            return RemoteSessionMapping(id=record_id, remote_thread_id=remote_thread_id)
        except IntegrityError:
            # Another process won the insert for this deterministic mapping key.
            # Its remote thread is authoritative; never overwrite it with our candidate.
            async with self.database.session() as session:
                record = await session.scalar(
                    select(RemoteSessionMappingRecord).where(
                        RemoteSessionMappingRecord.mapping_key == mapping_key
                    )
                )
                if record is None:
                    raise
                return RemoteSessionMapping(
                    id=record_id,
                    remote_thread_id=record.remote_thread_id,
                )

    async def record_deleted_agent(
        self,
        *,
        agent_id: str,
        database_refs: list[str],
        namespaces: dict[str, str],
        cleanup_types: list[str],
    ) -> str:
        record_id = f"residual_{uuid.uuid4().hex}"
        async with self.database.session() as session, session.begin():
            session.add(
                DeletedAgentResidualRecord(
                    id=record_id,
                    agent_id=agent_id,
                    database_refs=database_refs,
                    namespaces=namespaces,
                    available_cleanup_types=cleanup_types,
                    cleaned_types=[],
                )
            )
        return record_id

    async def list_residuals(self) -> list[DeletedAgentResidualRecord]:
        statement = select(DeletedAgentResidualRecord).order_by(
            DeletedAgentResidualRecord.deleted_at.desc()
        )
        async with self.database.session() as session:
            return list((await session.scalars(statement)).all())

    async def get_residual(self, residual_id: str) -> DeletedAgentResidualRecord | None:
        async with self.database.session() as session:
            return await session.get(DeletedAgentResidualRecord, residual_id)

    async def mark_residual_cleaned(
        self,
        residual_id: str,
        cleanup_types: list[str],
    ) -> bool:
        async with self.database.session() as session, session.begin():
            record = await session.get(DeletedAgentResidualRecord, residual_id)
            if record is None:
                return False
            record.cleaned_types = sorted(set(record.cleaned_types).union(cleanup_types))
            return True

    async def find_agent_session(
        self, *, workspace_id: str, agent_id: str, user_id: str, external_thread_id: str
    ) -> str | None:
        statement = select(AgentSessionRecord.internal_thread_id).where(
            AgentSessionRecord.workspace_id == workspace_id,
            AgentSessionRecord.agent_id == agent_id,
            AgentSessionRecord.user_id == user_id,
            AgentSessionRecord.external_thread_id == external_thread_id,
            # Explicit namespace partitions use a separate prefix. Returning one
            # here would silently adopt it when switching back to the default.
            ~AgentSessionRecord.internal_thread_id.startswith(
                "meldfabric_namespace_", autoescape=True
            ),
        )
        async with self.database.session() as session:
            return await session.scalar(statement)

    async def record_agent_session(
        self,
        *,
        workspace_id: str,
        agent_id: str,
        user_id: str,
        external_thread_id: str,
        internal_thread_id: str,
        session_database_ref: str | None,
        memory_database_ref: str | None,
    ) -> None:
        record_id = hashlib.sha256(internal_thread_id.encode("utf-8")).hexdigest()
        async with self.database.session() as session, session.begin():
            record = await session.get(AgentSessionRecord, record_id)
            if record is None:
                session.add(
                    AgentSessionRecord(
                        id=record_id,
                        workspace_id=workspace_id,
                        agent_id=agent_id,
                        user_id=user_id,
                        external_thread_id=external_thread_id,
                        internal_thread_id=internal_thread_id,
                        session_database_ref=session_database_ref,
                        memory_database_ref=memory_database_ref,
                    )
                )

    async def list_agent_sessions(self, agent_id: str) -> list[AgentSessionRecord]:
        statement = select(AgentSessionRecord).where(AgentSessionRecord.agent_id == agent_id)
        async with self.database.session() as session:
            return list((await session.scalars(statement)).all())

    async def delete_agent_sessions(self, agent_id: str) -> int:
        async with self.database.session() as session, session.begin():
            result = await session.execute(
                delete(AgentSessionRecord).where(AgentSessionRecord.agent_id == agent_id)
            )
            return affected_rows(result)

    async def delete_agent_run_records(self, agent_id: str) -> int:
        async with self.database.session() as session, session.begin():
            deleted = 0
            for model in (
                AgentSessionRecord,
                AgentRunRecord,
                A2ATaskRecord,
                ToolExecutionRecord,
            ):
                result = await session.execute(delete(model).where(model.agent_id == agent_id))
                deleted += affected_rows(result)
            leases = await session.execute(
                delete(ThreadLeaseRecord).where(ThreadLeaseRecord.agent_id == agent_id)
            )
            deleted += affected_rows(leases)
            remote_interactions = await session.execute(
                delete(RemoteInteractionRecord).where(
                    RemoteInteractionRecord.local_agent_id == agent_id
                )
            )
            deleted += affected_rows(remote_interactions)
            return deleted

    async def delete_remote_mappings(self, local_agent_id: str) -> int:
        async with self.database.session() as session, session.begin():
            mappings = await session.execute(
                delete(RemoteSessionMappingRecord).where(
                    RemoteSessionMappingRecord.local_agent_id == local_agent_id
                )
            )
            interactions = await session.execute(
                delete(RemoteInteractionRecord).where(
                    RemoteInteractionRecord.local_agent_id == local_agent_id
                )
            )
            return affected_rows(mappings) + affected_rows(interactions)

    @staticmethod
    def remote_interaction_id(
        *,
        workspace_id: str,
        local_agent_id: str,
        binding_id: str,
        parent_thread_id: str,
    ) -> str:
        identity = ":".join([workspace_id, local_agent_id, binding_id, parent_thread_id])
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    async def get_remote_interaction(
        self,
        interaction_id: str,
    ) -> StoredRemoteInteraction | None:
        async with self.database.session() as session:
            record = await session.get(RemoteInteractionRecord, interaction_id)
            return self._stored_remote_interaction(record) if record is not None else None

    async def put_remote_interaction(
        self,
        *,
        interaction_id: str,
        workspace_id: str,
        local_agent_id: str,
        binding_id: str,
        remote_agent_id: str,
        user_id: str,
        parent_thread_id: str,
        remote_thread_id: str,
        remote_run_id: str,
        remote_context: dict[str, Any],
        stages: list[dict[str, Any]],
        status: str = "pending",
    ) -> StoredRemoteInteraction:
        async with self.database.session() as session, session.begin():
            record = await session.get(RemoteInteractionRecord, interaction_id)
            if record is None:
                record = RemoteInteractionRecord(
                    id=interaction_id,
                    workspace_id=workspace_id,
                    local_agent_id=local_agent_id,
                    binding_id=binding_id,
                    remote_agent_id=remote_agent_id,
                    user_id=user_id,
                    parent_thread_id=parent_thread_id,
                    remote_thread_id=remote_thread_id,
                    remote_run_id=remote_run_id,
                    remote_context=remote_context,
                    stages=stages,
                    status=status,
                )
                session.add(record)
                await session.flush()
            else:
                record.remote_run_id = remote_run_id
                record.remote_context = remote_context
                record.stages = stages
                record.status = status
                record.updated_at = datetime.now(UTC)
                await session.flush()
            return self._stored_remote_interaction(record)

    async def delete_remote_interaction(self, interaction_id: str) -> bool:
        async with self.database.session() as session, session.begin():
            record = await session.get(RemoteInteractionRecord, interaction_id)
            if record is None:
                return False
            await session.delete(record)
            return True

    @staticmethod
    def _stored_remote_interaction(
        record: RemoteInteractionRecord,
    ) -> StoredRemoteInteraction:
        return StoredRemoteInteraction(
            id=record.id,
            remote_agent_id=record.remote_agent_id,
            user_id=record.user_id,
            parent_thread_id=record.parent_thread_id,
            remote_thread_id=record.remote_thread_id,
            remote_run_id=record.remote_run_id,
            remote_context=dict(record.remote_context),
            stages=[dict(item) for item in record.stages],
            status=record.status,
        )

    @staticmethod
    def _run_record_id(
        workspace_id: str,
        agent_id: str,
        user_id: str,
        external_thread_id: str,
        run_id: str,
    ) -> str:
        return identity_digest(workspace_id, agent_id, user_id, external_thread_id, run_id)

    async def start_agent_run(
        self,
        *,
        workspace_id: str,
        agent_id: str,
        user_id: str,
        external_thread_id: str,
        internal_thread_id: str,
        run_id: str,
        config_generation: int,
        parent_run_id: str | None = None,
        owner_instance_id: str | None = None,
        fencing_token: int | None = None,
    ) -> StoredAgentRun | None:
        record_id = self._run_record_id(
            workspace_id,
            agent_id,
            user_id,
            external_thread_id,
            run_id,
        )
        try:
            async with self.database.session() as session, session.begin():
                record = AgentRunRecord(
                    id=record_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    external_thread_id=external_thread_id,
                    internal_thread_id=internal_thread_id,
                    run_id=run_id,
                    config_generation=config_generation,
                    status="running",
                    owner_instance_id=owner_instance_id,
                    fencing_token=fencing_token,
                    cancel_requested=False,
                    interrupt_ids=[],
                    parent_run_id=parent_run_id,
                )
                session.add(record)
                await session.flush()
                stored = self._stored_run(record)
            return stored
        except IntegrityError:
            return None

    async def finish_agent_run(
        self,
        record_id: str,
        *,
        status: str,
        interrupt_ids: list[str] | None = None,
        terminal_reason: str | None = None,
        owner_instance_id: str | None = None,
        fencing_token: int | None = None,
    ) -> bool:
        async with self.database.session() as session, session.begin():
            conditions = [AgentRunRecord.id == record_id]
            if owner_instance_id is not None:
                conditions.append(AgentRunRecord.owner_instance_id == owner_instance_id)
            if fencing_token is not None:
                conditions.append(AgentRunRecord.fencing_token == fencing_token)
            result = await session.execute(
                update(AgentRunRecord)
                .where(*conditions)
                .values(
                    status=status,
                    interrupt_ids=list(interrupt_ids or []),
                    terminal_reason=terminal_reason,
                )
            )
            return bool(affected_rows(result))

    async def get_paused_agent_run(
        self,
        *,
        workspace_id: str,
        agent_id: str,
        user_id: str,
        external_thread_id: str,
    ) -> StoredAgentRun | None:
        statement = (
            select(AgentRunRecord)
            .where(
                AgentRunRecord.workspace_id == workspace_id,
                AgentRunRecord.agent_id == agent_id,
                AgentRunRecord.user_id == user_id,
                AgentRunRecord.external_thread_id == external_thread_id,
                AgentRunRecord.status == "interrupted",
            )
            .order_by(AgentRunRecord.updated_at.desc())
        )
        async with self.database.session() as session:
            record = await session.scalar(statement)
            return self._stored_run(record) if record is not None else None

    async def request_run_cancellation(
        self,
        *,
        workspace_id: str,
        agent_id: str,
        run_id: str,
        user_id: str | None = None,
        external_thread_id: str | None = None,
    ) -> int:
        conditions = [
            AgentRunRecord.workspace_id == workspace_id,
            AgentRunRecord.agent_id == agent_id,
            AgentRunRecord.run_id == run_id,
            AgentRunRecord.status == "running",
        ]
        if external_thread_id is not None:
            conditions.append(AgentRunRecord.external_thread_id == external_thread_id)
        if user_id is not None:
            conditions.append(AgentRunRecord.user_id == user_id)
        statement = select(AgentRunRecord).where(*conditions)
        async with self.database.session() as session, session.begin():
            records = list((await session.scalars(statement)).all())
            if len(records) > 1:
                raise RunConflictError(
                    "run id is ambiguous; provide a user identity and thread_id",
                    details={"run_id": run_id},
                )
            for record in records:
                record.cancel_requested = True
            return len(records)

    async def is_run_cancellation_requested(self, record_id: str) -> bool:
        async with self.database.session() as session:
            record = await session.get(AgentRunRecord, record_id)
            return bool(record and record.cancel_requested)

    async def put_a2a_task(
        self,
        *,
        task_id: str,
        workspace_id: str,
        agent_id: str,
        access_key_id: str,
        user_id: str,
        context_id: str,
        thread_id: str,
        run_id: str,
        config_generation: int,
        state: str,
        task: dict[str, Any],
        owner_instance_id: str | None = None,
    ) -> StoredA2ATask:
        async with self.database.session() as session, session.begin():
            record = await session.get(A2ATaskRecord, task_id)
            if record is None:
                record = A2ATaskRecord(
                    id=task_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    access_key_id=access_key_id,
                    user_id=user_id,
                    context_id=context_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    config_generation=config_generation,
                    state=state,
                    owner_instance_id=owner_instance_id,
                    task=task,
                )
                session.add(record)
                await session.flush()
            else:
                if (
                    record.workspace_id != workspace_id
                    or record.agent_id != agent_id
                    or record.access_key_id != access_key_id
                    or record.context_id != context_id
                ):
                    raise RunConflictError(
                        "A2A task id is already owned by another identity",
                        details={"task_id": task_id},
                    )
                record.run_id = run_id
                record.config_generation = config_generation
                record.state = state
                record.owner_instance_id = owner_instance_id
                record.task = task
                record.updated_at = datetime.now(UTC)
                await session.flush()
            return self._stored_a2a_task(record)

    async def get_a2a_task(
        self,
        task_id: str,
        *,
        agent_id: str,
        access_key_id: str,
    ) -> StoredA2ATask | None:
        statement = select(A2ATaskRecord).where(
            A2ATaskRecord.id == task_id,
            A2ATaskRecord.agent_id == agent_id,
            A2ATaskRecord.access_key_id == access_key_id,
        )
        async with self.database.session() as session:
            record = await session.scalar(statement)
            return self._stored_a2a_task(record) if record is not None else None

    async def delete_a2a_task(
        self,
        task_id: str,
        *,
        agent_id: str,
        access_key_id: str,
    ) -> None:
        statement = delete(A2ATaskRecord).where(
            A2ATaskRecord.id == task_id,
            A2ATaskRecord.agent_id == agent_id,
            A2ATaskRecord.access_key_id == access_key_id,
        )
        async with self.database.session() as session, session.begin():
            await session.execute(statement)

    async def list_a2a_tasks(
        self,
        *,
        agent_id: str,
        access_key_id: str,
        context_id: str | None = None,
        state: str | None = None,
        status_timestamp_after: datetime | None = None,
        cursor: tuple[datetime, str] | None = None,
        page_size: int = 50,
    ) -> tuple[list[StoredA2ATask], bool, int]:
        conditions = [
            A2ATaskRecord.agent_id == agent_id,
            A2ATaskRecord.access_key_id == access_key_id,
        ]
        if context_id is not None:
            conditions.append(A2ATaskRecord.context_id == context_id)
        if state is not None:
            conditions.append(A2ATaskRecord.state == state)
        if status_timestamp_after is not None:
            conditions.append(A2ATaskRecord.updated_at >= status_timestamp_after)

        count_statement = select(func.count()).select_from(A2ATaskRecord).where(*conditions)
        page_conditions = list(conditions)
        if cursor is not None:
            cursor_time, cursor_id = cursor
            page_conditions.append(
                or_(
                    A2ATaskRecord.updated_at < cursor_time,
                    (A2ATaskRecord.updated_at == cursor_time) & (A2ATaskRecord.id < cursor_id),
                )
            )
        statement = (
            select(A2ATaskRecord)
            .where(*page_conditions)
            .order_by(A2ATaskRecord.updated_at.desc(), A2ATaskRecord.id.desc())
            .limit(page_size + 1)
        )
        async with self.database.session() as session:
            total = int((await session.scalar(count_statement)) or 0)
            records = list((await session.scalars(statement)).all())
        has_more = len(records) > page_size
        return (
            [self._stored_a2a_task(item) for item in records[:page_size]],
            has_more,
            total,
        )

    @staticmethod
    def _stored_a2a_task(record: A2ATaskRecord) -> StoredA2ATask:
        return StoredA2ATask(
            id=record.id,
            workspace_id=record.workspace_id,
            agent_id=record.agent_id,
            access_key_id=record.access_key_id,
            user_id=record.user_id,
            context_id=record.context_id,
            thread_id=record.thread_id,
            run_id=record.run_id,
            config_generation=record.config_generation,
            state=record.state,
            task=dict(record.task),
            created_at=record.created_at,
            updated_at=record.updated_at,
            owner_instance_id=record.owner_instance_id,
        )

    @staticmethod
    def _stored_run(record: AgentRunRecord) -> StoredAgentRun:
        return StoredAgentRun(
            id=record.id,
            run_id=record.run_id,
            agent_id=record.agent_id,
            user_id=record.user_id,
            external_thread_id=record.external_thread_id,
            internal_thread_id=record.internal_thread_id,
            config_generation=record.config_generation,
            status=record.status,
            cancel_requested=record.cancel_requested,
            interrupt_ids=tuple(record.interrupt_ids or []),
            owner_instance_id=record.owner_instance_id,
            fencing_token=record.fencing_token,
            terminal_reason=record.terminal_reason,
        )

    @staticmethod
    def tool_execution_id(
        *,
        workspace_id: str,
        agent_id: str,
        internal_thread_id: str,
        tool_call_id: str,
        config_generation: int,
    ) -> str:
        identity = ":".join(
            [
                workspace_id,
                agent_id,
                internal_thread_id,
                tool_call_id,
                str(config_generation),
            ]
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    async def get_tool_execution(self, record_id: str) -> StoredToolExecution | None:
        async with self.database.session() as session:
            record = await session.get(ToolExecutionRecord, record_id)
            if record is None:
                return None
            return StoredToolExecution(
                id=record.id,
                arguments=dict(record.arguments),
                result=dict(record.result),
                status=record.status,
            )

    async def put_tool_execution(
        self,
        *,
        record_id: str,
        workspace_id: str,
        agent_id: str,
        internal_thread_id: str,
        tool_call_id: str,
        tool_name: str,
        config_generation: int,
        arguments: dict[str, Any],
        result: dict[str, Any],
        status: str = "executed",
    ) -> StoredToolExecution:
        async with self.database.session() as session, session.begin():
            record = await session.get(ToolExecutionRecord, record_id)
            if record is None:
                record = ToolExecutionRecord(
                    id=record_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    internal_thread_id=internal_thread_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    config_generation=config_generation,
                    arguments=arguments,
                    result=result,
                    status=status,
                )
                session.add(record)
            else:
                record.arguments = arguments
                record.result = result
                record.status = status
        return StoredToolExecution(
            id=record_id,
            arguments=arguments,
            result=result,
            status=status,
        )

    async def set_tool_execution_status(self, record_id: str, status: str) -> None:
        async with self.database.session() as session, session.begin():
            record = await session.get(ToolExecutionRecord, record_id)
            if record is not None:
                record.status = status
