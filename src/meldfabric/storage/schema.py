"""SQLAlchemy tables owned by MeldFabric.

These tables are intentionally separate from Strands session and memory schemas.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SchemaMigrationRecord(Base):
    __tablename__ = "meldfabric_schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    checksum: Mapped[str] = mapped_column(String(64))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SecretRecord(Base):
    __tablename__ = "meldfabric_secret_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    encrypted_value: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    key_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_prefix: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RemoteSessionMappingRecord(Base):
    __tablename__ = "meldfabric_remote_session_mappings"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    mapping_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    local_agent_id: Mapped[str] = mapped_column(String(128), index=True)
    remote_agent_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    parent_thread_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    remote_thread_id: Mapped[str] = mapped_column(String(256))
    policy: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RemoteInteractionRecord(Base):
    """Replay-safe state for a remote sub-Agent interaction spanning interrupts."""

    __tablename__ = "meldfabric_remote_interactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), index=True)
    local_agent_id: Mapped[str] = mapped_column(String(128), index=True)
    binding_id: Mapped[str] = mapped_column(String(128), index=True)
    remote_agent_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(256), index=True)
    parent_thread_id: Mapped[str] = mapped_column(String(256), index=True)
    remote_thread_id: Mapped[str] = mapped_column(String(256), index=True)
    remote_run_id: Mapped[str] = mapped_column(String(256), index=True)
    remote_context: Mapped[dict[str, Any]] = mapped_column(JSON)
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DeletedAgentResidualRecord(Base):
    __tablename__ = "meldfabric_deleted_agent_residuals"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    database_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    namespaces: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    available_cleanup_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    cleaned_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentSessionRecord(Base):
    __tablename__ = "meldfabric_agent_sessions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(256), index=True)
    external_thread_id: Mapped[str] = mapped_column(String(256), index=True)
    internal_thread_id: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    session_database_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    memory_database_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PlaygroundConversationRecord(Base):
    """A management-console transcript, independent of Agent session storage."""

    __tablename__ = "meldfabric_playground_conversations"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "thread_id",
            name="uq_meldfabric_playground_conversation_thread",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), index=True)
    endpoint_id: Mapped[str] = mapped_column(String(128), index=True)
    endpoint_name: Mapped[str] = mapped_column(String(256))
    user_id: Mapped[str] = mapped_column(String(256), index=True)
    thread_id: Mapped[str] = mapped_column(String(256), index=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_generation: Mapped[int] = mapped_column(Integer)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_preview: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class PlaygroundMessageRecord(Base):
    """One display entry in a Playground transcript.

    ``sequence`` is a database-assigned ordering key. ``id`` remains the stable
    protocol message identity used when the transcript is sent through AG-UI.
    """

    __tablename__ = "meldfabric_playground_messages"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "id",
            name="uq_meldfabric_playground_message_identity",
        ),
    )

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(128), index=True)
    workspace_id: Mapped[str] = mapped_column(String(128), index=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("meldfabric_playground_conversations.id", ondelete="CASCADE"),
        index=True,
    )
    run_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32), default="message", index=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRunRecord(Base):
    __tablename__ = "meldfabric_agent_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(256), index=True)
    external_thread_id: Mapped[str] = mapped_column(String(256), index=True)
    internal_thread_id: Mapped[str] = mapped_column(String(256), index=True)
    run_id: Mapped[str] = mapped_column(String(256), index=True)
    config_generation: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    owner_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    fencing_token: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terminal_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    interrupt_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    parent_run_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class A2ATaskRecord(Base):
    """Durable A2A task state scoped to the inbound credential that created it."""

    __tablename__ = "meldfabric_a2a_tasks"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    access_key_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(256), index=True)
    context_id: Mapped[str] = mapped_column(String(256), index=True)
    thread_id: Mapped[str] = mapped_column(String(256), index=True)
    run_id: Mapped[str] = mapped_column(String(256), index=True)
    config_generation: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(64), index=True)
    owner_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    task: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True
    )


class ToolExecutionRecord(Base):
    __tablename__ = "meldfabric_tool_executions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    internal_thread_id: Mapped[str] = mapped_column(String(256), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(256), index=True)
    tool_name: Mapped[str] = mapped_column(String(256))
    config_generation: Mapped[int] = mapped_column(Integer)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="executed", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RuntimeInstanceRecord(Base):
    """One concrete AgentKit process participating in runtime coordination."""

    __tablename__ = "meldfabric_runtime_instances"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), index=True)
    hostname: Mapped[str] = mapped_column(String(256))
    process_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="ready", index=True)
    config_generation: Mapped[int] = mapped_column(Integer)
    catalog_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ThreadLeaseRecord(Base):
    """Database-backed exclusive execution lease for one logical Agent thread."""

    __tablename__ = "meldfabric_thread_leases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(256), index=True)
    external_thread_id: Mapped[str] = mapped_column(String(256), index=True)
    owner_instance_id: Mapped[str] = mapped_column(String(128), index=True)
    run_id: Mapped[str] = mapped_column(String(256), index=True)
    fencing_token: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
