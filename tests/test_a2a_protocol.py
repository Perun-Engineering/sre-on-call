"""Tests for :mod:`shared.a2a_protocol` envelope parsers."""

from __future__ import annotations

from shared.a2a_protocol import extract_response_data


def _data_part(kind: str, payload: dict) -> dict:
    return {"kind": "data", "data": {"kind": kind, "payload": payload}}


# ---------------------------------------------------------------------------
# extract_response_data — collect SRE DataPart payloads keyed by kind
# ---------------------------------------------------------------------------


class TestExtractResponseData:
    def test_collects_from_artifact_parts(self):
        result = {
            "artifacts": [
                {
                    "name": "agent_data",
                    "parts": [
                        _data_part("AGENT_RESULT", {"agent_name": "eks"}),
                        _data_part("AGENT_METADATA", {"model_id": "haiku"}),
                    ],
                }
            ]
        }
        assert extract_response_data(result) == {
            "AGENT_RESULT": {"agent_name": "eks"},
            "AGENT_METADATA": {"model_id": "haiku"},
        }

    def test_collects_from_inline_parts(self):
        result = {"parts": [_data_part("AGENT_RESULT", {"x": 1})]}
        assert extract_response_data(result) == {"AGENT_RESULT": {"x": 1}}

    def test_collects_from_wrapped_message_parts(self):
        result = {"message": {"parts": [_data_part("SNAPSHOT_RESULT", {"x": 2})]}}
        assert extract_response_data(result) == {"SNAPSHOT_RESULT": {"x": 2}}

    def test_scans_across_multiple_artifacts(self):
        result = {
            "artifacts": [
                {"name": "agent_response", "parts": [{"kind": "text", "text": "hi"}]},
                {"name": "agent_data", "parts": [_data_part("AGENT_RESULT", {"x": 3})]},
            ]
        }
        assert extract_response_data(result) == {"AGENT_RESULT": {"x": 3}}

    def test_ignores_text_and_malformed_parts(self):
        result = {
            "parts": [
                {"kind": "text", "text": "ignore me"},
                {"kind": "data", "data": "not-an-envelope"},
                {"kind": "data", "data": {"payload": {"x": 1}}},  # no kind
                {"kind": "data", "data": {"kind": "AGENT_RESULT"}},  # no payload
            ]
        }
        assert extract_response_data(result) == {}

    def test_empty_when_no_data_parts(self):
        assert extract_response_data({"artifacts": []}) == {}
        assert extract_response_data({}) == {}
