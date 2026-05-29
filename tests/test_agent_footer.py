"""Tests for :mod:`shared.agent_footer`."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from shared.agent_footer import AgentFooter


@dataclass
class _Sample:
    name: str
    value: int


def _parse_sample(payload: dict) -> _Sample:
    """Permissive parser — coerces types but raises on missing required keys."""
    return _Sample(name=str(payload["name"]), value=int(payload["value"]))


@pytest.fixture
def footer() -> AgentFooter[_Sample]:
    return AgentFooter[_Sample]("SAMPLE", parse=_parse_sample)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_encode_extract_round_trip(self, footer):
        original = _Sample(name="hello", value=42)
        text = "preamble line\n\n" + footer.encode(original)
        clean, recovered = footer.extract(text)
        assert recovered == original
        assert clean == "preamble line"

    def test_encode_uses_compact_json_separators(self, footer):
        encoded = footer.encode(_Sample(name="x", value=1))
        assert encoded.startswith("<<<SAMPLE ")
        assert encoded.endswith(" SAMPLE>>>")
        # Compact JSON: no whitespace after `,` or `:` separators
        body = encoded[len("<<<SAMPLE "):-len(" SAMPLE>>>")]
        assert ", " not in body
        assert ": " not in body


# ---------------------------------------------------------------------------
# Extract semantics
# ---------------------------------------------------------------------------


class TestExtract:
    def test_no_footer_returns_text_unchanged_with_none(self, footer):
        clean, recovered = footer.extract("a plain reply")
        assert clean == "a plain reply"
        assert recovered is None

    def test_malformed_json_drops_silently_strips_footer(self, footer):
        text = "preamble\n\n<<<SAMPLE not-json SAMPLE>>>"
        clean, recovered = footer.extract(text)
        assert recovered is None
        assert clean == "preamble"

    def test_parser_keyerror_drops_silently(self, footer):
        # JSON is valid but missing required field — _parse_sample raises KeyError
        text = 'ok\n\n<<<SAMPLE {"name":"x"} SAMPLE>>>'
        clean, recovered = footer.extract(text)
        assert recovered is None
        assert clean == "ok"

    def test_parser_typeerror_drops_silently(self):
        def picky_parser(payload: dict) -> _Sample:
            # Force a TypeError on bad value type
            return _Sample(name=payload["name"], value=payload["value"] + 1)

        f = AgentFooter[_Sample]("SAMPLE", parse=picky_parser)
        text = 'ok\n\n<<<SAMPLE {"name":"x","value":"not-an-int"} SAMPLE>>>'
        clean, recovered = f.extract(text)
        assert recovered is None
        assert clean == "ok"

    def test_other_kind_marker_is_ignored(self, footer):
        text = 'noise\n\n<<<OTHER {"name":"x","value":1} OTHER>>>'
        clean, recovered = footer.extract(text)
        assert recovered is None
        assert clean == text  # untouched — no SAMPLE marker present

    def test_two_kinds_in_same_text_extract_independently(self):
        a = AgentFooter[_Sample]("ALPHA", parse=_parse_sample)
        b = AgentFooter[_Sample]("BETA", parse=_parse_sample)
        text = (
            "body\n\n"
            + a.encode(_Sample(name="a", value=1))
            + "\n"
            + b.encode(_Sample(name="b", value=2))
        )

        clean_after_a, found_a = a.extract(text)
        assert found_a == _Sample(name="a", value=1)
        # ALPHA gone, BETA still present
        assert a.prefix not in clean_after_a
        assert b.prefix in clean_after_a

        clean_after_b, found_b = b.extract(clean_after_a)
        assert found_b == _Sample(name="b", value=2)
        assert b.prefix not in clean_after_b

    def test_extract_strips_trailing_whitespace(self, footer):
        text = "body\n\n" + footer.encode(_Sample(name="x", value=1)) + "\n  \n"
        clean, _ = footer.extract(text)
        assert clean == "body"


# ---------------------------------------------------------------------------
# find()
# ---------------------------------------------------------------------------


class TestFind:
    def test_returns_raw_block_with_markers(self, footer):
        text = "noise\n\n" + footer.encode(_Sample(name="x", value=9))
        block = footer.find(text)
        assert block is not None
        assert block.startswith("<<<SAMPLE ")
        assert block.endswith(" SAMPLE>>>")

    def test_returns_none_when_absent(self, footer):
        assert footer.find("plain text, no footer here") is None

    def test_other_kind_returns_none(self, footer):
        text = '<<<OTHER {"name":"x","value":1} OTHER>>>'
        assert footer.find(text) is None


# ---------------------------------------------------------------------------
# Markers and discriminator
# ---------------------------------------------------------------------------


class TestMarkers:
    def test_prefix_format(self, footer):
        assert footer.prefix == "<<<SAMPLE "

    def test_suffix_format(self, footer):
        assert footer.suffix == " SAMPLE>>>"

    def test_kind_exposes_discriminator(self, footer):
        assert footer.kind == "SAMPLE"
