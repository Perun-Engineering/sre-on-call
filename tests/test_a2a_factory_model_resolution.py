"""Model-id resolution precedence in the A2A factory (issue #81).

``_resolve_agent_model`` builds the dispatch :class:`BedrockModel`; the snapshot
header needs the same resolved id as a *string*. Both must read from one source
of truth — :func:`shared.a2a_factory._resolve_agent_model_id` — so the displayed
``model=…`` label can never drift from the model actually dispatched on.

Precedence: per-agent config ``model_id`` > ``MODEL_ID`` env > ``defaults.model_id``
> bundled ``DEFAULT_MODEL_ID``.
"""

from __future__ import annotations

import pytest

from shared.a2a_factory import (
    DEFAULT_MODEL_ID,
    _resolve_agent_model,
    _resolve_agent_model_id,
)
from shared.config import AgentConfig, Defaults, ProjectConfig


def _config(*, agents: dict[str, AgentConfig], default_model: str = "default-model") -> ProjectConfig:
    return ProjectConfig(
        project="test",
        environment="dev",
        defaults=Defaults(model_id=default_model),
        agents=agents,
    )


@pytest.fixture(autouse=True)
def _clean_model_env(monkeypatch):
    monkeypatch.delenv("MODEL_ID", raising=False)


def test_per_agent_model_id_wins_over_env_and_default(monkeypatch):
    monkeypatch.setenv("MODEL_ID", "env-haiku")
    cfg = _config(
        agents={"master": AgentConfig(model_id="us.anthropic.claude-sonnet-4-6")},
        default_model="default-haiku",
    )
    assert _resolve_agent_model_id(cfg, "master") == "us.anthropic.claude-sonnet-4-6"


def test_env_wins_when_no_per_agent_override(monkeypatch):
    monkeypatch.setenv("MODEL_ID", "env-haiku")
    cfg = _config(agents={"slack_scanner": AgentConfig()}, default_model="default-haiku")
    assert _resolve_agent_model_id(cfg, "slack_scanner") == "env-haiku"


def test_defaults_model_when_no_override_and_no_env():
    cfg = _config(agents={"slack_scanner": AgentConfig()}, default_model="default-haiku")
    assert _resolve_agent_model_id(cfg, "slack_scanner") == "default-haiku"


def test_unknown_agent_falls_through_to_defaults_then_bundled():
    cfg = _config(agents={}, default_model="default-haiku")
    # No per-agent config for "ghost" → defaults.model_id.
    assert _resolve_agent_model_id(cfg, "ghost") == "default-haiku"


def test_resolved_id_matches_dispatched_bedrock_model(monkeypatch):
    """The header string must equal the id the dispatch BedrockModel runs on —
    single source of truth, so the label can't drift from reality."""
    monkeypatch.setenv("MODEL_ID", "env-haiku")
    cfg = _config(
        agents={
            "master": AgentConfig(model_id="us.anthropic.claude-sonnet-4-6"),
            "slack_scanner": AgentConfig(),
        },
        default_model="default-haiku",
    )
    for agent_name in ("master", "slack_scanner"):
        model = _resolve_agent_model(cfg, agent_name)
        assert _resolve_agent_model_id(cfg, agent_name) == model.config.get("model_id")
