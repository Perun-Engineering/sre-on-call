"""Tests for shared.agents — Agent dataclass, CATALOGUE, AgentRegistry."""

from __future__ import annotations


import pytest

from shared.agents import (
    AgentRegistry,
    CATALOGUE,
    catalogue_ids,
    get_registry,
    reset_cache,
)
from shared.config import AgentConfig, Defaults, ProjectConfig


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    reset_cache()
    yield
    reset_cache()


def _config(*, agents: dict[str, AgentConfig] | None = None) -> ProjectConfig:
    """Build a minimal ProjectConfig with the given agents block."""
    if agents is None:
        agents = {
            "master": AgentConfig(skills=["investigate_alert"], mcps=[]),
            "slack_scanner": AgentConfig(enabled=True, skills=["scan_slack_channels"]),
            "discord_scanner": AgentConfig(enabled=False, skills=["scan_discord_channels"]),
            "cloudwatch_logs": AgentConfig(enabled=True, skills=["query_cloudwatch_logs"]),
            "eks": AgentConfig(enabled=True, network_mode="VPC", skills=["gather_eks_state"]),
        }
    return ProjectConfig(
        project="test",
        environment="dev",
        defaults=Defaults(model_id="anthropic.claude-test"),
        agents=agents,
    )


# ---------------------------------------------------------------------------
# Catalogue invariants
# ---------------------------------------------------------------------------


class TestCatalogueInvariants:
    """Static catalogue facts that must hold regardless of deployment config."""

    def test_catalogue_includes_master_and_six_specialized(self):
        ids = {a.id for a in CATALOGUE}
        assert ids == {
            "master",
            "slack_scanner",
            "discord_scanner",
            "cloudwatch_logs",
            "eks",
            "prometheus",
            "incident_history",
        }

    def test_master_is_orchestrator_kind(self):
        master = next(a for a in CATALOGUE if a.id == "master")
        assert master.kind == "orchestrator"

    def test_all_other_agents_are_specialized_kind(self):
        for agent in CATALOGUE:
            if agent.id == "master":
                continue
            assert agent.kind == "specialized", (
                f"{agent.id} kind should be 'specialized', got {agent.kind!r}"
            )

    def test_unique_ids(self):
        ids = [a.id for a in CATALOGUE]
        assert len(ids) == len(set(ids)), f"duplicate ids in CATALOGUE: {ids}"

    def test_unique_runtime_arn_envs(self):
        envs = [a.runtime_arn_env for a in CATALOGUE]
        assert len(envs) == len(set(envs)), f"duplicate runtime_arn_env keys: {envs}"

    def test_unique_url_envs(self):
        envs = [a.url_env for a in CATALOGUE]
        assert len(envs) == len(set(envs)), f"duplicate url_env keys: {envs}"

    def test_unique_default_local_urls(self):
        urls = [a.default_local_url for a in CATALOGUE]
        assert len(urls) == len(set(urls)), f"duplicate default_local_urls: {urls}"

    def test_unique_render_orders(self):
        orders = [a.order for a in CATALOGUE]
        assert len(orders) == len(set(orders)), f"duplicate render orders: {orders}"

    def test_emoji_non_empty(self):
        for agent in CATALOGUE:
            assert agent.emoji, f"{agent.id} has empty emoji"

    def test_display_name_non_empty(self):
        for agent in CATALOGUE:
            assert agent.display_name, f"{agent.id} has empty display_name"

    def test_runtime_arn_env_naming_convention(self):
        # Runtime ARN env vars consistently end in _RUNTIME_ARN.
        for agent in CATALOGUE:
            assert agent.runtime_arn_env.endswith("_RUNTIME_ARN"), (
                f"{agent.id}.runtime_arn_env {agent.runtime_arn_env!r} "
                f"breaks the *_RUNTIME_ARN naming convention"
            )

    def test_catalogue_ids_helper_matches_records(self):
        assert catalogue_ids() == frozenset(a.id for a in CATALOGUE)


# ---------------------------------------------------------------------------
# Agent.resolve_endpoint
# ---------------------------------------------------------------------------


class TestResolveEndpoint:
    """Endpoint resolution priority: runtime ARN > URL env > default local URL."""

    def test_runtime_arn_wins(self, monkeypatch):
        monkeypatch.setenv("EKS_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:0:r/x")
        monkeypatch.setenv("EKS_AGENT_URL", "http://override:9999")
        agent = next(a for a in CATALOGUE if a.id == "eks")

        assert agent.resolve_endpoint() == "arn:aws:bedrock-agentcore:us-east-1:0:r/x"

    def test_url_used_when_arn_unset(self, monkeypatch):
        monkeypatch.delenv("EKS_AGENT_RUNTIME_ARN", raising=False)
        monkeypatch.setenv("EKS_AGENT_URL", "http://override:9999")
        agent = next(a for a in CATALOGUE if a.id == "eks")

        assert agent.resolve_endpoint() == "http://override:9999"

    def test_default_used_when_both_unset(self, monkeypatch):
        monkeypatch.delenv("EKS_AGENT_RUNTIME_ARN", raising=False)
        monkeypatch.delenv("EKS_AGENT_URL", raising=False)
        agent = next(a for a in CATALOGUE if a.id == "eks")

        assert agent.resolve_endpoint() == "http://localhost:9005"

    def test_empty_string_treated_as_unset(self, monkeypatch):
        # Empty string env values are common from Lambda terraform when a
        # variable is wired but unset. Treat them as unset, not as a literal
        # empty endpoint.
        monkeypatch.setenv("EKS_AGENT_RUNTIME_ARN", "")
        monkeypatch.setenv("EKS_AGENT_URL", "")
        agent = next(a for a in CATALOGUE if a.id == "eks")

        assert agent.resolve_endpoint() == "http://localhost:9005"


# ---------------------------------------------------------------------------
# AgentRegistry — folding config.yaml into the catalogue
# ---------------------------------------------------------------------------


class TestRegistryFold:
    """Construction folds AgentConfig into the catalogue records."""

    def test_listed_agent_carries_deployment_fields(self):
        registry = AgentRegistry(_config())
        eks = registry.lookup("eks")

        assert eks.deployed is True
        assert eks.is_active is True
        assert eks.enabled is True
        assert eks.skills == ["gather_eks_state"]
        assert eks.network_mode == "VPC"

    def test_unlisted_agent_has_none_deployment_fields(self):
        # Prometheus is in the catalogue but not in config.yaml.
        registry = AgentRegistry(_config())
        prom = registry.lookup("prometheus")

        assert prom.deployed is False
        assert prom.is_active is False
        assert prom.enabled is None
        assert prom.skills is None
        assert prom.mcps is None
        assert prom.network_mode is None

    def test_disabled_agent_is_deployed_but_not_active(self):
        registry = AgentRegistry(_config())
        discord = registry.lookup("discord_scanner")

        assert discord.deployed is True
        assert discord.is_active is False
        assert discord.enabled is False


# ---------------------------------------------------------------------------
# Registry queries
# ---------------------------------------------------------------------------


class TestRegistryQueries:
    """all() / deployed() / active() / disabled_in_config() / lookup()."""

    def test_all_returns_full_catalogue(self):
        registry = AgentRegistry(_config())
        ids = [a.id for a in registry.all()]
        assert ids == [a.id for a in CATALOGUE]  # preserves catalogue order

    def test_all_filters_by_kind(self):
        registry = AgentRegistry(_config())
        specialized_ids = {a.id for a in registry.all(kind="specialized")}
        orchestrator_ids = {a.id for a in registry.all(kind="orchestrator")}

        assert "master" not in specialized_ids
        assert specialized_ids == {
            "slack_scanner",
            "discord_scanner",
            "cloudwatch_logs",
            "eks",
            "prometheus",
            "incident_history",
        }
        assert orchestrator_ids == {"master"}

    def test_deployed_excludes_unlisted_agents(self):
        registry = AgentRegistry(_config())
        deployed_ids = {a.id for a in registry.deployed()}

        # Prometheus is in the catalogue but not in config.yaml.
        assert "prometheus" not in deployed_ids
        assert deployed_ids == {
            "master",
            "slack_scanner",
            "discord_scanner",
            "cloudwatch_logs",
            "eks",
        }

    def test_active_excludes_disabled_in_config(self):
        registry = AgentRegistry(_config())
        active_ids = {a.id for a in registry.active(kind="specialized")}

        # discord_scanner has enabled=False; prometheus is unlisted.
        assert active_ids == {"slack_scanner", "cloudwatch_logs", "eks"}

    def test_active_includes_master(self):
        registry = AgentRegistry(_config())
        master = next(a for a in registry.active(kind="orchestrator"))
        assert master.id == "master"

    def test_disabled_in_config_only_includes_explicitly_disabled(self):
        registry = AgentRegistry(_config())
        disabled_ids = {a.id for a in registry.disabled_in_config(kind="specialized")}

        # Only discord_scanner — prometheus is unlisted (not "disabled"; "absent").
        assert disabled_ids == {"discord_scanner"}

    def test_lookup_returns_record(self):
        registry = AgentRegistry(_config())
        eks = registry.lookup("eks")
        assert eks.id == "eks"
        assert eks.display_name == "EKS Cluster State"

    def test_lookup_unknown_raises(self):
        registry = AgentRegistry(_config())
        with pytest.raises(KeyError, match="typo"):
            registry.lookup("typo")


# ---------------------------------------------------------------------------
# Process-wide cache
# ---------------------------------------------------------------------------


class TestRegistryCache:
    """get_registry() caches; reset_cache() invalidates."""

    def test_get_registry_returns_same_instance(self, monkeypatch):
        # Avoid touching the real config.yaml by stubbing load.
        from shared import config as config_module

        monkeypatch.setattr(config_module, "load", lambda *_: _config())
        config_module.reset_cache()

        first = get_registry()
        second = get_registry()
        assert first is second

    def test_reset_cache_drops_instance(self, monkeypatch):
        from shared import config as config_module

        monkeypatch.setattr(config_module, "load", lambda *_: _config())
        config_module.reset_cache()

        first = get_registry()
        reset_cache()
        second = get_registry()
        assert first is not second
