"""Property-based tests for CloudWatch Logs Agent tools."""

from __future__ import annotations

import boto3
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from agents.cloudwatch_logs.tools import _get_existing_log_groups

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Log group names follow the pattern /aws/... or similar path-like strings.
# We generate simple path-like names to keep things realistic.
_log_group_name = st.from_regex(
    r"/[a-z]{1,8}/[a-z]{1,8}/[a-z0-9\-]{1,12}", fullmatch=True
)

# Strategy for a pair of (existing, non_existing) log group name lists.
# We generate two disjoint sets of log group names.
_log_group_pair = st.tuples(
    st.lists(_log_group_name, min_size=0, max_size=8, unique=True),
    st.lists(_log_group_name, min_size=0, max_size=8, unique=True),
).filter(
    # Ensure the two lists are disjoint so we have a clear existing vs missing split
    lambda pair: not set(pair[0]) & set(pair[1])
)


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(data=_log_group_pair)
def test_non_existent_log_groups_skipped_without_failure(
    data: tuple[list[str], list[str]],
) -> None:
    """
    For any list of log group names where a subset does not exist:
    1. ``_get_existing_log_groups`` SHALL return the existing groups in the
       first element and the missing groups in the second element.
    2. The returned existing set SHALL match exactly the groups that were
       created in CloudWatch Logs.
    3. The returned missing set SHALL match exactly the groups that were NOT
       created in CloudWatch Logs.
    4. No exceptions SHALL be raised.
    """
    existing_names, non_existing_names = data

    with mock_aws():
        client = boto3.client("logs", region_name="us-east-1")

        # Create only the "existing" log groups in the mock
        for name in existing_names:
            client.create_log_group(logGroupName=name)

        # Combine both lists into the input (interleaved order doesn't matter,
        # but mixing them exercises the function more realistically)
        all_names = existing_names + non_existing_names

        # Call the function under test — no exception should be raised
        returned_existing, returned_missing = _get_existing_log_groups(
            client, all_names
        )

        # The returned existing set must match exactly the created groups
        assert set(returned_existing) == set(existing_names)

        # The returned missing set must match exactly the non-created groups
        assert set(returned_missing) == set(non_existing_names)

        # Every input name must appear in exactly one of the two returned lists
        assert set(returned_existing) | set(returned_missing) == set(all_names)
        assert set(returned_existing) & set(returned_missing) == set()
