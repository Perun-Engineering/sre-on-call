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
