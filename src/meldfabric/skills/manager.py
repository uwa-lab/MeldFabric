"""Safe Skill directory scanning and deterministic overlay reporting."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from meldfabric.config.models import SkillSource, WorkspaceConfig

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_MAX_SKILL_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DiscoveredSkill:
    name: str
    description: str
    path: str
    source_id: str
    source_order: int
    allowed_tools: tuple[str, ...] = ()
    effective: bool = True
    overridden_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "source_id": self.source_id,
            "source_order": self.source_order,
            "allowed_tools": list(self.allowed_tools),
            "effective": self.effective,
            "overridden_by": self.overridden_by,
        }


@dataclass(frozen=True, slots=True)
class SkillSourceScan:
    source_id: str
    path: str
    status: str
    skills: tuple[DiscoveredSkill, ...] = ()
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "path": self.path,
            "status": self.status,
            "skills": [item.as_dict() for item in self.skills],
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    sources: tuple[SkillSourceScan, ...]
    effective_skills: tuple[DiscoveredSkill, ...]
    all_skills: tuple[DiscoveredSkill, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sources": [item.as_dict() for item in self.sources],
            "effective_skills": [item.as_dict() for item in self.effective_skills],
            "all_skills": [item.as_dict() for item in self.all_skills],
        }


class SkillManager:
    def __init__(self, *, config_dir: Path) -> None:
        self.config_dir = config_dir.resolve()

    def resolve_path(self, source: SkillSource) -> Path:
        path = Path(source.path).expanduser()
        if not path.is_absolute():
            path = self.config_dir / path
        return path.resolve()

    def runtime_sources(
        self,
        config: WorkspaceConfig,
        source_ids: set[str],
    ) -> list[tuple[str, Path, SkillSource]]:
        selected = [
            (index, source)
            for index, source in enumerate(config.resources.skill_sources)
            if source.enabled and source.id in source_ids
        ]
        selected.sort(key=lambda pair: (pair[1].order, pair[0]))
        return [
            (f"/__meldfabric_skills/{source.id}", self.resolve_path(source), source)
            for _, source in selected
        ]

    def scan_source(self, source: SkillSource) -> SkillSourceScan:
        path = self.resolve_path(source)
        if not source.enabled:
            return SkillSourceScan(
                source_id=source.id,
                path=str(path),
                status="disabled",
            )
        if not path.exists():
            return SkillSourceScan(
                source_id=source.id,
                path=str(path),
                status="error",
                errors=("Skill source directory does not exist",),
            )
        if not path.is_dir():
            return SkillSourceScan(
                source_id=source.id,
                path=str(path),
                status="error",
                errors=("Skill source path is not a directory",),
            )

        skills: list[DiscoveredSkill] = []
        errors: list[str] = []
        try:
            candidates = sorted(path.glob("*/SKILL.md"))
        except OSError as exc:
            return SkillSourceScan(
                source_id=source.id,
                path=str(path),
                status="error",
                errors=(f"Unable to list Skill source: {exc}",),
            )
        for skill_file in candidates:
            try:
                skill = self._read_skill(source, skill_file)
            except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                errors.append(f"{skill_file}: {exc}")
                continue
            skills.append(skill)
        status = "partial" if errors and skills else "error" if errors else "ok"
        return SkillSourceScan(
            source_id=source.id,
            path=str(path),
            status=status,
            skills=tuple(skills),
            errors=tuple(errors),
        )

    def scan_workspace(self, config: WorkspaceConfig) -> SkillCatalog:
        indexed = list(enumerate(config.resources.skill_sources))
        indexed.sort(key=lambda pair: (pair[1].order, pair[0]))
        scans = [self.scan_source(source) for _, source in indexed]
        all_skills: list[DiscoveredSkill] = []
        effective_by_name: dict[str, DiscoveredSkill] = {}
        for scan in scans:
            for skill in scan.skills:
                previous = effective_by_name.get(skill.name)
                if previous is not None:
                    all_skills = [
                        item
                        if item is not previous
                        else DiscoveredSkill(
                            name=item.name,
                            description=item.description,
                            path=item.path,
                            source_id=item.source_id,
                            source_order=item.source_order,
                            allowed_tools=item.allowed_tools,
                            effective=False,
                            overridden_by=skill.source_id,
                        )
                        for item in all_skills
                    ]
                all_skills.append(skill)
                effective_by_name[skill.name] = skill
        return SkillCatalog(
            sources=tuple(scans),
            effective_skills=tuple(effective_by_name.values()),
            all_skills=tuple(all_skills),
        )

    @staticmethod
    def _read_skill(source: SkillSource, skill_file: Path) -> DiscoveredSkill:
        if skill_file.stat().st_size > _MAX_SKILL_BYTES:
            raise ValueError("SKILL.md exceeds the 10 MiB limit")
        content = skill_file.read_text(encoding="utf-8")
        match = _FRONTMATTER.match(content)
        if match is None:
            raise ValueError("SKILL.md has no valid YAML frontmatter")
        metadata = yaml.safe_load(match.group(1))
        if not isinstance(metadata, dict):
            raise ValueError("SKILL.md frontmatter must be a mapping")
        name = str(metadata.get("name", "")).strip()
        description = str(metadata.get("description", "")).strip()
        if not name or not description:
            raise ValueError("SKILL.md requires non-empty name and description")
        raw_allowed = metadata.get("allowed-tools", [])
        if isinstance(raw_allowed, str):
            allowed_tools = tuple(item for item in raw_allowed.split() if item)
        elif isinstance(raw_allowed, list):
            allowed_tools = tuple(str(item) for item in raw_allowed)
        else:
            raise ValueError("allowed-tools must be a string or list")
        return DiscoveredSkill(
            name=name,
            description=description[:1024],
            path=str(skill_file.resolve()),
            source_id=source.id,
            source_order=source.order,
            allowed_tools=allowed_tools,
        )
