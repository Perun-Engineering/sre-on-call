"""MCP loader — connects external MCP servers per config.yaml.

The loader holds a context-managed handle (``MCPConnections``) that
opens all clients on entry, harvests their tools, and closes all clients
on exit. ``shared.a2a_factory`` keeps the handle open for the agent's
lifetime — connections are torn down only when the process exits.

Failures during connect propagate (fail-fast). Already-connected clients
are unwound before the exception bubbles up so we never leak transports.
"""
from __future__ import annotations

import logging
from contextlib import AbstractContextManager, ExitStack
from typing import Iterable, cast

from strands.tools.mcp import MCPClient

from shared.config import MCPConfig
from shared.secrets import resolve_secret

logger = logging.getLogger(__name__)

# Auth scheme prefix in ``MCPConfig.auth``. ``api_key:<ENV_VAR>`` resolves the
# token via ``shared.secrets.resolve_secret`` (so the env var may carry a
# literal token for local dev or a Secrets Manager ARN in AWS) and sends it as
# an HTTP Authorization header into the streamable_http / sse transport.
_API_KEY_PREFIX = "api_key:"


class MCPConnections:
    def __init__(self) -> None:
        self._stack = ExitStack()
        self.tools: list = []

    def __enter__(self) -> "MCPConnections":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool | None:
        return self._stack.__exit__(exc_type, exc, tb)

    def connect(self, cfg: MCPConfig) -> None:
        client = self._build_client(cfg)
        client = self._stack.enter_context(cast(AbstractContextManager[MCPClient], client))
        tools = client.list_tools_sync()
        logger.info("Connected MCP %s — discovered %d tool(s)", cfg.name, len(tools))
        self.tools.extend(tools)

    def _build_client(self, cfg: MCPConfig) -> MCPClient:
        if cfg.transport == "streamable_http":
            from mcp.client.streamable_http import streamablehttp_client
            headers = self._auth_headers(cfg)
            return MCPClient(lambda: streamablehttp_client(cfg.endpoint, headers=headers))
        if cfg.transport == "sse":
            from mcp.client.sse import sse_client
            headers = self._auth_headers(cfg)
            return MCPClient(lambda: sse_client(cfg.endpoint, headers=headers))
        if cfg.transport == "stdio":
            from mcp.client.stdio import stdio_client, StdioServerParameters
            params = StdioServerParameters(command=cfg.endpoint)
            return MCPClient(lambda: stdio_client(params))
        raise ValueError(f"Unsupported MCP transport: {cfg.transport!r}")

    def _auth_headers(self, cfg: MCPConfig) -> dict[str, str] | None:
        """Resolve ``cfg.auth`` to HTTP auth headers for the HTTP transports.

        ``none`` (the default) yields ``None`` — the transport's no-header
        default, byte-identical to the pre-auth behaviour. ``api_key:<ENV_VAR>``
        resolves the token via :func:`shared.secrets.resolve_secret` (env may
        carry a literal token locally or a Secrets Manager ARN in AWS) and
        returns the Authorization/Grafana headers. Fail-open: an unresolved
        token or an unrecognised scheme logs a warning and returns ``None``
        rather than aborting the whole connection set.
        """
        auth = (cfg.auth or "none").strip()
        if auth in ("", "none", "iam"):
            # ``iam`` (SigV4) is not header-based; nothing to inject here.
            return None
        if auth.startswith(_API_KEY_PREFIX):
            env_var = auth[len(_API_KEY_PREFIX):].strip()
            token = resolve_secret(env_var) if env_var else ""
            if not token:
                logger.warning(
                    "MCP %s: api_key auth env %r resolved to no token — connecting without auth header",
                    cfg.name, env_var,
                )
                return None
            return {
                "Authorization": f"Bearer {token}",
                "X-Grafana-Service-Account-Token": token,
            }
        logger.warning(
            "MCP %s: unsupported auth scheme %r — connecting without auth header",
            cfg.name, auth,
        )
        return None


def open(mcp_configs: Iterable[MCPConfig]) -> MCPConnections:
    """Open all MCP connections, return a context-managed handle.

    On any connect failure, already-opened clients are unwound and the
    exception re-raised — caller never sees a half-open handle.
    """
    conns = MCPConnections()
    try:
        for cfg in mcp_configs:
            conns.connect(cfg)
    except Exception:
        conns._stack.close()
        raise
    return conns
