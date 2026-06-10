"""Marker-delimited footer for round-tripping a structured payload through
an agent's A2A text response.

Each footer is a literal-marker-bracketed JSON block appended to the end
of the agent's reply: ``<<<KIND { ... } KIND>>>``. The marker pair is the
discriminator (one per concept — ``AGENT_RESULT``, ``SNAPSHOT_RESULT``,
``AGENT_METADATA``); the body is the JSON form of a dataclass.

Permissive on read: malformed JSON, parser failures, and unknown fields
are silently dropped. The footer is transport telemetry, not load-bearing
data — an investigation that flows past a malformed footer continues
unharmed.

Each concrete footer instance lives next to the dataclass it carries
(``shared/tool_result.py`` for ``AGENT_RESULT`` and ``SNAPSHOT_RESULT``,
``shared/agent_telemetry.py`` for ``AGENT_METADATA``). This module exposes
only the generic :class:`AgentFooter` class — no instances — so there is
no import edge from the footer module back into the modules that use it.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Callable, Generic, TypeVar

T = TypeVar("T")

# Marker delimiters are exactly three angle brackets (``<<<KIND `` / `` KIND>>>``).
# Collapsing any run of 3+ angle brackets to 2 makes it impossible for untrusted
# text to *contain* a marker delimiter — so embedded content can neither forge a
# footer nor break the legitimate one. Runs are collapsed (not stripped) so the
# regex can't reconstruct a triple from adjacent pairs (e.g. ``<<<<`` → ``<<``).
_OPEN_DELIM_RE = re.compile(r"<{3,}")
_CLOSE_DELIM_RE = re.compile(r">{3,}")


def neutralize_markers(text: str) -> str:
    """Render *text* unable to contain a footer marker delimiter.

    Used on every untrusted span before it is placed into an agent's text
    response — both the human-readable body and the JSON footer payload — so
    that ingested content (Slack/Discord messages, log lines, k8s events)
    cannot spoof or suppress the structured ``AGENT_RESULT`` footer the master
    orchestrator parses.
    """
    text = _OPEN_DELIM_RE.sub("<<", text)
    return _CLOSE_DELIM_RE.sub(">>", text)


class AgentFooter(Generic[T]):
    """Marker-delimited footer for a single structured payload type.

    Construct one instance per ``kind`` next to the dataclass it carries::

        AGENT_RESULT: AgentFooter[AgentResult] = AgentFooter(
            "AGENT_RESULT", parse=_agent_result_from_dict,
        )

    The parser callable owns dataclass reconstruction (field whitelisting,
    nested dataclass nesting, type coercion). It may raise freely on bad
    input — :meth:`extract` swallows every exception and returns ``None``
    in the second tuple slot, preserving the silent-drop contract.
    """

    def __init__(self, kind: str, *, parse: Callable[[dict], T]) -> None:
        self._kind = kind
        self._prefix = f"<<<{kind} "
        self._suffix = f" {kind}>>>"
        self._parse = parse
        self._re = re.compile(
            re.escape(self._prefix) + r"(.*?)" + re.escape(self._suffix),
            re.DOTALL,
        )

    @property
    def prefix(self) -> str:
        """The literal opening marker (e.g. ``<<<AGENT_RESULT ``)."""
        return self._prefix

    @property
    def suffix(self) -> str:
        """The literal closing marker (e.g. `` AGENT_RESULT>>>``)."""
        return self._suffix

    @property
    def kind(self) -> str:
        """The discriminator string (e.g. ``"AGENT_RESULT"``)."""
        return self._kind

    def encode(self, payload: T) -> str:
        """Serialise *payload* (a dataclass instance) into a footer block.

        Calls :func:`dataclasses.asdict` internally — *payload* must be a
        dataclass. Compact JSON separators keep the footer one-liner-friendly.
        """
        body = json.dumps(asdict(payload), separators=(",", ":"))  # type: ignore[arg-type]
        # Neutralise any marker delimiter carried in string values (untrusted
        # finding content) so it cannot prematurely close this footer's body.
        body = neutralize_markers(body)
        return f"{self._prefix}{body}{self._suffix}"

    def extract(self, text: str) -> tuple[str, T | None]:
        """Strip and decode the footer if present.

        Returns ``(cleaned_text, payload)``. When no footer is found,
        returns ``(text, None)`` unchanged. When a footer is found but
        the JSON is malformed or the parser raises, returns
        ``(cleaned_text, None)`` — the footer is still stripped so the
        caller doesn't redisplay the raw marker block.

        ``cleaned_text`` is the input with the matched footer block
        removed and surrounding whitespace trimmed.
        """
        # Take the LAST matching footer: the legitimate footer is always
        # appended at the very end of the response, so any earlier marker pair
        # is untrusted preamble (a spoof) and must not win.
        matches = list(self._re.finditer(text))
        if not matches:
            return text, None
        match = matches[-1]
        cleaned = self._re.sub("", text).strip()
        try:
            payload_dict = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            return cleaned, None
        try:
            return cleaned, self._parse(payload_dict)
        except Exception:  # noqa: BLE001 — parser may raise anything; silent-drop contract
            return cleaned, None

    def find(self, text: str) -> str | None:
        """Return the raw footer block (markers included) if present, else ``None``.

        Used by callers that need to forward a footer verbatim — e.g. the
        A2A executor that re-emits the latest tool-output footer as part
        of the streamed response.
        """
        match = self._re.search(text)
        return match.group(0) if match is not None else None
