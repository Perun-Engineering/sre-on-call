"""A2A JSON-RPC 2.0 envelope builders and response parsers.

Both the orchestrator (master → specialized agent) and the Lambda intake
(Lambda → master) must produce A2A ``message/send`` envelopes; the
orchestrator must also parse the response shapes Strands' A2AServer can
emit. Keeping both sides in one module prevents the request/response
schemas from drifting independently.
"""

from __future__ import annotations

import uuid
from typing import Any


def build_a2a_request(text: str, request_id: str) -> dict[str, Any]:
    """Build an A2A JSON-RPC 2.0 ``message/send`` request.

    ``text`` becomes the single text part of the user message. ``request_id``
    sets the JSON-RPC ``id`` so callers can correlate logs; the inner
    ``messageId`` is always a fresh UUID.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
                "messageId": str(uuid.uuid4()),
            }
        },
    }


def _iter_parts(result_data: dict) -> Any:
    """Yield every ``parts`` list reachable in an A2A ``result``.

    Covers all three reply shapes (inline ``parts``, wrapped ``message.parts``,
    and every artifact's ``parts``) so a structured payload is found wherever
    the server placed it.
    """
    inline = result_data.get("parts")
    if isinstance(inline, list):
        yield inline
    message = result_data.get("message")
    if isinstance(message, dict) and isinstance(message.get("parts"), list):
        yield message["parts"]
    artifacts = result_data.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, dict) and isinstance(artifact.get("parts"), list):
                yield artifact["parts"]


def extract_response_data(result_data: dict) -> dict[str, dict]:
    """Collect SRE structured DataPart payloads from an A2A ``result``.

    Scans every parts list (inline, wrapped message, and all artifacts) for
    ``{"kind": "data", "data": {"kind": <KIND>, "payload": {...}}}`` envelopes
    — the structured-transport counterpart to the text footers parsed by
    :meth:`shared.agent_footer.AgentFooter.extract`. Returns ``{KIND: payload}``;
    malformed or non-SRE data parts are skipped. On duplicate kinds the last
    occurrence wins, matching the "appended footer is authoritative" rule.
    """
    collected: dict[str, dict] = {}
    for parts in _iter_parts(result_data):
        for part in parts:
            if not isinstance(part, dict) or part.get("kind") != "data":
                continue
            envelope = part.get("data")
            if not isinstance(envelope, dict):
                continue
            kind = envelope.get("kind")
            payload = envelope.get("payload")
            if isinstance(kind, str) and isinstance(payload, dict):
                collected[kind] = payload
    return collected


def _extract_text_parts(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(
        p.get("text", "") for p in parts
        if isinstance(p, dict) and p.get("kind") == "text"
    )


def extract_response_text(result_data: dict) -> str:
    """Extract the canonical agent reply from an A2A JSON-RPC ``result``.

    Strands' A2AServer can return any of three shapes depending on whether
    the agent ran a tool, streamed, or replied inline:

    * **Inline Message** — ``result`` itself has ``parts``.
    * **Wrapped Message** — ``result.message.parts``.
    * **Task** — canonical reply lives in ``result.artifacts[*].parts``.
      Strands names the final-reply artifact ``agent_response``;
      ``history[]`` holds streaming chunks and must NOT be concatenated
      (it duplicates output and mid-token splits).

    Returns an empty string when no text can be extracted — callers should
    surface that as a degraded result rather than dumping the raw envelope.
    """
    inline_text = _extract_text_parts(result_data.get("parts"))
    if inline_text:
        return inline_text

    message = result_data.get("message")
    if isinstance(message, dict):
        wrapped_text = _extract_text_parts(message.get("parts"))
        if wrapped_text:
            return wrapped_text

    artifacts = result_data.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        named = next(
            (a for a in artifacts if isinstance(a, dict) and a.get("name") == "agent_response"),
            None,
        )
        chosen = named or artifacts[0]
        if isinstance(chosen, dict):
            return _extract_text_parts(chosen.get("parts"))

    return ""
