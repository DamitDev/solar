"""OpenAI-compatible API gateway endpoints.

Each request is authenticated against the api_keys table; the
resolved endpoint is stored in request.state by the auth middleware and
passed through to the gateway for scoping and logging. A key belongs to
exactly one endpoint, whose model scoping restricts the registry.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.gateway import gateway
from app.models import (
    ChatCompletionRequest,
    ClassifyRequest,
    CompletionRequest,
    EmbeddingRequest,
    RerankRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["openai"])


def _get_endpoint_id(request: Request) -> str | None:
    return getattr(request.state, "endpoint_id", None)  # set by auth_middleware


def _model_patterns(request: Request) -> list[str] | None:
    """Registry restriction for the authenticated endpoint, or None.

    ``None`` is the unrestricted case: the management key (no endpoint) and
    an endpoint with ``serve_all_models``. A scoped endpoint returns its
    glob patterns verbatim, so an empty list correctly serves no models.
    """
    endpoint = getattr(request.state, "endpoint", None)
    if not endpoint:
        return None
    if endpoint.serve_all_models:
        return None
    return list(endpoint.model_patterns or [])


def _raise_model_not_found(model: str):
    """OpenAI-shaped 404 so a denied model does not leak registry existence."""
    raise HTTPException(
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


def _safe_stream(
    model: str,
    endpoint: str,
    request_data: dict,
    client_ip: str,
    endpoint_id,
    model_patterns: list[str] | None,
):
    """Wrap gateway.stream_request so client disconnects emit a proper error event."""

    async def generator():
        stream = gateway.stream_request(
            model,
            endpoint,
            request_data,
            client_ip,
            endpoint_id=endpoint_id,
            model_patterns=model_patterns,
        )
        try:
            async for chunk in stream:
                yield chunk
        except Exception as e:  # noqa: BLE001
            payload = json.dumps({"error": str(e)})
            yield f"data: {payload}\n\n".encode()
        finally:
            await stream.aclose()

    return StreamingResponse(generator(), media_type="text/event-stream")


@router.get("/models")
async def list_models(client: Request):
    try:
        result = await gateway.get_available_models(_model_patterns(client))
        return {
            "object": "list",
            "models": result.get("models", []),
            "data": result.get("data", []),
        }
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest, client: Request):
    try:
        client_ip = client.client.host if client.client else "unknown"
        endpoint_id = _get_endpoint_id(client)
        model_patterns = _model_patterns(client)
        request_data = request.model_dump(exclude_none=True)

        if request.stream:
            return _safe_stream(
                request.model,
                "/v1/chat/completions",
                request_data,
                client_ip,
                endpoint_id,
                model_patterns,
            )
        else:
            response = await gateway.route_request(
                request.model,
                "/v1/chat/completions",
                request_data,
                client_ip,
                endpoint_id=endpoint_id,
                model_patterns=model_patterns,
            )
            return response
    except ValueError:
        _raise_model_not_found(request.model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/completions")
async def completions(request: CompletionRequest, client: Request):
    try:
        client_ip = client.client.host if client.client else "unknown"
        endpoint_id = _get_endpoint_id(client)
        model_patterns = _model_patterns(client)
        request_data = request.model_dump(exclude_none=True)

        if request.stream:
            return _safe_stream(
                request.model,
                "/v1/completions",
                request_data,
                client_ip,
                endpoint_id,
                model_patterns,
            )
        else:
            response = await gateway.route_request(
                request.model,
                "/v1/completions",
                request_data,
                client_ip,
                endpoint_id=endpoint_id,
                model_patterns=model_patterns,
            )
            return response
    except ValueError:
        _raise_model_not_found(request.model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/classify")
async def classify(request: ClassifyRequest, client: Request):
    try:
        client_ip = client.client.host if client.client else "unknown"
        endpoint_id = _get_endpoint_id(client)
        model_patterns = _model_patterns(client)
        request_data = request.model_dump(exclude_none=True)
        response = await gateway.route_request(
            request.model,
            "/v1/classify",
            request_data,
            client_ip,
            required_endpoint="/v1/classify",
            endpoint_id=endpoint_id,
            model_patterns=model_patterns,
        )
        return response
    except ValueError:
        _raise_model_not_found(request.model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/embeddings")
async def embeddings(request: EmbeddingRequest, client: Request):
    try:
        client_ip = client.client.host if client.client else "unknown"
        endpoint_id = _get_endpoint_id(client)
        model_patterns = _model_patterns(client)
        request_data = request.model_dump(exclude_none=True)
        response = await gateway.route_request(
            request.model,
            "/v1/embeddings",
            request_data,
            client_ip,
            required_endpoint="/v1/embeddings",
            endpoint_id=endpoint_id,
            model_patterns=model_patterns,
        )
        return response
    except ValueError:
        _raise_model_not_found(request.model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rerank")
async def rerank(request: RerankRequest, client: Request):
    try:
        client_ip = client.client.host if client.client else "unknown"
        endpoint_id = _get_endpoint_id(client)
        model_patterns = _model_patterns(client)
        request_data = request.model_dump(exclude_none=True)
        response = await gateway.route_request(
            request.model,
            "/v1/rerank",
            request_data,
            client_ip,
            required_endpoint="/v1/rerank",
            endpoint_id=endpoint_id,
            model_patterns=model_patterns,
        )
        return response
    except ValueError:
        _raise_model_not_found(request.model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))
