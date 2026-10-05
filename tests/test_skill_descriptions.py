"""Every bundled skill's frontmatter description fits the skill list.

The description is the trigger: it is what the model reads when deciding to
load a skill. Past 1024 characters it is truncated in the skill list, so the
end of an over-long description — often the triggers — silently never reaches
the model. Three bundled skills had drifted past it.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL_FILES = sorted((ROOT / "skills").glob("*/SKILL.md"))
LIMIT = 1024


def _frontmatter_description(text: str) -> str:
    assert text.startswith("---\n"), "SKILL.md must open with a frontmatter block"
    lines = text.split("\n")
    end = lines.index("---", 1)
    for i, line in enumerate(lines[1:end], 1):
        if not line.startswith("description:"):
            continue
        value = line[len("description:"):].strip()
        if value in (">", "|", ">-", "|-"):                     # block scalar: the indented lines below
            body = []
            for nxt in lines[i + 1:end]:
                if nxt and not nxt.startswith((" ", "\t")):
                    break
                body.append(nxt.strip())
            return (" " if value.startswith(">") else "\n").join(body).strip()
        if value.startswith('"'):
            return json.loads(value)                             # YAML double-quoted escapes are JSON's
        if value.startswith("'"):
            return value[1:-1].replace("''", "'")
        return value
    raise AssertionError("frontmatter has no description")


def test_there_are_skills_to_check():
    assert len(SKILL_FILES) >= 8


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_description_fits_the_skill_list(path):
    desc = _frontmatter_description(path.read_text(encoding="utf-8"))
    assert desc, f"{path.parent.name}: empty description"
    assert len(desc) <= LIMIT, f"{path.parent.name}: description is {len(desc)} chars (limit {LIMIT})"
