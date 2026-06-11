"""Tests for the CloudWatch Logs Insights deep-link builder.

The expected URLs are anchored to AWS's console fragment format (the
``#logsV2:logs-insights$3FqueryDetail$3D~(...)`` shape produced by the real
Logs Insights "share"/"copy link" affordance), not merely to our own
implementation — getting the escaping exactly right is the whole point.
"""

from shared.deep_links import cloudwatch_logs_insights_url

# 2024-01-15T12:00:00Z .. 2024-01-15T12:30:00Z
_START = 1705320000
_END = 1705321800


def test_single_group_full_url():
    url = cloudwatch_logs_insights_url(
        region="us-east-1",
        log_groups=["/aws/lambda/my-func"],
        query="fields @timestamp, @message\n| sort @timestamp desc",
        start_epoch=_START,
        end_epoch=_END,
    )
    assert url == (
        "https://us-east-1.console.aws.amazon.com/cloudwatch/home"
        "?region=us-east-1#logsV2:logs-insights$3FqueryDetail$3D"
        "~(end~'2024-01-15T12*3a30*3a00.000Z"
        "~start~'2024-01-15T12*3a00*3a00.000Z"
        "~timeType~'ABSOLUTE~tz~'UTC"
        "~editorString~'fields*20*40timestamp*2c*20*40message"
        "*0a*7c*20sort*20*40timestamp*20desc"
        "~source~(~'*2faws*2flambda*2fmy-func))"
    )


def test_multiple_groups_source_list():
    url = cloudwatch_logs_insights_url(
        region="eu-west-2",
        log_groups=["/aws/lambda/a", "/aws/lambda/b"],
        query="fields @message",
        start_epoch=_START,
        end_epoch=_END,
    )
    # Region appears in both host and query-string.
    assert url.startswith(
        "https://eu-west-2.console.aws.amazon.com/cloudwatch/home?region=eu-west-2#"
    )
    # Each group is a `~'`-prefixed, escaped entry inside the source tuple.
    assert url.endswith("~source~(~'*2faws*2flambda*2fa~'*2faws*2flambda*2fb))")


def test_value_escaping_rules():
    # A query packed with the characters whose escaping matters.
    url = cloudwatch_logs_insights_url(
        region="us-east-1",
        log_groups=["g"],
        query="a b:c,d|e/f@h(i)'j*k",
        start_epoch=_START,
        end_epoch=_END,
    )
    # Unreserved [A-Za-z0-9-._] stay literal; everything else -> *<2-hex-lower>.
    assert "~editorString~'a*20b*3ac*2cd*7ce*2ff*40h*28i*29*27j*2ak~source~" in url


def test_query_separators_use_dollar_escapes():
    url = cloudwatch_logs_insights_url(
        region="us-east-1",
        log_groups=["g"],
        query="x",
        start_epoch=_START,
        end_epoch=_END,
    )
    # The `?`/`=` of `?queryDetail=` are AWS `$`-escaped, not percent-encoded.
    assert "#logsV2:logs-insights$3FqueryDetail$3D~(" in url
    assert "queryDetail=" not in url
    assert "%3F" not in url and "%3D" not in url


def test_timestamps_are_utc_iso_with_millis():
    url = cloudwatch_logs_insights_url(
        region="us-east-1",
        log_groups=["g"],
        query="x",
        start_epoch=_START,
        end_epoch=_END,
    )
    assert "(end~'2024-01-15T12*3a30*3a00.000Z" in url
    assert "~start~'2024-01-15T12*3a00*3a00.000Z" in url
