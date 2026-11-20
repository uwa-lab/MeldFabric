"""Versioned migrations for tables owned by MeldFabric.

Version 1 is the bootstrap schema.  Future versions append explicit upgrade functions;
the runner never performs an implicit downgrade.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import Index, func, insert, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from meldfabric.errors import ConfigurationError
from meldfabric.storage.schema import Base, SchemaMigrationRecord
from meldfabric.storage.sql import create_mapped_table, mapped_table

Upgrade = Callable[[AsyncConnection], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    upgrade: Upgrade

    @property
    def checksum(self) -> str:
        identity = f"{self.version}:{self.name}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()


async def _bootstrap(connection: AsyncConnection) -> None:
    await connection.run_sync(Base.metadata.create_all)


async def _runtime_coordination(connection: AsyncConnection) -> None:
    """Add portable runtime-coordination state to an existing v1 database."""

    from meldfabric.storage.schema import (
        A2ATaskRecord,
        AgentRunRecord,
        RuntimeInstanceRecord,
        ThreadLeaseRecord,
    )

    await create_mapped_table(connection, RuntimeInstanceRecord)
    await create_mapped_table(connection, ThreadLeaseRecord)

    async def add_columns(table_name: str, definitions: dict[str, str]) -> None:
        existing = await connection.run_sync(
            lambda sync_connection: {
                column["name"] for column in inspect(sync_connection).get_columns(table_name)
            }
        )
        for name, ddl in definitions.items():
            if name not in existing:
                await connection.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}"
                )

    await add_columns(
        AgentRunRecord.__tablename__,
        {
            "owner_instance_id": "VARCHAR(128) NULL",
            "fencing_token": "INTEGER NULL",
            "terminal_reason": "VARCHAR(128) NULL",
        },
    )
    await add_columns(
        A2ATaskRecord.__tablename__,
        {"owner_instance_id": "VARCHAR(128) NULL"},
    )
    for table, names in (
        (mapped_table(AgentRunRecord), {"ix_meldfabric_agent_runs_owner_instance_id"}),
        (mapped_table(A2ATaskRecord), {"ix_meldfabric_a2a_tasks_owner_instance_id"}),
    ):
        for index in table.indexes:
            if index.name in names:
                await _create_index(connection, index)


async def _create_index(connection: AsyncConnection, index: Index) -> None:
    def create(sync_connection: Connection) -> None:
        index.create(sync_connection, checkfirst=True)

    await connection.run_sync(create)


async def _runtime_catalog_digest(connection: AsyncConnection) -> None:
    """Record the process code catalog used to reconcile shared configuration."""

    from meldfabric.storage.schema import RuntimeInstanceRecord

    existing = await connection.run_sync(
        lambda sync_connection: {
            column["name"]
            for column in inspect(sync_connection).get_columns(RuntimeInstanceRecord.__tablename__)
        }
    )
    if "catalog_digest" not in existing:
        await connection.exec_driver_sql(
            f"ALTER TABLE {RuntimeInstanceRecord.__tablename__} "
            "ADD COLUMN catalog_digest VARCHAR(64) NULL"
        )
    for index in mapped_table(RuntimeInstanceRecord).indexes:
        if index.name == "ix_meldfabric_runtime_instances_catalog_digest":
            await _create_index(connection, index)


async def _playground_history(connection: AsyncConnection) -> None:
    """Add management-only conversations without touching Agent session tables."""

    from meldfabric.storage.schema import (
        PlaygroundConversationRecord,
        PlaygroundMessageRecord,
    )

    await create_mapped_table(connection, PlaygroundConversationRecord)
    await create_mapped_table(connection, PlaygroundMessageRecord)


MIGRATIONS = (
    Migration(version=1, name="bootstrap", upgrade=_bootstrap),
    Migration(version=2, name="runtime-coordination", upgrade=_runtime_coordination),
    Migration(version=3, name="runtime-catalog-digest", upgrade=_runtime_catalog_digest),
    Migration(version=4, name="playground-history", upgrade=_playground_history),
)
SYSTEM_SCHEMA_VERSION = MIGRATIONS[-1].version


class SystemSchemaMigrator:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def migrate(self) -> int:
        async with self._locked_connection() as connection:
            await create_mapped_table(connection, SchemaMigrationRecord)
            current = await self._current_version(connection)
            if current > SYSTEM_SCHEMA_VERSION:
                raise ConfigurationError(
                    "system database schema is newer than this MeldFabric version: "
                    f"database={current}, supported={SYSTEM_SCHEMA_VERSION}"
                )
            for migration in MIGRATIONS:
                if migration.version <= current:
                    continue
                await migration.upgrade(connection)
                await connection.execute(
                    insert(SchemaMigrationRecord).values(
                        version=migration.version,
                        name=migration.name,
                        checksum=migration.checksum,
                    )
                )
                current = migration.version
            await self._verify_checksums(connection)
            return current

    async def current_version(self) -> int:
        async with self.engine.connect() as connection:
            return await self._current_version(connection)

    @staticmethod
    async def _current_version(connection: AsyncConnection) -> int:
        version = await connection.scalar(select(func.max(SchemaMigrationRecord.version)))
        return int(version or 0)

    async def _verify_checksums(self, connection: AsyncConnection) -> None:
        expected = {item.version: item for item in MIGRATIONS}
        rows = (
            await connection.execute(
                select(
                    SchemaMigrationRecord.version,
                    SchemaMigrationRecord.name,
                    SchemaMigrationRecord.checksum,
                )
            )
        ).all()
        for version, name, checksum in rows:
            migration = expected.get(version)
            if migration is None:
                continue
            if name != migration.name or checksum != migration.checksum:
                raise ConfigurationError(
                    f"system database migration {version} does not match packaged history"
                )

    @asynccontextmanager
    async def _locked_connection(self):
        """Hold a database-native migration lock for the complete migration transaction."""

        dialect = self.engine.dialect.name
        connection = await self.engine.connect()
        mysql_lock_name = "meldfabric_system_schema"
        try:
            if dialect == "sqlite":
                # BEGIN IMMEDIATE serializes writers before either worker can inspect the
                # migration table, eliminating the check-then-insert race.
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
            elif dialect == "postgresql":
                await connection.begin()
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": 0x464D4147454E54},
                )
            elif dialect in {"mysql", "mariadb"}:
                acquired = await connection.scalar(
                    text("SELECT GET_LOCK(:lock_name, 30)"),
                    {"lock_name": mysql_lock_name},
                )
                if acquired != 1:
                    raise ConfigurationError("timed out acquiring the System DB migration lock")
                await connection.commit()
                await connection.begin()
            else:
                await connection.begin()
            try:
                yield connection
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()
        finally:
            if dialect in {"mysql", "mariadb"}:
                try:
                    await connection.execute(
                        text("SELECT RELEASE_LOCK(:lock_name)"),
                        {"lock_name": mysql_lock_name},
                    )
                    await connection.commit()
                except Exception:
                    await connection.rollback()
            await connection.close()
