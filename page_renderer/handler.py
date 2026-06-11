"""S3-triggered Lambda: turn a written ``page_model.json`` into ``pages/<id>.html``.

Triggered by an S3 ``ObjectCreated`` notification on the ``page_model.json``
suffix. Thin I/O around the pure :func:`page_renderer.render.render_page`:
reads the model + each referenced ``charts/<id>.json`` from the investigation
prefix, composes the page, writes it to a flat ``pages/<investigation_id>.html``
key (stable URL, independent of the dated trace partition). Fail-open: any error
is logged and swallowed — the chat report was posted independently.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from urllib.parse import unquote_plus

from page_renderer.render import render_page

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_ASSET_PATH = os.path.join(os.path.dirname(__file__), "assets", "echarts.min.js")


@lru_cache(maxsize=1)
def _echarts_js() -> str:
    with open(_ASSET_PATH, encoding="utf-8") as fh:
        return fh.read()


def _s3():
    import boto3

    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _prefix_of(key: str) -> str:
    """``dt=…/investigation_id=…/page_model.json`` → ``dt=…/investigation_id=…/``."""
    return key.rsplit("page_model.json", 1)[0]


def lambda_handler(event: dict, _context) -> None:
    s3 = _s3()
    for record in event.get("Records", []):
        try:
            bucket = record["s3"]["bucket"]["name"]
            key = unquote_plus(record["s3"]["object"]["key"])
            if not key.endswith("page_model.json"):
                continue
            prefix = _prefix_of(key)
            model = json.loads(
                s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            )
            charts: dict[str, dict] = {}
            for chart_id in model.get("chart_ids", []):
                chart_key = f"{prefix}charts/{chart_id}.json"
                try:
                    charts[chart_id] = json.loads(
                        s3.get_object(Bucket=bucket, Key=chart_key)["Body"].read()
                    )
                except Exception:
                    logger.warning("page_renderer: chart %s missing, skipping", chart_id)
            html = render_page(model, charts, _echarts_js())
            inv_id = model.get("investigation_id", "unknown")
            s3.put_object(
                Bucket=bucket,
                Key=f"pages/{inv_id}.html",
                Body=html.encode("utf-8"),
                ContentType="text/html; charset=utf-8",
                # AES256 (SSE-S3) not the trace CMK, so CloudFront's OAC can read
                # the rendered page without any KMS grant. See modules/sre-on-call/pages.tf.
                ServerSideEncryption="AES256",
            )
            logger.info("page_renderer: wrote pages/%s.html", inv_id)
        except Exception:
            logger.exception("page_renderer: render failed for record; continuing")
