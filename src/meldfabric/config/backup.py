"""One-shot full control-database backup to an ad-hoc SQL target."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, create_engine, delete, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from meldfabric.config.database import ControlDatabaseSpec
from meldfabric.config.schema import ControlBackupManifest
from meldfabric.errors import ConfigurationError, ControlDatabaseUnavailableError

_REQUIRED_TABLES = {
    "meldfabric_control_workspaces",
    "meldfabric_control_admin_credentials",
    "meldfabric_control_config_generations",
    "meldfabric_control_schema_migrations",
}
_EPHEMERAL_TABLES = {
    "meldfabric_runtime_instances",
    "meldfabric_thread_leases",
}


@dataclass(frozen=True, slots=True)
class ControlBackupResult:
    backup_id: str
    target: str
    workspace_id: str
    active_generation: int
    latest_generation: int
    last_snapshot_version: int
    table_counts: dict[str, int]
    content_hash: str


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value.isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _row_hash(rows: dict[str, list[dict[str, Any]]]) -> str:
    digest = hashlib.sha256()
    for table_name in sorted(rows):
        digest.update(table_name.encode("utf-8"))
        for row in rows[table_name]:
            payload = {key: _json_value(value) for key, value in sorted(row.items())}
            digest.update(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
    return digest.hexdigest()


def _engine(spec: ControlDatabaseSpec) -> Engine:
    options = dict(spec.options)
    options.setdefault("pool_pre_ping", True)
    if spec.engine == "sqlite":
        connect_args = dict(options.pop("connect_args", {}))
        connect_args.setdefault("timeout", 30)
        options["connect_args"] = connect_args
    return create_engine(spec.sync_url, **options)


class ControlBackupService:
    def __init__(self, source: Engine, source_spec: ControlDatabaseSpec) -> None:
        self.source = source
        self.source_spec = source_spec

    def backup(
        self,
        target_url: str,
        *,
        target_options: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> ControlBackupResult:
        target_spec = ControlDatabaseSpec.resolve(
            target_url,
            state_dir=Path.cwd(),
            options=target_options,
        )
        if target_spec.fingerprint == self.source_spec.fingerprint:
            raise ConfigurationError("backup target must differ from the active control database")
        target = _engine(target_spec)
        try:
            return self._copy(target, target_spec, overwrite=overwrite)
        except ConfigurationError:
            raise
        except (SQLAlchemyError, OSError, ImportError) as exc:
            raise ControlDatabaseUnavailableError(
                "control database backup failed",
                details={
                    "target": target_spec.display,
                    "exception_type": type(exc).__name__,
                },
            ) from exc
        finally:
            target.dispose()

    def _copy(
        self,
        target: Engine,
        target_spec: ControlDatabaseSpec,
        *,
        overwrite: bool,
    ) -> ControlBackupResult:
        source_names = {
            name
            for name in inspect(self.source).get_table_names()
            if name.startswith("meldfabric_")
        }
        missing = _REQUIRED_TABLES - source_names
        if missing:
            raise ConfigurationError(
                "active control database is incomplete",
                details={"missing_tables": sorted(missing)},
            )
        source_meta = MetaData()
        source_meta.reflect(bind=self.source, only=sorted(source_names))
        rows: dict[str, list[dict[str, Any]]] = {}
        with self.source.connect() as source_connection:
            if self.source.dialect.name in {"postgresql", "mysql"}:
                source_connection = source_connection.execution_options(
                    isolation_level="REPEATABLE READ"
                )
            transaction = source_connection.begin()
            try:
                for table in source_meta.sorted_tables:
                    rows[table.name] = (
                        []
                        if table.name in _EPHEMERAL_TABLES
                        else [
                            dict(row._mapping)
                            for row in source_connection.execute(select(table)).all()
                        ]
                    )
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise

        workspace_rows = rows["meldfabric_control_workspaces"]
        if len(workspace_rows) != 1:
            raise ConfigurationError("source control database must contain one workspace")
        workspace = workspace_rows[0]
        workspace_id = str(workspace["id"])
        latest_generation = max(
            int(row["generation"])
            for row in rows["meldfabric_control_config_generations"]
            if str(row["workspace_id"]) == workspace_id
        )
        target_names = set(inspect(target).get_table_names())
        target_meldfabric_names = {name for name in target_names if name.startswith("meldfabric_")}
        if target_meldfabric_names:
            if not _REQUIRED_TABLES.issubset(target_meldfabric_names):
                raise ConfigurationError(
                    "backup target contains incomplete MeldFabric control data",
                    details={"target": target_spec.display},
                )
            target_meta = MetaData()
            target_meta.reflect(bind=target, only=["meldfabric_control_workspaces"])
            target_workspace = target_meta.tables["meldfabric_control_workspaces"]
            with target.connect() as connection:
                identities = list(connection.scalars(select(target_workspace.c.id)).all())
            if identities != [workspace_id]:
                raise ConfigurationError(
                    "backup target belongs to another MeldFabric workspace",
                    details={"target": target_spec.display},
                )
            if not overwrite:
                raise ConfigurationError(
                    "backup target already contains this workspace; explicit overwrite is required"
                )

        source_meta.create_all(target, checkfirst=True)
        with target.begin() as target_connection:
            if target_meldfabric_names:
                for table in reversed(source_meta.sorted_tables):
                    target_connection.execute(delete(table))
            for table in source_meta.sorted_tables:
                values = rows[table.name]
                if values:
                    target_connection.execute(table.insert(), values)

        copied: dict[str, list[dict[str, Any]]] = {}
        with target.connect() as target_connection:
            for table in source_meta.sorted_tables:
                copied[table.name] = [
                    dict(row._mapping) for row in target_connection.execute(select(table)).all()
                ]
        expected_hash = _row_hash(rows)
        actual_hash = _row_hash(copied)
        counts = {name: len(values) for name, values in rows.items()}
        if counts != {name: len(values) for name, values in copied.items()} or (
            actual_hash != expected_hash
        ):
            raise ConfigurationError("backup verification failed")

        backup_id = f"backup_{uuid.uuid4().hex}"
        manifest = source_meta.tables.get(ControlBackupManifest.__tablename__)
        if manifest is None:
            raise ConfigurationError("backup manifest table is missing")
        with target.begin() as target_connection:
            target_connection.execute(
                delete(manifest).where(manifest.c.workspace_id == workspace_id)
            )
            target_connection.execute(
                manifest.insert(),
                {
                    "workspace_id": workspace_id,
                    "backup_id": backup_id,
                    "source_fingerprint": self.source_spec.fingerprint,
                    "active_generation": int(workspace["active_generation"]),
                    "last_snapshot_version": int(workspace["last_snapshot_version"]),
                    "content_hash": expected_hash,
                    "status": "complete",
                },
            )
        return ControlBackupResult(
            backup_id=backup_id,
            target=target_spec.display,
            workspace_id=workspace_id,
            active_generation=int(workspace["active_generation"]),
            latest_generation=latest_generation,
            last_snapshot_version=int(workspace["last_snapshot_version"]),
            table_counts=counts,
            content_hash=expected_hash,
        )
