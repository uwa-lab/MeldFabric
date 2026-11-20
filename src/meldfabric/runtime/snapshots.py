"""Immutable, reference-counted configuration snapshots."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime

from meldfabric.config.models import WorkspaceConfig


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    revision: int
    fingerprint: str
    created_at: datetime
    config: WorkspaceConfig

    @classmethod
    def compile(cls, config: WorkspaceConfig) -> RuntimeSnapshot:
        payload = config.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return cls(
            revision=config.config_generation,
            fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            created_at=datetime.now(UTC),
            config=deepcopy(config),
        )


class SnapshotRegistry:
    """Publishes new snapshots while keeping in-flight runs pinned to old revisions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: RuntimeSnapshot | None = None
        self._snapshots: dict[int, RuntimeSnapshot] = {}
        self._references: dict[int, int] = {}

    @property
    def current(self) -> RuntimeSnapshot:
        with self._lock:
            if self._current is None:
                raise RuntimeError("no runtime snapshot has been published")
            return self._current

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        with self._lock:
            if self._current is not None and snapshot.revision <= self._current.revision:
                raise ValueError(
                    "a new snapshot revision must be greater than the current revision"
                )
            self._snapshots[snapshot.revision] = snapshot
            self._references.setdefault(snapshot.revision, 0)
            self._current = snapshot
            self._collect_retired()

    def restore(self, snapshot: RuntimeSnapshot, *, retain: bool = False) -> None:
        """Install a historical snapshot without making it the active configuration."""

        with self._lock:
            if self._current is None:
                raise RuntimeError("cannot restore a revision before publishing the current one")
            if snapshot.revision >= self._current.revision:
                raise ValueError("only revisions older than the current snapshot can be restored")
            existing = self._snapshots.get(snapshot.revision)
            if existing is not None and existing.fingerprint != snapshot.fingerprint:
                raise ValueError(
                    f"configuration revision {snapshot.revision} has conflicting content"
                )
            self._snapshots[snapshot.revision] = snapshot
            self._references.setdefault(snapshot.revision, 0)
            if retain:
                self._references[snapshot.revision] += 1

    @contextmanager
    def pin_current(self) -> Iterator[RuntimeSnapshot]:
        with self.pin() as snapshot:
            yield snapshot

    @contextmanager
    def pin(self, revision: int | None = None) -> Iterator[RuntimeSnapshot]:
        with self._lock:
            snapshot = self.current if revision is None else self.snapshot(revision)
            self._references[snapshot.revision] += 1
        try:
            yield snapshot
        finally:
            with self._lock:
                self._references[snapshot.revision] -= 1
                self._collect_retired()

    def retain(self, revision: int) -> None:
        """Retain a snapshot beyond one request, for example while a run is interrupted."""

        with self._lock:
            self.snapshot(revision)
            self._references[revision] += 1

    def release(self, revision: int) -> None:
        with self._lock:
            count = self._references.get(revision, 0)
            if count <= 0:
                raise RuntimeError(f"runtime snapshot revision {revision} is not retained")
            self._references[revision] = count - 1
            self._collect_retired()

    def _collect_retired(self) -> None:
        if self._current is None:
            return
        current_revision = self._current.revision
        retired = [
            revision
            for revision, count in self._references.items()
            if revision != current_revision and count == 0
        ]
        for revision in retired:
            self._references.pop(revision, None)
            self._snapshots.pop(revision, None)

    def active_revisions(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(sorted(self._snapshots))

    def snapshot(self, revision: int) -> RuntimeSnapshot:
        with self._lock:
            try:
                return self._snapshots[revision]
            except KeyError as exc:
                raise KeyError(f"runtime snapshot revision {revision} is no longer active") from exc
