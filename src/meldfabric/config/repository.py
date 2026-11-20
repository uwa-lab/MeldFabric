"""Transactional configuration repository backed by the control database."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from meldfabric.config.database import ControlDatabaseSpec
from meldfabric.config.defaults import create_default_config
from meldfabric.config.models import WorkspaceConfig
from meldfabric.config.schema import (
    ControlAdminCredential,
    ControlBase,
    ControlConfigGeneration,
    ControlConfigSnapshot,
    ControlGenerationSecretRef,
    ControlSchemaMigration,
    ControlSecretVersion,
    ControlWorkspace,
)
from meldfabric.config.types import (
    ConfigSnapshot,
    EncryptedSecretWrite,
    GenerationState,
    LoadedWorkspace,
)
from meldfabric.errors import (
    ConfigurationConflictError,
    ConfigurationError,
    ControlDatabaseCorruptError,
    ControlDatabaseUnavailableError,
)
from meldfabric.security import hash_inbound_key, verify_inbound_key
from meldfabric.storage.schema import SecretRecord

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_KEY_ENV = "MELDFABRIC_ADMIN_KEY"
CONTROL_SCHEMA_VERSION = 1
_CORE_MIGRATION_CHECKSUM = hashlib.sha256(b"meldfabric-control-core-v1").hexdigest()


def generate_admin_key() -> str:
    return f"meldfabric_admin_{secrets.token_urlsafe(32)}"


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _fingerprint(document: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(document)).hexdigest()


def _prefix(value: str) -> str:
    return value[:24]


class ConfigRepository:
    """Own the authoritative control-plane configuration for one workspace."""

    def __init__(
        self,
        database: str | None = None,
        *,
        state_dir: str | Path = ".meldfabric",
        database_options: dict[str, Any] | None = None,
        admin_key: str | None = None,
        admin_key_env: str = DEFAULT_ADMIN_KEY_ENV,
    ) -> None:
        self.database = ControlDatabaseSpec.resolve(
            database, state_dir=state_dir, options=database_options
        )
        self.state_dir = self.database.state_dir
        self.admin_key_env = admin_key_env
        self._initial_admin_key = admin_key
        self._process_lock = threading.RLock()
        options = dict(self.database.options)
        options.setdefault("pool_pre_ping", True)
        if self.database.engine == "sqlite":
            connect_args = dict(options.pop("connect_args", {}))
            connect_args.setdefault("timeout", 30)
            options["connect_args"] = connect_args
        try:
            self.engine = create_engine(self.database.sync_url, **options)
            self._configure_engine(self.engine)
            self._bootstrap_schema()
        except (SQLAlchemyError, OSError, ImportError) as exc:
            raise ControlDatabaseUnavailableError(
                "cannot initialize the MeldFabric control database",
                details={
                    "database": self.database.display,
                    "exception_type": type(exc).__name__,
                },
            ) from exc

    @property
    def path(self) -> Path:
        """Filesystem base for runtime artifacts; this is never a YAML path."""

        return self.state_dir

    def _configure_engine(self, engine: Engine) -> None:
        if self.database.engine != "sqlite":
            return

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                deadline = time.monotonic() + 30
                while True:
                    try:
                        current = cursor.execute("PRAGMA journal_mode").fetchone()[0].lower()
                        if current != "wal":
                            cursor.execute("PRAGMA journal_mode=WAL").fetchone()
                        break
                    except sqlite3.OperationalError as exc:
                        # Concurrent first connections can both try to persist WAL mode
                        # before the control-schema transaction exists. SQLite may reject
                        # that PRAGMA immediately despite busy_timeout, so re-read until
                        # the winning process has committed the mode switch.
                        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                        if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                            raise
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
            finally:
                cursor.close()

    def _bootstrap_schema(self) -> None:
        with self._write_connection() as connection, Session(bind=connection) as session:
            # Schema creation must participate in the same database-native lock as all
            # later migrations. Several Pods can construct AgentKit against one empty
            # control database at exactly the same time.
            ControlBase.metadata.create_all(connection)
            row = session.get(ControlSchemaMigration, ("core", CONTROL_SCHEMA_VERSION))
            if row is None:
                session.add(
                    ControlSchemaMigration(
                        module="core",
                        version=CONTROL_SCHEMA_VERSION,
                        name="initial-control-schema",
                        checksum=_CORE_MIGRATION_CHECKSUM,
                    )
                )
                session.flush()
            elif row.checksum != _CORE_MIGRATION_CHECKSUM:
                raise ControlDatabaseCorruptError(
                    "control database schema checksum does not match this package"
                )

    @contextmanager
    def _write_connection(self) -> Iterator[Connection]:
        """Acquire a database-native serialized write transaction."""

        with self._process_lock, self.engine.connect() as connection:
            mysql_lock = False
            transaction = None
            lock_name = f"meldfabric:{self.database.fingerprint[:48]}"
            try:
                if self.database.engine == "sqlite":
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    transaction = connection.begin()
                    if self.database.engine == "postgresql":
                        connection.execute(
                            text("SELECT pg_advisory_xact_lock(:lock_id)"),
                            {"lock_id": int(self.database.fingerprint[:15], 16)},
                        )
                    elif self.database.engine == "mysql":
                        acquired = connection.execute(
                            text("SELECT GET_LOCK(:name, 30)"), {"name": lock_name}
                        ).scalar_one()
                        if acquired != 1:
                            raise ControlDatabaseUnavailableError(
                                "timed out waiting for the control database write lock"
                            )
                        mysql_lock = True
                yield connection
                if transaction is not None:
                    transaction.commit()
                else:
                    connection.commit()
            except Exception:
                if transaction is not None and transaction.is_active:
                    transaction.rollback()
                elif connection.in_transaction():
                    connection.rollback()
                raise
            finally:
                if mysql_lock:
                    connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})

    def initialize(self) -> LoadedWorkspace:
        generated_admin_key: str | None = None
        with self._write_connection() as connection, Session(bind=connection) as session:
            workspaces = list(session.scalars(select(ControlWorkspace)).all())
            if len(workspaces) > 1:
                raise ControlDatabaseCorruptError(
                    "the control database contains more than one workspace"
                )
            if not workspaces:
                generated = self._initial_admin_key is None and not os.getenv(self.admin_key_env)
                loaded = self._initialize_workspace(session)
                generated_admin_key = loaded.revealed_admin_key if generated else None
            else:
                workspace = workspaces[0]
                credential = session.get(ControlAdminCredential, workspace.id)
                if credential is None:
                    raise ControlDatabaseCorruptError(
                        "workspace exists but its administrator credential is missing",
                        details={"workspace_id": workspace.id},
                    )
                loaded = LoadedWorkspace(
                    config=self._load_latest(session, workspace),
                    state_dir=self.state_dir,
                    database=self.database,
                    admin_key_prefix=credential.key_prefix,
                )
        # Never reveal a credential before the transaction that persisted its hash
        # has committed. A failed commit must not leave an unusable key in the log.
        if generated_admin_key is not None:
            logger.warning(
                "Generated MeldFabric administrator key; it will only be logged in full once: %s",
                generated_admin_key,
            )
        return loaded

    def _initialize_workspace(self, session: Session) -> LoadedWorkspace:
        config = create_default_config()
        supplied = self._initial_admin_key or os.getenv(self.admin_key_env)
        admin_key = supplied or generate_admin_key()
        document = config.model_dump(mode="json")
        fingerprint = _fingerprint(document)
        session.add(
            ControlWorkspace(
                id=config.workspace.id,
                active_generation=config.config_generation,
                config_schema_version=config.schema_version,
            )
        )
        session.add(
            ControlAdminCredential(
                workspace_id=config.workspace.id,
                key_hash=hash_inbound_key(admin_key),
                key_prefix=_prefix(admin_key),
            )
        )
        self._write_generation(
            session,
            config,
            parent_generation=None,
            reason="initialize",
            fingerprint=fingerprint,
        )
        session.flush()
        return LoadedWorkspace(
            config=config,
            state_dir=self.state_dir,
            database=self.database,
            revealed_admin_key=admin_key,
            admin_key_prefix=_prefix(admin_key),
        )

    def load(self) -> LoadedWorkspace:
        """Load the latest saved configuration used by the management plane."""

        with Session(self.engine) as session:
            workspaces = list(session.scalars(select(ControlWorkspace)).all())
            if len(workspaces) != 1:
                if not workspaces:
                    return self.initialize()
                raise ControlDatabaseCorruptError(
                    "the control database contains more than one workspace"
                )
            workspace = workspaces[0]
            credential = session.get(ControlAdminCredential, workspace.id)
            if credential is None:
                raise ControlDatabaseCorruptError(
                    "workspace exists but its administrator credential is missing",
                    details={"workspace_id": workspace.id},
                )
            return LoadedWorkspace(
                config=self._load_latest(session, workspace),
                state_dir=self.state_dir,
                database=self.database,
                admin_key_prefix=credential.key_prefix,
            )

    def load_active(self) -> LoadedWorkspace:
        """Load the explicitly published configuration used by Agent runtimes."""

        with Session(self.engine) as session:
            workspace = session.scalar(select(ControlWorkspace))
            if workspace is None:
                return self.initialize()
            credential = session.get(ControlAdminCredential, workspace.id)
            if credential is None:
                raise ControlDatabaseCorruptError(
                    "workspace exists but its administrator credential is missing",
                    details={"workspace_id": workspace.id},
                )
            return LoadedWorkspace(
                config=self._load_active(session, workspace),
                state_dir=self.state_dir,
                database=self.database,
                admin_key_prefix=credential.key_prefix,
            )

    def generation_state(self) -> GenerationState:
        """Return the compact saved/published generation state for coordination loops."""

        with Session(self.engine) as session:
            workspace = session.scalar(select(ControlWorkspace))
            if workspace is None:
                raise ControlDatabaseCorruptError("workspace record is missing")
            latest = self._latest_generation_number(session, workspace.id)
            return GenerationState(
                workspace_id=workspace.id,
                latest_generation=latest,
                active_generation=workspace.active_generation,
            )

    def load_config(self) -> WorkspaceConfig:
        return self.load().config

    def load_generation(self, workspace_id: str, generation: int) -> WorkspaceConfig:
        with Session(self.engine) as session:
            record = session.get(ControlConfigGeneration, (workspace_id, generation))
            if record is None:
                raise ConfigurationError(
                    f"configuration generation {generation} is not available",
                    details={"workspace_id": workspace_id, "config_generation": generation},
                )
            return self._validate(record.document, source=f"generation {generation}")

    def save(
        self,
        candidate: WorkspaceConfig,
        *,
        expected_generation: int,
        reason: str = "save",
        created_by: str | None = None,
        secret_source_generation: int | None = None,
        secret_writes: Sequence[EncryptedSecretWrite] = (),
    ) -> LoadedWorkspace:
        with self._write_connection() as connection, Session(bind=connection) as session:
            workspace = session.get(ControlWorkspace, candidate.workspace.id)
            if workspace is None:
                raise ControlDatabaseCorruptError("active workspace record is missing")
            latest_generation = self._latest_generation_number(session, workspace.id)
            if latest_generation != expected_generation:
                raise ConfigurationConflictError(
                    "configuration changed before this update was saved",
                    details={
                        "expected_generation": expected_generation,
                        "current_generation": latest_generation,
                    },
                )
            expected_next = expected_generation + 1
            if candidate.config_generation != expected_next:
                candidate = candidate.model_copy(update={"config_generation": expected_next})
            document = candidate.model_dump(mode="json")
            fingerprint = _fingerprint(document)
            self._write_generation(
                session,
                candidate,
                parent_generation=expected_generation,
                reason=reason,
                fingerprint=fingerprint,
                created_by=created_by,
            )
            self._write_secrets(session, secret_writes)
            self._capture_secret_refs(
                session,
                candidate,
                source_generation=secret_source_generation,
                restore_current=secret_source_generation is not None,
            )
            workspace.config_schema_version = candidate.schema_version
            credential = session.get(ControlAdminCredential, workspace.id)
            if credential is None:
                raise ControlDatabaseCorruptError(
                    "workspace exists but its administrator credential is missing"
                )
            session.flush()
            return LoadedWorkspace(
                config=candidate,
                state_dir=self.state_dir,
                database=self.database,
                admin_key_prefix=credential.key_prefix,
            )

    @staticmethod
    def _write_secrets(
        session: Session,
        writes: Sequence[EncryptedSecretWrite],
    ) -> None:
        """Persist encrypted values inside the generation transaction.

        The control repository already serializes writes across processes.  Reusing that
        transaction makes version allocation and generation creation one atomic operation.
        """

        seen: set[str] = set()
        for write in writes:
            if write.secret_id in seen:
                raise ConfigurationError(
                    "one configuration mutation cannot write the same secret twice",
                    details={"secret_id": write.secret_id},
                )
            seen.add(write.secret_id)
            current_version = session.scalar(
                select(func.max(ControlSecretVersion.version)).where(
                    ControlSecretVersion.secret_id == write.secret_id
                )
            )
            session.add(
                ControlSecretVersion(
                    secret_id=write.secret_id,
                    version=int(current_version or 0) + 1,
                    kind=write.kind,
                    encrypted_value=write.encrypted_value,
                )
            )
            current = session.get(SecretRecord, write.secret_id)
            if current is None:
                session.add(
                    SecretRecord(
                        id=write.secret_id,
                        kind=write.kind,
                        encrypted_value=write.encrypted_value,
                        scopes=[],
                    )
                )
            else:
                current.kind = write.kind
                current.encrypted_value = write.encrypted_value
                current.key_hash = None
                current.status = "active"

    def activate_latest(self, *, expected_generation: int) -> LoadedWorkspace:
        """Publish the latest saved generation as the global runtime target."""

        with self._write_connection() as connection, Session(bind=connection) as session:
            workspace = session.scalar(select(ControlWorkspace))
            if workspace is None:
                raise ControlDatabaseCorruptError("workspace record is missing")
            latest_generation = self._latest_generation_number(session, workspace.id)
            if latest_generation != expected_generation:
                raise ConfigurationConflictError(
                    "configuration changed before it was applied",
                    details={
                        "expected_generation": expected_generation,
                        "current_generation": latest_generation,
                    },
                )
            config = self._load_generation(session, workspace.id, latest_generation)
            workspace.active_generation = latest_generation
            credential = session.get(ControlAdminCredential, workspace.id)
            if credential is None:
                raise ControlDatabaseCorruptError(
                    "workspace exists but its administrator credential is missing"
                )
            session.flush()
            return LoadedWorkspace(
                config=config,
                state_dir=self.state_dir,
                database=self.database,
                admin_key_prefix=credential.key_prefix,
            )

    def verify_admin_key(self, value: str) -> bool:
        if not value:
            return False
        with Session(self.engine) as session:
            credentials = list(session.scalars(select(ControlAdminCredential)).all())
            return len(credentials) == 1 and verify_inbound_key(value, credentials[0].key_hash)

    def set_admin_key(self, *, current_key: str, new_key: str) -> None:
        with self._write_connection() as connection, Session(bind=connection) as session:
            credential = session.scalar(select(ControlAdminCredential))
            if credential is None:
                raise ControlDatabaseCorruptError("administrator credential is missing")
            if not verify_inbound_key(current_key, credential.key_hash):
                raise ConfigurationError("administrator key verification failed")
            credential.key_hash = hash_inbound_key(new_key)
            credential.key_prefix = _prefix(new_key)
            credential.rotation_counter += 1
            session.flush()

    def create_snapshot(
        self,
        *,
        label: str | None = None,
        note: str | None = None,
        created_by: str | None = None,
        expected_generation: int | None = None,
    ) -> ConfigSnapshot:
        with self._write_connection() as connection, Session(bind=connection) as session:
            workspace = session.scalar(select(ControlWorkspace))
            if workspace is None:
                raise ControlDatabaseCorruptError("workspace record is missing")
            latest_generation = self._latest_generation_number(session, workspace.id)
            if expected_generation is not None and latest_generation != expected_generation:
                raise ConfigurationConflictError(
                    "configuration changed before the snapshot was created",
                    details={
                        "expected_generation": expected_generation,
                        "current_generation": latest_generation,
                    },
                )
            generation = session.get(ControlConfigGeneration, (workspace.id, latest_generation))
            if generation is None:
                raise ControlDatabaseCorruptError("latest configuration generation is missing")
            version = workspace.last_snapshot_version + 1
            record = ControlConfigSnapshot(
                id=f"snap_{uuid.uuid4().hex}",
                workspace_id=workspace.id,
                version=version,
                source_generation=generation.generation,
                label=label,
                note=note,
                fingerprint=generation.fingerprint,
                created_by=created_by,
            )
            session.add(record)
            workspace.last_snapshot_version = version
            session.flush()
            session.refresh(record)
            return self._snapshot_value(record)

    def list_snapshots(self) -> list[ConfigSnapshot]:
        with Session(self.engine) as session:
            rows = list(
                session.scalars(
                    select(ControlConfigSnapshot).order_by(ControlConfigSnapshot.version.desc())
                ).all()
            )
            return [self._snapshot_value(row) for row in rows]

    def delete_snapshot(self, version: int) -> None:
        with self._write_connection() as connection, Session(bind=connection) as session:
            snapshot = session.scalar(
                select(ControlConfigSnapshot).where(ControlConfigSnapshot.version == version)
            )
            if snapshot is None:
                raise ConfigurationError(f"configuration snapshot version {version} does not exist")
            session.delete(snapshot)
            session.flush()

    def restore_snapshot(
        self,
        version: int,
        *,
        expected_generation: int,
        created_by: str | None = None,
        validator: Callable[[WorkspaceConfig], WorkspaceConfig | None] | None = None,
    ) -> LoadedWorkspace:
        with Session(self.engine) as session:
            snapshot = session.scalar(
                select(ControlConfigSnapshot).where(ControlConfigSnapshot.version == version)
            )
            if snapshot is None:
                raise ConfigurationError(f"configuration snapshot version {version} does not exist")
            generation = session.get(
                ControlConfigGeneration,
                (snapshot.workspace_id, snapshot.source_generation),
            )
            if generation is None:
                raise ControlDatabaseCorruptError("snapshot source generation is missing")
            restored = self._validate(generation.document, source=f"snapshot {version}")
        restored = restored.model_copy(update={"config_generation": expected_generation + 1})
        if validator is not None:
            processed = validator(restored)
            if processed is not None:
                restored = processed
        return self.save(
            restored,
            expected_generation=expected_generation,
            reason=f"restore-snapshot:{version}",
            created_by=created_by,
            secret_source_generation=snapshot.source_generation,
        )

    def _load_active(self, session: Session, workspace: ControlWorkspace) -> WorkspaceConfig:
        return self._load_generation(session, workspace.id, workspace.active_generation)

    def _load_latest(self, session: Session, workspace: ControlWorkspace) -> WorkspaceConfig:
        latest = self._latest_generation_number(session, workspace.id)
        return self._load_generation(session, workspace.id, latest)

    @staticmethod
    def _latest_generation_number(session: Session, workspace_id: str) -> int:
        latest = session.scalar(
            select(func.max(ControlConfigGeneration.generation)).where(
                ControlConfigGeneration.workspace_id == workspace_id
            )
        )
        if latest is None:
            raise ControlDatabaseCorruptError("workspace has no configuration generations")
        return int(latest)

    def _load_generation(
        self,
        session: Session,
        workspace_id: str,
        generation_number: int,
    ) -> WorkspaceConfig:
        generation = session.get(
            ControlConfigGeneration,
            (workspace_id, generation_number),
        )
        if generation is None:
            raise ControlDatabaseCorruptError(
                "workspace points to a missing configuration generation",
                details={
                    "workspace_id": workspace_id,
                    "config_generation": generation_number,
                },
            )
        if _fingerprint(generation.document) != generation.fingerprint:
            raise ControlDatabaseCorruptError("stored configuration generation checksum is invalid")
        config = self._validate(
            generation.document,
            source=f"generation {generation_number}",
        )
        if config.config_generation != generation_number:
            raise ControlDatabaseCorruptError("configuration generation metadata is inconsistent")
        return config

    @staticmethod
    def _validate(document: dict[str, Any], *, source: str) -> WorkspaceConfig:
        try:
            return WorkspaceConfig.model_validate(document)
        except ValidationError as exc:
            raise ControlDatabaseCorruptError(
                f"stored configuration {source} is invalid",
                details={"errors": exc.errors(include_url=False)},
            ) from exc

    @staticmethod
    def _write_generation(
        session: Session,
        config: WorkspaceConfig,
        *,
        parent_generation: int | None,
        reason: str,
        fingerprint: str,
        created_by: str | None = None,
    ) -> None:
        session.add(
            ControlConfigGeneration(
                workspace_id=config.workspace.id,
                generation=config.config_generation,
                parent_generation=parent_generation,
                document=config.model_dump(mode="json"),
                fingerprint=fingerprint,
                reason=reason,
                created_by=created_by,
            )
        )

    @staticmethod
    def _secret_ids(document: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(document, dict):
            for key, value in document.items():
                if (
                    isinstance(value, str)
                    and value
                    and (key.endswith("_secret_ref") or key == "secret_key_ref")
                ):
                    found.add(value)
                else:
                    found.update(ConfigRepository._secret_ids(value))
        elif isinstance(document, list):
            for value in document:
                found.update(ConfigRepository._secret_ids(value))
        return found

    @staticmethod
    def _capture_secret_refs(
        session: Session,
        config: WorkspaceConfig,
        *,
        source_generation: int | None = None,
        restore_current: bool = False,
    ) -> None:
        secret_ids = ConfigRepository._secret_ids(config.model_dump(mode="json"))
        if not secret_ids:
            return
        if source_generation is None:
            for secret_id in sorted(secret_ids):
                latest_version = session.scalar(
                    select(ControlSecretVersion.version)
                    .where(ControlSecretVersion.secret_id == secret_id)
                    .order_by(ControlSecretVersion.version.desc())
                    .limit(1)
                )
                if latest_version is not None:
                    session.add(
                        ControlGenerationSecretRef(
                            workspace_id=config.workspace.id,
                            generation=config.config_generation,
                            secret_id=secret_id,
                            secret_version=latest_version,
                        )
                    )
            return

        refs = list(
            session.scalars(
                select(ControlGenerationSecretRef).where(
                    ControlGenerationSecretRef.workspace_id == config.workspace.id,
                    ControlGenerationSecretRef.generation == source_generation,
                    ControlGenerationSecretRef.secret_id.in_(secret_ids),
                )
            ).all()
        )
        for reference in refs:
            session.add(
                ControlGenerationSecretRef(
                    workspace_id=config.workspace.id,
                    generation=config.config_generation,
                    secret_id=reference.secret_id,
                    secret_version=reference.secret_version,
                )
            )
            if not restore_current:
                continue
            secret_version = session.get(
                ControlSecretVersion,
                (reference.secret_id, reference.secret_version),
            )
            if secret_version is None:
                raise ControlDatabaseCorruptError(
                    "configuration generation references a missing secret version",
                    details={"secret_id": reference.secret_id},
                )
            current = session.get(SecretRecord, reference.secret_id)
            if current is None:
                current = SecretRecord(
                    id=reference.secret_id,
                    kind=secret_version.kind,
                    encrypted_value=secret_version.encrypted_value,
                    scopes=[],
                )
                session.add(current)
            else:
                current.kind = secret_version.kind
                current.encrypted_value = secret_version.encrypted_value
                current.status = "active"

    @staticmethod
    def _snapshot_value(record: ControlConfigSnapshot) -> ConfigSnapshot:
        return ConfigSnapshot(
            id=record.id,
            version=record.version,
            source_generation=record.source_generation,
            label=record.label,
            note=record.note,
            fingerprint=record.fingerprint,
            created_at=record.created_at,
        )

    def dispose(self) -> None:
        self.engine.dispose()
