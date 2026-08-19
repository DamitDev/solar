"""Cursor-facing model alias table for the /cursor/v1 proxy endpoint.

Cursor cannot control DeepSeek reasoning effort on custom-endpoint models,
so the proxy exposes one model name per effort level. Every alias maps to
the single upstream model (``deepseek-v4-flash:284b``) served through
solar-control's own /v1 gateway; the alias only selects the
``reasoning_effort`` injected into the upstream request.

The ``deepseek-v4-flash:*`` names are recognizable; the ``krumpli:*`` pair
exists because Cursor hardcodes a 200K context window for known DeepSeek
model names but falls back to its 1M default for unrecognized ones. Both
pairs are advertised side by side so users can pick whichever Cursor
treats correctly.
"""

UPSTREAM_MODEL = "deepseek-v4-flash:284b"

# alias -> reasoning_effort (EFFORT_ALIASES in transform.py canonicalizes
# to the upstream-valid values "high" and "max").
CURSOR_ALIASES: dict[str, str] = {
    "deepseek-v4-flash:max": "max",
    "deepseek-v4-flash:high": "high",
    "krumpli:max": "max",
    "krumpli:high": "high",
}

# Advertised context window is the upstream model's max_model_len (1M on
# the served instance). Kept as a fallback for /cursor/v1/models when the
# registry is unreachable.
DEFAULT_MAX_MODEL_LEN = 1_048_576


def is_cursor_alias(model: str) -> bool:
    """True when the model name is one of the exposed /cursor aliases."""
    return model in CURSOR_ALIASES


def reasoning_effort_for(model: str, default: str = "max") -> str:
    """The effort level selected by an alias, or the default for unknown names."""
    return CURSOR_ALIASES.get(model, default)


def alias_model_entries(max_model_len: int | None = None) -> list[dict]:
    """OpenAI-shaped ``data`` entries advertising the four aliases."""
    context = max_model_len or DEFAULT_MAX_MODEL_LEN
    created = 1_787_143_953  # stable placeholder; kept constant across calls
    return [
        {
            "id": alias,
            "object": "model",
            "created": created,
            "owned_by": "solar-cursor",
            "root": UPSTREAM_MODEL,
            "parent": None,
            "max_model_len": context,
        }
        for alias in CURSOR_ALIASES
    ]
