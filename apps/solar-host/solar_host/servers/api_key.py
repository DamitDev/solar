"""API-key precedence helpers for spawned backend servers.

Torch-free so tests can exercise them without the huggingface extra
(IT Sec #97: the key reaches the process via an env var, with --api-key
as an optional override).
"""


def resolve_api_key(cli_api_key: str | None, env_api_key: str | None) -> str:
    """Precedence for the spawned server's key: CLI flag beats env var."""
    return cli_api_key or env_api_key or ""
