"""Unit tests for page_renderer.handler — S3-triggered render I/O."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from page_renderer import handler


def _event(key: str, bucket: str = "traces") -> dict:
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}


def _model_bytes(chart_ids: list[str]) -> bytes:
    return json.dumps({
        "investigation_id": "inv-1", "severity": "🔴", "affected_services": "x",
        "time_of_detection": "t", "status": "completed", "summary": "s",
        "root_cause": "rc", "analysis": None, "evidence": [], "chart_ids": chart_ids,
        "alert_text": "a",
    }).encode()


def test_handler_reads_model_and_writes_page(monkeypatch):
    s3 = MagicMock()
    bodies = {
        "dt=2026-06-12/investigation_id=inv-1/page_model.json": _model_bytes(["c1"]),
        "dt=2026-06-12/investigation_id=inv-1/charts/c1.json": json.dumps(
            {"points": [{"t": 1, "v": 2}]}
        ).encode(),
    }

    def _get(Bucket, Key):
        return {"Body": MagicMock(read=MagicMock(return_value=bodies[Key]))}

    s3.get_object.side_effect = _get
    monkeypatch.setattr(handler, "_s3", lambda: s3)
    handler.lambda_handler(
        _event("dt=2026-06-12/investigation_id=inv-1/page_model.json"), None
    )
    put = s3.put_object.call_args.kwargs
    assert put["Key"] == "pages/inv-1.html"
    assert put["ContentType"] == "text/html; charset=utf-8"
    assert put["ServerSideEncryption"] == "AES256"
    assert b"<!DOCTYPE html>" in put["Body"]


def test_handler_is_fail_open_on_error(monkeypatch):
    s3 = MagicMock()
    s3.get_object.side_effect = RuntimeError("boom")
    monkeypatch.setattr(handler, "_s3", lambda: s3)
    handler.lambda_handler(_event("dt=x/investigation_id=inv/page_model.json"), None)  # must not raise


def test_handler_tolerates_missing_chart_file(monkeypatch):
    s3 = MagicMock()

    def _get(Bucket, Key):
        if Key.endswith("page_model.json"):
            return {"Body": MagicMock(read=MagicMock(return_value=_model_bytes(["missing"])))}
        raise RuntimeError("404")

    s3.get_object.side_effect = _get
    monkeypatch.setattr(handler, "_s3", lambda: s3)
    handler.lambda_handler(_event("dt=d/investigation_id=inv-1/page_model.json"), None)
    assert s3.put_object.called  # page still written without the missing chart
