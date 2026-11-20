"""Revision-scoped copies of process-level decorated tool candidates."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from meldfabric.tools.registry import ToolDefinition, registered_tool_snapshot


@dataclass(frozen=True, slots=True)
class ToolImportIssue:
    module: str
    error: str


class WorkspaceToolRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._definitions: dict[int, dict[str, ToolDefinition]] = {}
        self._issues: dict[int, tuple[ToolImportIssue, ...]] = {}
        self._generations: dict[int, int] = {}

    def capture(self, revision: int) -> None:
        with self._lock:
            self._generations.pop(revision, None)
            self.refresh(revision)

    def refresh(self, revision: int) -> int:
        """Adopt executed decorators without importing files or saving configuration."""
        with self._lock:
            generation, candidates = registered_tool_snapshot()
            if self._generations.get(revision) == generation:
                return generation
            issues: list[ToolImportIssue] = []
            definitions = {item.id: item for item in candidates}
            name_owners: dict[str, str] = {}
            for item in definitions.values():
                previous = name_owners.get(item.name)
                if previous is not None and previous != item.id:
                    issues.append(
                        ToolImportIssue(
                            module=item.python_path,
                            error=(
                                f"tool name {item.name!r} is shared by IDs "
                                f"{previous!r} and {item.id!r}"
                            ),
                        )
                    )
                else:
                    name_owners[item.name] = item.id
            self._definitions[revision] = definitions
            self._issues[revision] = tuple(issues)
            self._generations[revision] = generation
            return generation

    def snapshot(
        self,
        revision: int,
        *,
        refresh: bool = False,
    ) -> tuple[int, dict[str, ToolDefinition]]:
        with self._lock:
            if refresh:
                self.refresh(revision)
            return self._generations.get(revision, -1), dict(self._definitions.get(revision, {}))

    def definitions(self, revision: int) -> dict[str, ToolDefinition]:
        return self.snapshot(revision)[1]

    def issues(self, revision: int) -> tuple[ToolImportIssue, ...]:
        with self._lock:
            return self._issues.get(revision, ())

    def prune(self, active_revisions: set[int]) -> None:
        with self._lock:
            for revision in list(self._definitions):
                if revision not in active_revisions:
                    self._definitions.pop(revision, None)
                    self._issues.pop(revision, None)
                    self._generations.pop(revision, None)
