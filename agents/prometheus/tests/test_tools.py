"""Unit tests for the Prometheus Agent.

Tests cover MCP client configuration, agent card loading (including
system_prompt), skill building, PromQL query formulation guidance via
system prompt, and the tools_factory escape hatch.

Requirements: 5.1, 5.2, 5.4, 5.5
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock, patch


from agents.prometheus import (
    _create_mcp_client,
    _get_mcp_endpoint,
    tools_factory,
    TOOLS,
)

_AGENT_CARD_PATH = pathlib.Path(__file__).resolve().parent.parent / "agent_card.json"


def _load_system_prompt() -> str:
    with open(_AGENT_CARD_PATH) as fh:
        return json.load(fh)["system_prompt"]


# ---------------------------------------------------------------------------
# _get_mcp_endpoint
# ---------------------------------------------------------------------------


class TestGetMcpEndpoint:
    """Tests for the _get_mcp_endpoint helper."""

    @patch.dict("os.environ", {"PROMETHEUS_MCP_ENDPOINT": "http://prom-mcp:8080/sse"})
    def test_returns_env_var_when_set(self):
        assert _get_mcp_endpoint() == "http://prom-mcp:8080/sse"

    @patch.dict("os.environ", {}, clear=True)
    def test_returns_default_when_env_var_missing(self):
        assert _get_mcp_endpoint() == "http://localhost:9090/sse"


# ---------------------------------------------------------------------------
# _create_mcp_client
# ---------------------------------------------------------------------------


class TestCreateMcpClient:
    """Tests for the _create_mcp_client helper."""

    @patch("agents.prometheus._get_mcp_endpoint", return_value="http://prom:9090/sse")
    @patch("agents.prometheus.MCPClient")
    def test_creates_mcp_client_with_sse_transport(self, mock_mcp_cls, mock_endpoint):
        """MCPClient is instantiated with a callable that produces an SSE client."""
        _create_mcp_client()

        mock_mcp_cls.assert_called_once()
        # The first argument should be a callable (lambda)
        transport_factory = mock_mcp_cls.call_args[0][0]
        assert callable(transport_factory)


# ---------------------------------------------------------------------------
# tools_factory / TOOLS
# ---------------------------------------------------------------------------


class TestToolsFactory:
    """Tests for the tools_factory escape hatch."""

    def test_tools_list_is_empty(self):
        """TOOLS is empty — real tools come from tools_factory at runtime."""
        assert TOOLS == []

    @patch("agents.prometheus._create_mcp_client")
    def test_tools_factory_returns_mcp_client(self, mock_create):
        mock_mcp = MagicMock()
        mock_create.return_value = mock_mcp

        result = tools_factory()

        assert result == [mock_mcp]
        mock_create.assert_called_once()


# ---------------------------------------------------------------------------
# load_agent_card
# ---------------------------------------------------------------------------


class TestLoadAgentCard:
    """Tests for the Prometheus agent card content."""

    def test_loads_valid_agent_card(self):
        with open(_AGENT_CARD_PATH) as fh:
            card = json.load(fh)

        assert card["name"] == "Prometheus Agent"
        assert "Prometheus" in card["description"]
        assert card["version"] == "0.1.0"
        assert card["url"] == "http://localhost:9000"

    def test_agent_card_has_system_prompt(self):
        with open(_AGENT_CARD_PATH) as fh:
            card = json.load(fh)

        assert "system_prompt" in card
        assert "PromQL" in card["system_prompt"]

    def test_agent_card_has_capabilities(self):
        with open(_AGENT_CARD_PATH) as fh:
            card = json.load(fh)

        assert "capabilities" in card
        assert card["capabilities"]["streaming"] is False

    def test_agent_card_has_io_modes(self):
        with open(_AGENT_CARD_PATH) as fh:
            card = json.load(fh)

        assert card["defaultInputModes"] == ["text/plain"]
        assert card["defaultOutputModes"] == ["text/plain"]


# ---------------------------------------------------------------------------
# System prompt content (now from agent_card.json)
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    """Tests for the system prompt content."""

    def test_prompt_mentions_promql(self):
        assert "PromQL" in _load_system_prompt()

    def test_prompt_instructs_query_formulation(self):
        prompt = _load_system_prompt()
        assert "alert context" in prompt.lower()
        assert "formulate" in prompt.lower()

    def test_prompt_instructs_error_handling(self):
        assert "unreachable" in _load_system_prompt().lower()

    def test_prompt_forbids_fabrication(self):
        assert "fabricate" in _load_system_prompt().lower()

    def test_prompt_mentions_common_metrics(self):
        prompt = _load_system_prompt()
        assert "http_requests_total" in prompt
        assert "cpu_usage" in prompt.lower()


