"""Minimal Prometheus exposition-format parser.

The inference backends (llama.cpp, SGLang) expose their usage counters as
plain text in the Prometheus exposition format. A full client library would
be overkill for the handful of named metrics solar-host reads every couple
of seconds, so this module parses just the ``name{labels} value`` lines and
ignores ``# HELP`` / ``# TYPE`` comments.

Metric names may contain colons (llama.cpp and SGLang both export
``<backend>:<name>`` series). When the same name appears on several lines
with different label sets, the last value wins — for the single-series
counters solar-host reads, label sets do not vary.
"""

import re

_LABELS_RE = re.compile(r"\{.*\}")


def parse_prometheus(text: str) -> dict[str, float]:
    """Parse *text* into a ``{metric_name: value}`` mapping.

    Returns an empty dict for text without any value lines (e.g. a not-yet
    populated endpoint or a comment-only body).
    """
    values: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = _LABELS_RE.sub("", parts[0])
        try:
            values[name] = float(parts[-1])
        except ValueError:
            # A non-numeric value (e.g. "NaN") is not a counter solar-host
            # can use; skip it rather than letting it poison the snapshot.
            continue
    return values
