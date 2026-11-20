"""Relational control-plane schema.

Configuration sections are JSON documents because their validation and cross-resource
references belong to the Pydantic domain model.  Authentication, generations, and
snapshots remain relational so their concurrency and retention rules are enforceable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ControlBase(DeclarativeBase):
    pass


class ControlSchemaMigration(ControlBase):
    __tablename__ = "meldfabric_control_schema_migrations"

    module: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    checksum: Mapped[str] = mapped_column(String(64))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ControlWorkspace(ControlBase):
    __tablename__ = "meldfabric_control_workspaces"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    active_generation: Mapped[int] = mapped_column(BigInteger)
    last_snapshot_version: Mapped[int] = mapped_column(Integer, default=0)
    config_schema_version: Mapped[str] = mapped_column(String(32))
    maintenance_state: Mapped[str] = mapped_column(String(32), default="active")
    encryption_key_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ControlConfigGeneration(ControlBase):
    __tablename__ = "meldfabric_control_config_generations"

    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    parent_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(String(64), default="save")
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ControlConfigSnapshot(ControlBase):
    __tablename__ = "meldfabric_control_config_snapshots"
    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="uq_control_snapshot_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer)
    source_generation: Mapped[int] = mapped_column(BigInteger)
    label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ControlAdminCredential(ControlBase):
    __tablename__ = "meldfabric_control_admin_credentials"

    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    key_hash: Mapped[str] = mapped_column(Text)
    key_prefix: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="active")
    rotation_counter: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ControlSecretVersion(ControlBase):
    """Immutable encrypted value for a configuration-bound outbound credential."""

    __tablename__ = "meldfabric_control_secret_versions"

    secret_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ControlGenerationSecretRef(ControlBase):
    """Secret versions pinned by one immutable configuration generation."""

    __tablename__ = "meldfabric_control_generation_secret_refs"

    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    secret_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    secret_version: Mapped[int] = mapped_column(Integer)


class ControlBackupManifest(ControlBase):
    __tablename__ = "meldfabric_control_backup_manifest"

    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    backup_id: Mapped[str] = mapped_column(String(64), unique=True)
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    active_generation: Mapped[int] = mapped_column(BigInteger)
    last_snapshot_version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="complete")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
