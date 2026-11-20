"""Typed SQLAlchemy helpers shared by storage repositories and migrations."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import Table
from sqlalchemy.engine import Connection, CursorResult, Result
from sqlalchemy.ext.asyncio import AsyncConnection


def mapped_table(model: type[Any]) -> Table:
    """Return the concrete table owned by a declarative model.

    SQLAlchemy exposes ``__table__`` as the wider ``FromClause`` type on its
    declarative base even though mapped classes created here always own a ``Table``.
    Keep that boundary assertion in one place instead of weakening migration types.
    """

    return cast(Table, model.__table__)


async def create_mapped_table(connection: AsyncConnection, model: type[Any]) -> None:
    """Create one declarative table through an async connection if it is absent."""

    table = mapped_table(model)

    def create(sync_connection: Connection) -> None:
        table.create(sync_connection, checkfirst=True)

    await connection.run_sync(create)


def affected_rows(result: Result[Any]) -> int:
    """Return a portable non-negative row count for one UPDATE or DELETE result."""

    rowcount = cast(CursorResult[Any], result).rowcount
    return rowcount if rowcount >= 0 else 0
