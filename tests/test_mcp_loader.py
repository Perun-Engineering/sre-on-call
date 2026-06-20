"""Tests for shared.mcp_loader."""
from __future__ import annotations
from unittest.mock import MagicMock, patch

import pytest

from shared.config import MCPConfig
from shared.mcp_loader import open as mcp_open


def test_open_with_no_configs_returns_empty_handle():
    with mcp_open([]) as conns:
        assert conns.tools == []


def test_open_streamable_http_connects_and_collects_tools():
    cfg = MCPConfig(name="aws_docs", transport="streamable_http", endpoint="https://example", auth="none")
    fake_tool = MagicMock(name="fake_tool")

    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=None)
    fake_client.list_tools_sync = MagicMock(return_value=[fake_tool])

    with patch("shared.mcp_loader.MCPClient", return_value=fake_client) as mock_class:
        with mcp_open([cfg]) as conns:
            assert conns.tools == [fake_tool]
        mock_class.assert_called_once()
    fake_client.__exit__.assert_called_once()


def test_open_unsupported_transport_raises():
    cfg = MCPConfig.model_construct(name="x", transport="ws", endpoint="https://x", auth="none")
    with pytest.raises(ValueError, match="transport"):
        with mcp_open([cfg]):
            pass


def test_open_failure_during_connect_unwinds_already_connected_clients():
    """If MCP #2 fails to connect, MCP #1 must still be cleanly closed."""
    cfg_ok = MCPConfig(name="ok", transport="streamable_http", endpoint="https://x1", auth="none")
    cfg_bad = MCPConfig(name="bad", transport="streamable_http", endpoint="https://x2", auth="none")

    ok_client = MagicMock()
    ok_client.__enter__ = MagicMock(return_value=ok_client)
    ok_client.__exit__ = MagicMock(return_value=None)
    ok_client.list_tools_sync = MagicMock(return_value=[])

    bad_client = MagicMock()
    bad_client.__enter__ = MagicMock(side_effect=RuntimeError("connect failed"))
    bad_client.__exit__ = MagicMock(return_value=None)

    with patch("shared.mcp_loader.MCPClient", side_effect=[ok_client, bad_client]):
        with pytest.raises(RuntimeError, match="connect failed"):
            mcp_open([cfg_ok, cfg_bad])

    ok_client.__exit__.assert_called_once()


# ── auth header support (rec #6 prereq) ──────────────────────────────────────


def _run_factory(captured: dict, transport_path: str, cfg: MCPConfig):
    """Build the client for ``cfg`` and invoke the transport factory once,
    capturing the kwargs the transport was called with."""

    def _spy(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return MagicMock()

    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=None)
    fake_client.list_tools_sync = MagicMock(return_value=[])

    def _capture_factory(factory):
        # MCPClient(transport_callable) — invoke the callable so the transport
        # constructor runs and we can assert on the headers it received.
        factory()
        return fake_client

    with patch(transport_path, side_effect=_spy):
        with patch("shared.mcp_loader.MCPClient", side_effect=_capture_factory):
            with mcp_open([cfg]):
                pass


def test_api_key_auth_passes_resolved_bearer_header_streamable_http():
    """An api_key:<env> auth resolves the token from Secrets Manager and passes
    it as an Authorization: Bearer header (+ Grafana SA header) to the transport."""
    cfg = MCPConfig(
        name="grafana",
        transport="streamable_http",
        endpoint="https://grafana.example/mcp",
        auth="api_key:GRAFANA_TOKEN",
    )
    captured: dict = {}

    with patch("shared.mcp_loader.resolve_secret", return_value="glsa-supersecret") as mock_resolve:
        _run_factory(captured, "mcp.client.streamable_http.streamablehttp_client", cfg)

    mock_resolve.assert_called_once_with("GRAFANA_TOKEN")
    headers = captured["kwargs"]["headers"]
    assert headers["Authorization"] == "Bearer glsa-supersecret"
    assert headers["X-Grafana-Service-Account-Token"] == "glsa-supersecret"
    # endpoint still passed (positionally or by kw)
    assert "https://grafana.example/mcp" in captured["args"] or captured["kwargs"].get("url") == "https://grafana.example/mcp"


def test_api_key_auth_passes_resolved_bearer_header_sse():
    cfg = MCPConfig(
        name="grafana",
        transport="sse",
        endpoint="https://grafana.example/sse",
        auth="api_key:GRAFANA_TOKEN",
    )
    captured: dict = {}

    with patch("shared.mcp_loader.resolve_secret", return_value="tok"):
        _run_factory(captured, "mcp.client.sse.sse_client", cfg)

    headers = captured["kwargs"]["headers"]
    assert headers["Authorization"] == "Bearer tok"


def test_auth_none_passes_no_headers():
    """The existing auth: none path must be unchanged — headers=None (the
    transport's default, byte-identical to omitting the kwarg)."""
    cfg = MCPConfig(name="aws_docs", transport="streamable_http", endpoint="https://x", auth="none")
    captured: dict = {}

    with patch("shared.mcp_loader.resolve_secret") as mock_resolve:
        _run_factory(captured, "mcp.client.streamable_http.streamablehttp_client", cfg)

    mock_resolve.assert_not_called()
    assert captured["kwargs"].get("headers") is None


def test_api_key_auth_fails_open_when_secret_empty():
    """If the token can't be resolved (empty), build the client without auth
    headers rather than aborting the whole connection set."""
    cfg = MCPConfig(
        name="grafana",
        transport="streamable_http",
        endpoint="https://x",
        auth="api_key:GRAFANA_TOKEN",
    )
    captured: dict = {}

    with patch("shared.mcp_loader.resolve_secret", return_value=""):
        _run_factory(captured, "mcp.client.streamable_http.streamablehttp_client", cfg)

    assert captured["kwargs"].get("headers") is None


def test_unsupported_auth_scheme_fails_open_no_headers():
    """An unrecognised auth scheme must not crash — fail open, no headers."""
    cfg = MCPConfig.model_construct(
        name="x", transport="streamable_http", endpoint="https://x", auth="oauth:whatever"
    )
    captured: dict = {}

    _run_factory(captured, "mcp.client.streamable_http.streamablehttp_client", cfg)

    assert captured["kwargs"].get("headers") is None
