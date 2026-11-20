"""Bootstrap connection for the MeldFabric control database.

The control database is deployment infrastructure.  It is deliberately not a
``DatabaseConnection`` resource and is never exposed to Agents as a persistence
target.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from meldfabric.config.structured import validate_database_options
from meldfabric.errors import ConfigurationError

ControlDatabaseEngine = Literal["sqlite", "postgresql", "mysql"]


def _engine(url: str) -> ControlDatabaseEngine:
    scheme = urlsplit(url).scheme.split("+", 1)[0]
    if scheme == "postgres":
        scheme = "postgresql"
    if scheme not in {"sqlite", "postgresql", "mysql"}:
        raise ConfigurationError(
            "control database must use SQLite, PostgreSQL, or MySQL",
            details={"scheme": scheme or None},
        )
    return cast(ControlDatabaseEngine, scheme)


def _sqlite_path(url: str, *, state_dir: Path) -> Path:
    prefixes = ("sqlite+aiosqlite:///", "sqlite:///")
    prefix = next((value for value in prefixes if url.startswith(value)), None)
    if prefix is None:
        raise ConfigurationError("SQLite control database must use a file URL")
    raw = url[len(prefix) :]
    if raw == ":memory:":
        raise ConfigurationError("the control database cannot use in-memory SQLite")
    path = Path(raw)
    if not path.is_absolute():
        path = state_dir / path
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _replace_scheme(url: str, scheme: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


@dataclass(frozen=True, slots=True)
class ControlDatabaseSpec:
    """Resolved bootstrap coordinates for one independent control database."""

    engine: ControlDatabaseEngine
    sync_url: str
    async_url: str
    state_dir: Path
    options: dict[str, Any]

    @classmethod
    def resolve(
        cls,
        database: str | None,
        *,
        state_dir: str | Path = ".meldfabric",
        options: dict[str, Any] | None = None,
    ) -> ControlDatabaseSpec:
        root = Path(state_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        url = database or "sqlite+aiosqlite:///control.db"
        engine = _engine(url)
        if engine == "sqlite":
            path = _sqlite_path(url, state_dir=root)
            sync_url = f"sqlite:///{path}"
            async_url = f"sqlite+aiosqlite:///{path}"
        elif engine == "postgresql":
            sync_url = _replace_scheme(url, "postgresql+psycopg")
            async_url = _replace_scheme(url, "postgresql+asyncpg")
        else:
            sync_url = _replace_scheme(url, "mysql+pymysql")
            async_url = _replace_scheme(url, "mysql+asyncmy")
        validated_options = validate_database_options(options or {}, engine=engine)
        return cls(
            engine=engine,
            sync_url=sync_url,
            async_url=async_url,
            state_dir=root,
            options=validated_options,
        )

    @property
    def fingerprint(self) -> str:
        """Stable identity that never exposes credentials."""

        parsed = urlsplit(self.sync_url)
        host = parsed.hostname or ""
        port = parsed.port or 0
        identity = f"{self.engine}|{host}|{port}|{parsed.path}|{parsed.query}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @property
    def display(self) -> str:
        parsed = urlsplit(self.sync_url)
        if self.engine == "sqlite":
            return str(Path(parsed.path).resolve())
        host = parsed.hostname or "localhost"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{self.engine}://{host}{port}{parsed.path}"
