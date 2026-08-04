import asyncio

import aiohttp
from fastapi import HTTPException

from .parser import HuggingFaceURI


async def resolve_huggingface(
    uri: HuggingFaceURI,
    source_uri: str,
    host_url: str,
    host_api_key: str,
    file_filters: list[str] | None = None,
) -> str:
    """
    Resolves a huggingface:// URI by telling the Solar Host to pull it.
    Returns the resolved local:// path.

    ``file_filters`` restricts the snapshot to matching files so a multi-quant
    repository does not download in full.
    """
    url = f"{host_url.rstrip('/')}/models/pull"
    headers = {"X-API-Key": host_api_key, "Content-Type": "application/json"}
    payload: dict = {
        "source": "huggingface",
        "model_id": uri.model_id,
        "source_uri": source_uri,
    }
    if file_filters:
        payload["file_filters"] = file_filters

    try:
        # Long timeout for model pull as it might involve downloading GBs
        async with (
            aiohttp.ClientSession() as session,
            session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as response,
        ):
            if response.status == 200:
                data = await response.json()
                path = data.get("path")
                if not path:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Host '{host_url}' returned success but no path for model pull.",
                    )
                return f"local://{path}"

            # Propagate specific error codes, wrap others in 502
            PROPAGATED_CODES = {404, 507}
            try:
                err = await response.json()
                detail = err.get("detail") or err.get("error") or await response.text()
            except Exception:  # noqa: BLE001
                detail = await response.text()

            out_code = response.status if response.status in PROPAGATED_CODES else 502
            raise HTTPException(
                status_code=out_code,
                detail=f"Model pull failed on host '{host_url}' [{response.status}]: {detail}",
            )
    except HTTPException:
        raise
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ) as e:
        raise HTTPException(
            status_code=502,
            detail=f"Host '{host_url}' is unreachable during model pull: {e}",
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"Unexpected error during model pull on host: {e}"
        )
