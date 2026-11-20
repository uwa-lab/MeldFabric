"""Async SQLAlchemy database resource adapter."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from meldfabric.config.database import ControlDatabaseSpec
from meldfabric.config.models import DatabaseConnection
from meldfabric.domain import DatabaseEngine
from meldfabric.errors import ConfigurationError


async def _configure_sqlite(connection) -> None:
    await connection.execute("PRAGMA busy_timeout=30000")
    deadline = time.monotonic() + 30
    while True:
        try:
            async with connection.execute("PRAGMA journal_mode") as cursor:
                current = (await cursor.fetchone())[0].lower()
            if current not in {"wal", "memory"}:
                async with connection.execute("PRAGMA journal_mode=WAL") as cursor:
                    await cursor.fetchone()
            return
        except sqlite3.OperationalError as exc:
            # Concurrent first connections can both observe DELETE mode. SQLite
            # may reject that upgrade immediately despite busy_timeout. Yield and
            # re-read the persisted mode rather than failing worker initialization.
            code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
            if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.05)


def resolve_database_url(connection: DatabaseConnection, *, config_dir: Path) -> str:
    """Resolve a relative SQLite URL against the configuration directory."""

    url = connection.url
    if connection.engine is not DatabaseEngine.SQLITE:
        return url
    prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
    prefix = next((item for item in prefixes if url.startswith(item)), None)
    if prefix is None:
        raise ConfigurationError(
            f"SQLite database resource {connection.id!r} must use sqlite+aiosqlite:/// URL"
        )
    raw_path = url[len(prefix) :]
    if raw_path == ":memory:":
        return "sqlite+aiosqlite:///:memory:"
    path = Path(raw_path)
    if not path.is_absolute():
        path = (config_dir / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{path}"


class AsyncDatabase:
    """Lifecycle wrapper for a configured SQL database resource."""

    def __init__(
        self,
        connection: DatabaseConnection | ControlDatabaseSpec,
        *,
        config_dir: Path | None = None,
        bridge_safe: bool = False,
    ) -> None:
        self.connection = connection
        if isinstance(connection, ControlDatabaseSpec):
            self.url = connection.async_url
            engine_options = dict(connection.options)
            is_sqlite = connection.engine == "sqlite"
        else:
            if config_dir is None:
                raise ValueError("config_dir is required for database resources")
            self.url = resolve_database_url(connection, config_dir=config_dir)
            engine_options = dict(connection.options)
            is_sqlite = connection.engine is DatabaseEngine.SQLITE
        if bridge_safe:
            # Strands' synchronous session callbacks bridge async Storage calls
            # through short-lived event loops in worker threads. A pooled async
            # connection is bound to the loop that created it, so it must never be
            # reused by a later bridge loop. NullPool keeps the engine reusable
            # while giving every operation a fresh, loop-local connection.
            for option in (
                "pool_size",
                "max_overflow",
                "pool_timeout",
                "pool_recycle",
                "pool_use_lifo",
            ):
                engine_options.pop(option, None)
            engine_options["poolclass"] = NullPool
        self.engine: AsyncEngine = create_async_engine(
            self.url,
            pool_pre_ping=True,
            **engine_options,
        )
        if is_sqlite:
            # Runtime polling reads, run bookkeeping and checkpoints can share one
            # SQLite resource across workers. WAL allows readers during a write.
            @event.listens_for(self.engine.sync_engine, "connect")
            def configure_sqlite(dbapi_connection, _record):
                dbapi_connection.run_async(_configure_sqlite)

        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        return self.sessions()

    async def close(self) -> None:
        await self.engine.dispose()
