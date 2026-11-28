"""OpenAI-compatible API gateway endpoints.

Each request is authenticated against the api_keys table; the
resolved endpoint is stored in request.state by the auth middleware and
passed through to the gateway for scoping and logging. A key belongs to
exactly one endpoint, whose model scoping restricts the registry.
"""

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from app.config import settings
from app.gateway import (
    MultipartUpload,
    UpstreamResponse,
    UpstreamServiceError,
    VirtualModelUnavailableError,
    gateway,
)
from app.models import (
    ChatCompletionRequest,
    ClassifyRequest,
    CompletionRequest,
    EmbeddingRequest,
    RerankRequest,
    TranscriptionRequest,
)
from app.services.client_ip import resolve_client_ip

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["openai"])


def _get_endpoint_id(request: Request) -> str | None:
    return getattr(request.state, "endpoint_id", None)  # set by auth_middleware


def _get_api_key_attribution(request: Request) -> tuple[str | None, str | None]:
    """(api_key_id, api_key_name) stamped by auth_middleware.

    Management-key traffic gets (None, None) - it has no named credential.
    """
    return (
        getattr(request.state, "api_key_id", None),
        getattr(request.state, "api_key_name", None),
    )


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


def _raise_virtual_unavailable(exc: VirtualModelUnavailableError):
    """OpenAI-shaped 503 carrying the per-target failure reasons."""
    raise HTTPException(
        status_code=503,
        detail={
            "message": str(exc),
            "type": "service_unavailable",
            "param": None,
            "code": "virtual_model_unavailable",
            "targets": exc.reasons,
        },
    )


def _safe_stream(
    model: str,
    endpoint: str,
    request_data: dict,
    client_ip: str,
    endpoint_id,
    model_patterns: list[str] | None,
    api_key_id: str | None = None,
    api_key_name: str | None = None,
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
            api_key_id=api_key_id,
            api_key_name=api_key_name,
        )
        try:
            async for chunk in stream:
                yield chunk
        except VirtualModelUnavailableError as e:
            # Structured payload so virtual-model exhaustion keeps its
            # per-target reasons even though HTTP status is already 200.
            payload = json.dumps(
                {
                    "error": str(e),
                    "code": "virtual_model_unavailable",
                    "targets": e.reasons,
                }
            )
            yield f"data: {payload}\n\n".encode()
        except Exception as e:  # noqa: BLE001
            payload = json.dumps({"error": str(e)})
            yield f"data: {payload}\n\n".encode()
        finally:
            await stream.aclose()

    return StreamingResponse(generator(), media_type="text/event-stream")


def _transcription_error(
    message: str,
    param: str | None = None,
    code: str = "invalid_request_error",
    status_code: int = 400,
) -> JSONResponse:
    """OpenAI-shaped error envelope for the transcription route."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": (
                    "invalid_request_error" if status_code < 500 else "server_error"
                ),
                "param": param,
                "code": code,
            }
        },
    )


@router.post("/audio/transcriptions")
async def audio_transcriptions(request: Request):
    """OpenAI-compatible transcription, streamed to the host as multipart.

    The upload is spooled to a disk temporary file (Starlette's 1 MiB threshold
    keeps only a small part in RAM) and the gateway opens a fresh handle per
    attempt, so the file is never held whole. The request model is resolved
    before the forward, so a host that cannot serve the endpoint is never picked,
    and the per-key rate limiter in ``auth_middleware`` already covers this path.

    The OpenAI optional ``keywords`` and ``languages`` fields are forwarded to
    the shim, which maps them onto the runtime's prompt and language fields;
    ``chunking_strategy`` and ``include[]`` are forwarded too and the shim drops
    them because no runtime implements them.
    """
    try:
        form = await request.form()
    except (MultipartParseError, MultiPartException, StarletteHTTPException):
        # Three layers can surface a bad body: python-multipart's own
        # MultipartParseError, Starlette's MultiPartException (a missing
        # boundary, too many fields/files), and - because Starlette converts
        # the latter to HTTPException(400) inside an app - an HTTPException.
        # None carries the OpenAI envelope, so all are answered with one here.
        return _transcription_error("malformed multipart body")

    upload = form.get("file")
    tmp_path: str | None = None
    model_name = form.get("model")
    try:
        if not isinstance(upload, UploadFile):
            return _transcription_error("file is required", "file")

        try:
            payload = TranscriptionRequest.from_form(form)
        except ValidationError as exc:
            first = exc.errors()[0]
            field = first.get("loc") or ()
            param = ".".join(str(part) for part in field) or None
            # Pydantic prefixes a validator's ValueError with "Value error, ";
            # that wrapper is not part of the message the client should read.
            message = str(first.get("msg")).removeprefix("Value error, ")
            return _transcription_error(message, param)

        # Copy the spooled upload to a named temp file so the gateway can
        # re-open it for every attempt (aiohttp closes the handle it is given
        # after a connection error, so a single handle cannot be retried). The
        # copy runs in a worker thread so a large upload cannot block the event
        # loop.
        with tempfile.NamedTemporaryFile(delete=False) as temp:
            tmp_path = temp.name
            upload.file.seek(0)
            await asyncio.to_thread(shutil.copyfileobj, upload.file, temp, 1024 * 1024)

        fields: list[tuple[str, str]] = []
        for name, value in form.multi_items():
            if name == "file" or not isinstance(value, str):
                continue
            fields.append((name, value))

        api_key_id, api_key_name = _get_api_key_attribution(request)
        result = await gateway.route_request(
            payload.model,
            "/v1/audio/transcriptions",
            {"model": payload.model},
            resolve_client_ip(request),
            required_endpoint="/v1/audio/transcriptions",
            endpoint_id=_get_endpoint_id(request),
            model_patterns=_model_patterns(request),
            api_key_id=api_key_id,
            api_key_name=api_key_name,
            multipart=MultipartUpload(
                fields=fields,
                path=tmp_path,
                filename=upload.filename or "audio",
                content_type=upload.content_type or "application/octet-stream",
            ),
            timeout_s=settings.audio_route_total_timeout_s,
        )
        if isinstance(result, UpstreamResponse):
            return Response(
                content=result.body,
                status_code=result.status_code,
                media_type=result.content_type,
            )
        return result
    except VirtualModelUnavailableError as exc:
        _raise_virtual_unavailable(exc)
    except UpstreamServiceError as exc:
        # A reachable instance answered with a retryable upstream failure (a
        # 504 from a slow or overloaded runtime). Relay its status and body:
        # the generic ValueError funnel would answer 404 model_not_found and
        # blame the model for the host's timeout.
        return Response(
            content=exc.body.encode("utf-8", "replace"),
            status_code=exc.status_code,
            media_type=exc.content_type,
        )
    except ValueError:
        _raise_model_not_found(str(model_name) if model_name is not None else "")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)
        if isinstance(upload, UploadFile):
            await upload.close()


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
        client_ip = resolve_client_ip(client)
        endpoint_id = _get_endpoint_id(client)
        api_key_id, api_key_name = _get_api_key_attribution(client)
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
                api_key_id,
                api_key_name,
            )
        else:
            response = await gateway.route_request(
                request.model,
                "/v1/chat/completions",
                request_data,
                client_ip,
                endpoint_id=endpoint_id,
                model_patterns=model_patterns,
                api_key_id=api_key_id,
                api_key_name=api_key_name,
            )
            return response
    except VirtualModelUnavailableError as exc:
        _raise_virtual_unavailable(exc)
    except ValueError:
        _raise_model_not_found(request.model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/completions")
async def completions(request: CompletionRequest, client: Request):
    try:
        client_ip = resolve_client_ip(client)
        endpoint_id = _get_endpoint_id(client)
        api_key_id, api_key_name = _get_api_key_attribution(client)
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
                api_key_id,
                api_key_name,
            )
        else:
            response = await gateway.route_request(
                request.model,
                "/v1/completions",
                request_data,
                client_ip,
                endpoint_id=endpoint_id,
                model_patterns=model_patterns,
                api_key_id=api_key_id,
                api_key_name=api_key_name,
            )
            return response
    except VirtualModelUnavailableError as exc:
        _raise_virtual_unavailable(exc)
    except ValueError:
        _raise_model_not_found(request.model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/classify")
async def classify(request: ClassifyRequest, client: Request):
    try:
        client_ip = resolve_client_ip(client)
        endpoint_id = _get_endpoint_id(client)
        api_key_id, api_key_name = _get_api_key_attribution(client)
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
            api_key_id=api_key_id,
            api_key_name=api_key_name,
        )
        return response
    except VirtualModelUnavailableError as exc:
        _raise_virtual_unavailable(exc)
    except ValueError:
        _raise_model_not_found(request.model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/embeddings")
async def embeddings(request: EmbeddingRequest, client: Request):
    try:
        client_ip = resolve_client_ip(client)
        endpoint_id = _get_endpoint_id(client)
        api_key_id, api_key_name = _get_api_key_attribution(client)
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
            api_key_id=api_key_id,
            api_key_name=api_key_name,
        )
        return response
    except VirtualModelUnavailableError as exc:
        _raise_virtual_unavailable(exc)
    except ValueError:
        _raise_model_not_found(request.model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rerank")
async def rerank(request: RerankRequest, client: Request):
    try:
        client_ip = resolve_client_ip(client)
        endpoint_id = _get_endpoint_id(client)
        api_key_id, api_key_name = _get_api_key_attribution(client)
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
            api_key_id=api_key_id,
            api_key_name=api_key_name,
        )
        return response
    except VirtualModelUnavailableError as exc:
        _raise_virtual_unavailable(exc)
    except ValueError:
        _raise_model_not_found(request.model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))
