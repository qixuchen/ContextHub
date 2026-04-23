from __future__ import annotations

from pathlib import Path

import pytest

from core.skills.skill_loader import load_skills

pytestmark = pytest.mark.unit


def test_load_skills_reads_valid_frontmatter(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill-a"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: skill-a\ndescription: useful skill\n---\n# body\n",
        encoding="utf-8",
    )
    docs, warnings = load_skills(root=str(tmp_path))
    assert warnings == []
    assert len(docs) == 1
    assert docs[0].skill_name == "skill-a"
    assert docs[0].description == "useful skill"
    assert docs[0].content_sha256


def test_load_skills_skips_invalid_entries(tmp_path: Path) -> None:
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "SKILL.md").write_text(
        "---\nname: bad\n---\n# body\n",
        encoding="utf-8",
    )
    docs, warnings = load_skills(root=str(tmp_path))
    assert docs == []
    assert any("missing description" in w for w in warnings)

