"""Tests for shared.config — ProjectConfig schema validation."""
from __future__ import annotations
import pytest
from shared.config import ProjectConfig


@pytest.fixture(autouse=True)
def _reset_config_cache():
    from shared.config import reset_cache
    reset_cache()
    yield
    reset_cache()


def _base() -> dict:
    return {
        "project": "test",
        "environment": "dev",
        "defaults": {"model_id": "anthropic.claude-haiku-4-5", "network_mode": "PUBLIC"},
        "agents": {
            "master": {"skills": ["investigate_alert"], "mcps": []},
            "slack_scanner": {"enabled": True, "skills": ["scan_slack_channels"], "mcps": []},
            "discord_scanner": {"enabled": True, "skills": ["scan_discord_channels"], "mcps": []},
            "cloudwatch_logs": {"enabled": True, "skills": ["query_cloudwatch_logs"], "mcps": []},
            "eks": {"enabled": True, "network_mode": "VPC", "skills": ["gather_eks_state"], "mcps": []},
        },
    }


def test_minimal_valid_config_parses():
    cfg = ProjectConfig(**_base())
    assert cfg.agents["master"].enabled is True  # master defaults to enabled
    assert cfg.agents["eks"].network_mode == "VPC"


def test_agent_model_id_defaults_to_none():
    """Configs without a per-agent model_id still validate; field defaults None."""
    cfg = ProjectConfig(**_base())
    assert cfg.agents["master"].model_id is None


def test_agent_model_id_accepted_when_set():
    data = _base()
    data["agents"]["master"]["model_id"] = "us.anthropic.claude-sonnet-4-6-20250929-v1:0"
    cfg = ProjectConfig(**data)
    assert cfg.agents["master"].model_id == "us.anthropic.claude-sonnet-4-6-20250929-v1:0"


def test_unknown_agent_name_rejected():
    data = _base()
    data["agents"]["typo"] = {"skills": [], "mcps": []}
    with pytest.raises(Exception, match="Unknown agent"):
        ProjectConfig(**data)


def test_master_disabled_rejected():
    data = _base()
    data["agents"]["master"]["enabled"] = False
    with pytest.raises(Exception, match="master.*cannot be disabled"):
        ProjectConfig(**data)


def test_eks_public_rejected():
    data = _base()
    data["agents"]["eks"]["network_mode"] = "PUBLIC"
    with pytest.raises(Exception, match="eks.*VPC"):
        ProjectConfig(**data)


def test_disabled_specialized_agent_allowed():
    data = _base()
    data["agents"]["discord_scanner"]["enabled"] = False
    cfg = ProjectConfig(**data)
    assert cfg.agents["discord_scanner"].enabled is False


def test_mcp_config_transport_validated():
    data = _base()
    data["agents"]["cloudwatch_logs"]["mcps"] = [
        {"name": "aws_docs", "transport": "streamable_http", "endpoint": "https://example", "auth": "none"}
    ]
    cfg = ProjectConfig(**data)
    assert cfg.agents["cloudwatch_logs"].mcps[0].transport == "streamable_http"


def test_mcp_invalid_transport_rejected():
    data = _base()
    data["agents"]["cloudwatch_logs"]["mcps"] = [
        {"name": "x", "transport": "websocket", "endpoint": "https://example"}
    ]
    with pytest.raises(Exception):
        ProjectConfig(**data)


def test_load_reads_repo_config_yaml():
    """Smoke test that load() finds the repo's actual config.yaml after Step 4 lands."""
    from shared.config import load
    cfg = load()
    assert cfg.project == "sre-on-call"
    assert "master" in cfg.agents


_SSM_YAML = """
project: from-ssm
environment: dev
defaults:
  model_id: anthropic.claude-haiku-4-5
  network_mode: PUBLIC
agents:
  master:
    skills: [investigate_alert]
    mcps: []
"""


def test_load_fetches_from_ssm_when_env_set(monkeypatch):
    """With CONFIG_SSM_PARAMETER set, load() reads the parameter, not the file."""
    import shared.config as config

    captured: dict[str, str] = {}

    def _fake_fetch(name: str) -> str:
        captured["name"] = name
        return _SSM_YAML

    monkeypatch.setattr(config, "_fetch_ssm_parameter", _fake_fetch)
    monkeypatch.setenv(config.CONFIG_SSM_ENV, "/sre-on-call/dev/config")

    cfg = config.load()
    assert cfg.project == "from-ssm"
    assert captured["name"] == "/sre-on-call/dev/config"


def test_load_falls_back_to_file_when_env_unset(monkeypatch):
    """Without CONFIG_SSM_PARAMETER, load() reads the repo file and never calls SSM."""
    import shared.config as config

    def _boom(name: str) -> str:
        raise AssertionError("SSM must not be consulted when the env var is unset")

    monkeypatch.setattr(config, "_fetch_ssm_parameter", _boom)
    monkeypatch.delenv(config.CONFIG_SSM_ENV, raising=False)

    cfg = config.load()
    assert cfg.project == "sre-on-call"


def test_fetch_ssm_parameter_reads_value(monkeypatch):
    """_fetch_ssm_parameter returns the parameter's Value via a boto3 ssm client."""
    import shared.config as config

    class _FakeClient:
        def get_parameter(self, Name: str) -> dict:
            assert Name == "/sre-on-call/dev/config"
            return {"Parameter": {"Value": _SSM_YAML}}

    class _FakeBoto3:
        def client(self, service: str, **kwargs):
            assert service == "ssm"
            return _FakeClient()

    monkeypatch.setitem(__import__("sys").modules, "boto3", _FakeBoto3())
    assert "from-ssm" in config._fetch_ssm_parameter("/sre-on-call/dev/config")
