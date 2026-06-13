"""Skill loader — parses SKILL.md bundles and resolves tool symbols.

Each skill is a markdown file with YAML frontmatter:

    ---
    name: list_pods
    description: List pods in a namespace.
    tool: agents.eks.tools:list_pods
    ---
    # When to use
    ...

The loader is the only module that reads SKILL.md. The agent's @tool
function is imported via the frontmatter's `tool:` qualified name.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pydantic
import yaml


class SkillFrontmatter(pydantic.BaseModel):
    name: str
    description: str
    tool: str  # "module.path:symbol"


class Skill(pydantic.BaseModel):
    name: str
    description: str
    tool_symbol: str
    body: str


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def parse_skill_md(text: str) -> tuple[SkillFrontmatter, str]:
    """Split a SKILL.md into (frontmatter model, body markdown)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("SKILL.md must start with YAML frontmatter delimited by '---'")
    fm = SkillFrontmatter(**yaml.safe_load(m.group(1)))
    return fm, m.group(2).strip()


def resolve(skill_name: str, agent_name: str, repo_root: Path | None = None) -> Skill:
    """Resolve a skill by name. Agent-specific path wins over shared.

    Resolution order:
    1. ``agents/<agent_name>/skills/<skill_name>/SKILL.md``
    2. ``skills/<skill_name>/SKILL.md``
    """
    root = repo_root if repo_root is not None else _find_repo_root()
    candidates = [
        root / "agents" / agent_name / "skills" / skill_name / "SKILL.md",
        root / "skills" / skill_name / "SKILL.md",
    ]
    for path in candidates:
        if path.exists():
            fm, body = parse_skill_md(path.read_text())
            if fm.name != skill_name:
                raise ValueError(
                    f"SKILL.md frontmatter name {fm.name!r} doesn't match skill {skill_name!r}"
                )
            return Skill(name=fm.name, description=fm.description, tool_symbol=fm.tool, body=body)

    searched = [str(p) for p in candidates]
    raise FileNotFoundError(f"No SKILL.md found for skill {skill_name!r} (agent {agent_name!r}). Searched: {searched}")


def _find_repo_root() -> Path:
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "config.yaml").exists():
            return parent
    # config.yaml is no longer on disk in AWS runtime images (#23 externalized
    # it to SSM). Fall back to the source layout: skill_loader.py always lives
    # at <root>/shared/skill_loader.py, so its package parent is the repo root
    # in both the container (/app) and local checkouts.
    return Path(__file__).resolve().parent.parent


def import_tool(tool_symbol: str):
    """Import a Strands @tool function by qualified name ``module:symbol``."""
    if ":" not in tool_symbol:
        raise ValueError(f"Tool symbol must be 'module:name', got {tool_symbol!r}")
    module_path, attr = tool_symbol.split(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr)


