"""The A2A round-trip seam — one request to one specialized agent.

Sits above :mod:`shared.a2a_protocol` (which stays a pure, dependency-free
envelope builder + text extractor). This module owns the *transport*
(``aiohttp`` for local URLs, Bedrock AgentCore for ``arn:`` runtimes) and
the single-call pipeline:

    build envelope -> POST -> surface JSON-RPC error -> extract reply text
    -> parse the one caller-supplied footer

:class:`A2AClient` knows the A2A wire format and nothing about
``AgentResult`` / ``SnapshotReport`` *semantics*. The caller picks which
:class:`shared.agent_footer.AgentFooter` to expect and maps the resulting
:class:`A2AReply` to its own domain type — the alert orchestrator to an
``AgentResult(status="error")``, the ``/sre-snapshot`` orchestrator to a raised
``RuntimeError``. A JSON-RPC ``error`` comes back as a *value* on the
reply; transport/network failures still raise out of :meth:`A2AClient.send`.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from shared.a2a_protocol import build_a2a_request, extract_response_text
from shared.agent_footer import AgentFooter
from shared.constants import HARD_CUTOFF_SECONDS

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Transport adapters (dependency injection / mocking seam)
# ---------------------------------------------------------------------------


class AsyncHTTPClient(Protocol):
    """Minimal async HTTP client interface for A2A calls."""

    async def post_json(self, url: str, payload: dict) -> dict:
        """POST *payload* as JSON to *url* and return the parsed response."""
        ...  # pragma: no cover


class AiohttpClient:
    """Default :class:`AsyncHTTPClient` backed by ``aiohttp``."""

    async def post_json(self, url: str, payload: dict) -> dict:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=HARD_CUTOFF_SECONDS),
            ) as resp:
                return await resp.json()


class AgentCoreClient:
    """:class:`AsyncHTTPClient` that invokes Bedrock AgentCore runtimes.

    ``url`` is interpreted as an Agent Runtime ARN. Use this client when
    the orchestrator's per-agent ``*_AGENT_RUNTIME_ARN`` env vars are set
    in deployed environments. Local-dev paths can still use
    :class:`AiohttpClient`.
    """

    def __init__(self, *, client: Any = None, region_name: str | None = None):
        if client is not None:
            self._client = client
        else:
            import boto3

            self._client = boto3.client(
                "bedrock-agentcore",
                region_name=region_name or os.environ.get("AWS_REGION", "us-east-1"),
            )

    async def post_json(self, url: str, payload: dict) -> dict:
        response = await asyncio.to_thread(
            self._client.invoke_agent_runtime,
            agentRuntimeArn=url,
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        body = response["response"]
        if hasattr(body, "read"):
            body = body.read()
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return json.loads(body)


class RoutingHTTPClient:
    """:class:`AsyncHTTPClient` that routes each call by endpoint shape.

    ``arn:`` endpoints go to :class:`AgentCoreClient`; everything else is
    treated as a URL and goes to :class:`AiohttpClient`. This lets a single
    client serve a mixed deployment (some agents on AgentCore, some on local
    URLs) correctly — the previous all-or-nothing ``any(arn)`` pick routed
    every agent through one transport. Sub-transports are built lazily, so no
    boto3 client is constructed unless an ``arn:`` endpoint is dispatched.
    """

    def __init__(
        self,
        *,
        aiohttp_client: AsyncHTTPClient | None = None,
        agentcore_client: AsyncHTTPClient | None = None,
    ) -> None:
        self._aiohttp = aiohttp_client
        self._agentcore = agentcore_client

    async def post_json(self, url: str, payload: dict) -> dict:
        if url.startswith("arn:"):
            if self._agentcore is None:
                self._agentcore = AgentCoreClient()
            return await self._agentcore.post_json(url, payload)
        if self._aiohttp is None:
            self._aiohttp = AiohttpClient()
        return await self._aiohttp.post_json(url, payload)


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


@dataclass
class A2AReply(Generic[T]):
    """The neutral result of one :meth:`A2AClient.send`.

    ``text`` is the agent's reply with the requested footer stripped (any
    *other* footers — e.g. ``AGENT_METADATA`` — are left in place for the
    caller to peel). ``payload`` is the parsed footer, or ``None`` when it
    is absent or malformed (footers are permissive on read). ``error`` is
    the JSON-RPC ``error.message`` when the agent returned a protocol-level
    error, else ``None``.
    """

    text: str
    payload: T | None
    error: str | None


class A2AClient:
    """Sends one A2A ``message/send`` to an agent and parses the response.

    Wraps an injected :class:`AsyncHTTPClient` transport — the fan-out
    selects the transport once (ARN vs URL) and constructs one client;
    :meth:`send` is called per agent.
    """

    def __init__(self, http_client: AsyncHTTPClient) -> None:
        self._http = http_client

    async def send(
        self,
        endpoint: str,
        text: str,
        *,
        footer: AgentFooter[T],
        request_id: str,
    ) -> A2AReply[T]:
        """Round-trip one request to *endpoint* and parse *footer* from the reply.

        Network/transport failures propagate. A JSON-RPC ``error`` response
        is returned as :attr:`A2AReply.error` (with ``text=""`` and
        ``payload=None``).
        """
        request = build_a2a_request(text=text, request_id=request_id)
        response = await self._http.post_json(endpoint, request)

        if "error" in response:
            message = response["error"].get("message", "Unknown A2A error")
            return A2AReply(text="", payload=None, error=message)

        raw = extract_response_text(response.get("result", {}))
        clean, payload = footer.extract(raw)
        return A2AReply(text=clean, payload=payload, error=None)
