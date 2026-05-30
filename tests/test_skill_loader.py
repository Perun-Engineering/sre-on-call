"""Tests for shared.skill_loader."""
from __future__ import annotations
import textwrap
from pathlib import Path

import pytest

from shared.skill_loader import (
    import_tool,
    parse_skill_md,
    resolve,
)


def test_parse_skill_md_extracts_frontmatter_and_body():
    text = textwrap.dedent(
        """\
        ---
        name: list_pods
        description: List pods in a namespace.
        tool: agents.eks.tools:list_pods
        ---
        # When to use

        Body text.
        """
    )
    fm, body = parse_skill_md(text)
    assert fm.name == "list_pods"
    assert fm.tool == "agents.eks.tools:list_pods"
    assert body.startswith("# When to use")
    assert body.endswith("Body text.")


def test_parse_skill_md_rejects_missing_frontmatter():
    with pytest.raises(ValueError, match="frontmatter"):
        parse_skill_md("# No frontmatter here\n\nbody only")


def test_resolve_finds_agent_specific_skill_first(tmp_path: Path):
    repo = tmp_path
    skill_dir = repo / "agents" / "eks" / "skills" / "list_pods"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: list_pods\ndescription: agent-scoped\ntool: x:y\n---\nbody\n"
    )
    shared_dir = repo / "skills" / "list_pods"
    shared_dir.mkdir(parents=True)
    (shared_dir / "SKILL.md").write_text(
        "---\nname: list_pods\ndescription: shared-scoped\ntool: x:y\n---\nshared body\n"
    )

    skill = resolve("list_pods", "eks", repo_root=repo)
    assert skill.description == "agent-scoped"  # agent-specific wins


def test_resolve_falls_back_to_shared(tmp_path: Path):
    repo = tmp_path
    shared_dir = repo / "skills" / "aws_credentials"
    shared_dir.mkdir(parents=True)
    (shared_dir / "SKILL.md").write_text(
        "---\nname: aws_credentials\ndescription: shared\ntool: shared.tools.aws:assume\n---\nshared body\n"
    )

    skill = resolve("aws_credentials", "eks", repo_root=repo)
    assert skill.description == "shared"
    assert skill.tool_symbol == "shared.tools.aws:assume"


def test_resolve_raises_when_neither_path_exists(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="No SKILL.md"):
        resolve("nonexistent", "eks", repo_root=tmp_path)


def test_resolve_rejects_name_mismatch(tmp_path: Path):
    repo = tmp_path
    skill_dir = repo / "agents" / "eks" / "skills" / "list_pods"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: WRONG_NAME\ndescription: x\ntool: x:y\n---\nbody\n"
    )
    with pytest.raises(ValueError, match="frontmatter name"):
        resolve("list_pods", "eks", repo_root=repo)


def test_import_tool_resolves_module_and_attr():
    tool = import_tool("shared.skill_loader:resolve")
    assert tool is resolve


def test_import_tool_rejects_invalid_format():
    with pytest.raises(ValueError, match="module:name"):
        import_tool("no_colon_here")
