from __future__ import annotations

from pathlib import Path

from meldfabric.config.defaults import create_default_config
from meldfabric.config.models import SkillSource
from meldfabric.skills import SkillManager


def write_skill(root: Path, name: str, description: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_skill_scan_reports_deterministic_overrides_and_errors(tmp_path: Path) -> None:
    base = tmp_path / "base-skills"
    project = tmp_path / "project-skills"
    write_skill(base, "review", "Base review workflow")
    write_skill(base, "research", "Research workflow")
    write_skill(project, "review", "Project review workflow")
    broken = project / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("not-frontmatter", encoding="utf-8")

    config = create_default_config()
    config.resources.skill_sources = [
        SkillSource(id="skills_base", name="base", path=str(base), order=10),
        SkillSource(id="skills_project", name="project", path=str(project), order=20),
        SkillSource(
            id="skills_missing",
            name="missing",
            path=str(tmp_path / "missing"),
            order=30,
        ),
    ]
    catalog = SkillManager(config_dir=tmp_path).scan_workspace(config)

    effective = {item.name: item for item in catalog.effective_skills}
    base_review = next(
        item
        for item in catalog.all_skills
        if item.name == "review" and item.source_id == "skills_base"
    )
    assert effective["review"].source_id == "skills_project"
    assert base_review.effective is False
    assert base_review.overridden_by == "skills_project"
    assert catalog.sources[1].status == "partial"
    assert catalog.sources[2].status == "error"
