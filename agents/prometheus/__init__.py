# Prometheus Agent — queries Prometheus metrics via MCP server
from __future__ import annotations

import os

from strands.tools.mcp import MCPClient

TOOLS = []  # Populated at runtime via tools_factory


def _get_mcp_endpoint() -> str:
    """Return the Prometheus MCP server endpoint from environment."""
    return os.environ.get("PROMETHEUS_MCP_ENDPOINT", "http://localhost:9090/sse")


def _create_mcp_client() -> MCPClient:
    """Create an MCPClient configured for the Prometheus MCP server."""
    from mcp.client.sse import sse_client

    return MCPClient(lambda: sse_client(_get_mcp_endpoint()))


def tools_factory() -> list:
    """Build the MCP-backed tool list at runtime."""
    return [_create_mcp_client()]
