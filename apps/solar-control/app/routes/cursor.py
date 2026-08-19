"""Cursor proxy endpoints (/cursor/v1).

Authenticated like the OpenAI gateway (api_keys table via the auth
middleware) so the caller's key both gates access and attributes
telemetry through the self-HTTP /v1 hop. Only the four flash aliases are
served; any other model name is an unknown-model 404.

Mirrors the upstream deepseek-cursor-proxy surface: /models and
/chat/completions.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.cursor_proxy.aliases import is_cursor_alias, reasoning_effort_for
from app.cursor_proxy.config import proxy_config_from_settings
from app.cursor_proxy.service import (
    CursorProxyUpstreamError,
    get_models_data,
    proxy_non_stream,
    proxy_stream,
)

logger = logging.getLogger("app.cursor_proxy")
router = APIRouter(prefix="/cursor/v1", tags=["cursor"])


def _auth_header(request: Request) -> str:
    return (
        request.headers.get("Authorization") or request.headers.get("X-API-Key") or ""
    )


def _model_not_found(model: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "message": (
                f"The model '{model}' does not exist or you do not have "
                "access to it."
            ),
            "type": "invalid_request_error",
            "param": None,
            "code": "model_not_found",
        },
    )


@router.get("/models")
async def list_models(client: Request):
    config = proxy_config_from_settings()
    data = await get_models_data(config)
    return {"object": "list", "data": data}


@router.post("/chat/completions")
async def chat_completions(client: Request):
    config = proxy_config_from_settings()
    raw_body = await client.body()
    if len(raw_body) > config.max_request_body_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "message": "Request body too large",
                "type": "invalid_request_error",
                "param": None,
                "code": "request_body_too_large",
            },
        )
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Invalid JSON body",
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_json",
            },
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Request body must be a JSON object",
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_json",
            },
        )

    model = str(payload.get("model") or config.upstream_model)
    if not is_cursor_alias(model):
        raise _model_not_found(model)
    effort = reasoning_effort_for(model, config.reasoning_effort)

    authorization = _auth_header(client)
    stream = bool(payload.get("stream"))

    if stream:

        async def generator():
            try:
                async for chunk in proxy_stream(
                    payload,
                    config,
                    authorization,
                    original_model=model,
                    reasoning_effort=effort,
                ):
                    yield chunk
            except CursorProxyUpstreamError as exc:
                # Mirror the gateway's _safe_stream behaviour: emit the
                # upstream error as an SSE event so clients stay in stream
                # parsing mode.
                yield (b"data: " + exc.body + b"\n\n")
            except Exception as exc:
                logger.warning("cursor stream failed", exc_info=True)
                payload_bytes = json.dumps(
                    {"error": {"message": str(exc), "type": "server_error"}}
                ).encode("utf-8")
                yield b"data: " + payload_bytes + b"\n\n"

        return StreamingResponse(generator(), media_type="text/event-stream")

    try:
        status, content_type, body = await proxy_non_stream(
            payload,
            config,
            authorization,
            original_model=model,
            reasoning_effort=effort,
        )
    except CursorProxyUpstreamError as exc:
        return Response(
            content=exc.body,
            status_code=exc.status,
            media_type=exc.content_type,
        )
    return Response(content=body, status_code=status, media_type=content_type)
