"""Shared constants for sre-on-call."""

# Master Agent posts the initial Incident Report after this many seconds.
INITIAL_DEADLINE_SECONDS: int = 60

# Master Agent terminates the investigation after this many seconds.
HARD_CUTOFF_SECONDS: int = 300

# Maximum number of Slack channels the Slack Scanner Agent will scan.
MAX_CHANNELS: int = 10

# Investigation window size in minutes (±5 minutes centered on alert timestamp).
INVESTIGATION_WINDOW_MINUTES: int = 10

# Experiment results expire after this many seconds (30 days).
EXPERIMENT_RESULTS_TTL_SECONDS: int = 30 * 86400
