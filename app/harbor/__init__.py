"""Harbor API client — thin wrapper providing app-level singleton lifecycle.

All types, exceptions, and media_types are re-exported from harbor_oci_client.
"""

from harbor_oci_client import (
    ArtifactDetail,
    ArtifactInfo,
    ArtifactNotFoundError,
    HarborAPIError,
    HarborAuthError,
    HarborClient,
    HarborConnectionError,
    HarborError,
    media_types,
)

__all__ = [
    "HarborClient",
    "ArtifactInfo",
    "ArtifactDetail",
    "HarborError",
    "HarborConnectionError",
    "HarborAuthError",
    "ArtifactNotFoundError",
    "HarborAPIError",
    "media_types",
    "harbor_client",
    "init_harbor",
    "close_harbor",
]

_client: HarborClient | None = None


def harbor_client() -> HarborClient:
    if _client is None:
        raise RuntimeError("Harbor client not initialized. Call init_harbor() first.")
    return _client


async def init_harbor(url: str, username: str, password: str) -> HarborClient:
    global _client
    _client = HarborClient(base_url=url, username=username, password=password)
    return _client


async def close_harbor() -> None:
    global _client
    if _client:
        await _client.close()
        _client = None
