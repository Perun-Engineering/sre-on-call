"""Deep-link builders into the data sources a finding came from.

These produce console URLs that land an on-call engineer on the exact query
and time window that produced a finding. They are pure string builders — no
I/O, no SDK calls — so they are cheap to call from an agent's hot path and
reusable by the interactive incident-page renderer (#32/#33).

CloudWatch console links use AWS's own fragment-escaping scheme, *not*
percent-encoding: the console parses ``location.hash`` client-side and the
browser never decodes the hash, so a percent-encoded ``%28`` would reach the
parser literally and fail. The structural tokens ``~ ( ) ' $`` are therefore
emitted verbatim, and characters inside each string value are escaped as
``*`` followed by two lowercase hex digits of the UTF-8 byte.
"""

from __future__ import annotations

from datetime import datetime, timezone

# Characters left untouched inside a console fragment string value. Everything
# else is escaped as ``*<2-hex-lower>`` per UTF-8 byte.
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._"
)


def _escape_value(value: str) -> str:
    """Escape a string for use as a ``'``-prefixed console fragment value."""
    out: list[str] = []
    for byte in value.encode("utf-8"):
        char = chr(byte)
        if char in _UNRESERVED:
            out.append(char)
        else:
            out.append(f"*{byte:02x}")
    return "".join(out)


def _iso_millis_utc(epoch_seconds: int) -> str:
    """Render epoch seconds as ``YYYY-MM-DDTHH:MM:SS.000Z`` (UTC)."""
    dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def cloudwatch_logs_insights_url(
    region: str,
    log_groups: list[str],
    query: str,
    start_epoch: int,
    end_epoch: int,
) -> str:
    """Build a CloudWatch Logs Insights console deep link.

    The link opens the Logs Insights editor pre-filled with *query*, the
    *log_groups* selected as the source, and an absolute time range of
    ``[start_epoch, end_epoch]`` in UTC.

    Args:
        region: AWS region (e.g. ``"us-east-1"``) — used in both the console
            host and the ``region`` query-string parameter.
        log_groups: Log group names selected as the query source.
        query: The Logs Insights query string (editor contents).
        start_epoch: Window start, epoch seconds.
        end_epoch: Window end, epoch seconds.
    """
    sources = "".join(f"~'{_escape_value(name)}" for name in log_groups)
    query_detail = (
        f"~(end~'{_escape_value(_iso_millis_utc(end_epoch))}"
        f"~start~'{_escape_value(_iso_millis_utc(start_epoch))}"
        f"~timeType~'ABSOLUTE~tz~'UTC"
        f"~editorString~'{_escape_value(query)}"
        f"~source~({sources}))"
    )
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#logsV2:logs-insights$3FqueryDetail$3D{query_detail}"
    )
