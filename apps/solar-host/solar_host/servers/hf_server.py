#!/usr/bin/env python3
"""
Standalone HuggingFace Server

A FastAPI application that loads HuggingFace models and provides
OpenAI-compatible API endpoints.

Supports:
- AutoModelForCausalLM: text generation (/v1/chat/completions, /v1/completions)
- AutoModelForSequenceClassification: classification (/v1/classify)
- AutoModel: embeddings (/v1/embeddings)

Usage:
    python -m solar_host.servers.hf_server \
        --model-id "meta-llama/Llama-2-7b-hf" \
        --model-type causal \
        --alias "llama2:7b" \
        --port 3501 \
        --api-key "secret"
"""

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import numpy
import torch
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from solar_host.servers.api_key import resolve_api_key
from solar_host.servers.audio_fields import (
    ACCEPTED_FIELDS,
    keywords_prompt,
    single_language,
    text_parts,
)
from solar_host.servers.audio_formats import (
    RESPONSE_FORMATS,
    TIMESTAMP_GRANULARITIES,
    attach_words_to_segments,
    normalize_language,
    render_cues,
    segments_from_chunks,
    words_from_chunks,
)

if TYPE_CHECKING:
    from transformers import (
        PreTrainedModel,
        PreTrainedTokenizer,
        PreTrainedTokenizerFast,
    )

# Capabilities exposed via /v1/models, used by downstream apps
# (e.g. Orchestrator) to differentiate text-only vs multimodal models.
MODEL_TYPE_CAPABILITIES: dict[str, list[str]] = {
    "causal": ["completion"],
    "classification": ["classification"],
    "embedding": ["embedding"],
    "vision": ["completion", "multimodal"],
    "audio": ["transcription"],
}

# A transcription request must always get an answer. Word-level timestamps make
# transformers run a cross-attention alignment pass that has been observed to keep a
# whisper-large-v3 worker at full CPU for more than 15 minutes on one short clip,
# so they get a shorter bound than ordinary transcription. The worker thread cannot
# be interrupted once started (asyncio.to_thread is not cancellable), so these
# bounds unblock the HTTP request; they do not reclaim the worker.
# Override with SOLAR_HF_TRANSCRIPTION_TIMEOUT_S and
# SOLAR_HF_WORD_TIMESTAMP_TIMEOUT_S.
TRANSCRIPTION_TIMEOUT_S = float(
    os.environ.get("SOLAR_HF_TRANSCRIPTION_TIMEOUT_S", "600")
)
WORD_TIMESTAMP_TIMEOUT_S = float(
    os.environ.get("SOLAR_HF_WORD_TIMESTAMP_TIMEOUT_S", "60")
)
# ffmpeg decodes a client-supplied upload, so the child gets its own bound: a
# crafted file must not hold the request open (and the worker slot) forever.
# Override with SOLAR_HF_DECODE_TIMEOUT_S.
DECODE_TIMEOUT_S = float(os.environ.get("SOLAR_HF_DECODE_TIMEOUT_S", "120"))

# Configure logging to output structured messages for the runner to parse
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ============================================================================
# Request/Response Models
# ============================================================================


class ChatMessage(BaseModel):
    role: str
    # Either a plain string (text-only) or an OpenAI-style list of content parts
    # like [{"type":"text","text":"..."}, {"type":"image_url","image_url":{"url":"..."}}]
    content: str | list[dict[str, Any]]
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: float | None = 1.0
    top_p: float | None = 1.0
    max_tokens: int | None = None
    stream: bool | None = False
    stop: list[str] | None = None


class CompletionRequest(BaseModel):
    model: str
    prompt: str
    temperature: float | None = 1.0
    top_p: float | None = 1.0
    max_tokens: int | None = None
    stream: bool | None = False
    stop: list[str] | None = None


class ClassifyRequest(BaseModel):
    model: str
    input: str | list[str] = Field(..., description="Text or list of texts to classify")
    return_all_scores: bool = Field(
        default=False,
        description="Return scores for all classes, not just top prediction",
    )


class ClassifyScoreItem(BaseModel):
    """Individual class score."""

    label: str
    score: float


class ClassifyChoice(BaseModel):
    index: int
    label: str
    score: float
    all_scores: list[ClassifyScoreItem] | None = Field(
        default=None, description="Scores for all classes (when return_all_scores=True)"
    )


class ClassifyResponse(BaseModel):
    id: str
    object: str = "classification"
    model: str
    choices: list[ClassifyChoice]
    usage: dict[str, int]


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: dict[str, int]


class CompletionChoice(BaseModel):
    index: int
    text: str
    finish_reason: str


class CompletionResponse(BaseModel):
    id: str
    object: str = "text_completion"
    created: int
    model: str
    choices: list[CompletionChoice]
    usage: dict[str, int]


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "huggingface"


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str] = Field(..., description="Text or list of texts to embed")
    encoding_format: str | None = Field(
        default="float", description="Encoding format: 'float' or 'base64'"
    )
    dimensions: int | None = Field(
        default=None, description="Optional dimension truncation"
    )


class EmbeddingData(BaseModel):
    object: str = "embedding"
    embedding: list[float]
    index: int


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingData]
    model: str
    usage: dict[str, int]


# ============================================================================
# Global State
# ============================================================================


class ServerState:
    """Global server state holding the loaded model."""

    def __init__(self):
        self.model: PreTrainedModel | None = None
        self.tokenizer: PreTrainedTokenizer | PreTrainedTokenizerFast | None = None
        # Multi-modal processor (used for vision/multimodal models). Wraps
        # the tokenizer plus image preprocessor.
        self.processor: Any | None = None
        self.model_id: str = ""
        self.model_type: str = "causal"
        self.alias: str = ""
        self.device: str = "cpu"
        self.max_length: int = 4096
        self.labels: list[str] | None = None
        self.normalize_embeddings: bool = True
        # Speech-to-text state (model_type == "audio")
        self.pipeline: Any | None = None
        self.chunk_length_s: float = 30.0
        self.batch_size: int = 8
        self.language: str | None = None
        self.api_key: str = ""
        self.created_at: int = int(datetime.now(UTC).timestamp())

    def ensure_loaded(self) -> None:
        """Ensure model and tokenizer are loaded, raise if not."""
        if self.model is None or self.tokenizer is None:
            raise RuntimeError("Model not loaded. Call load_model() first.")

    def get_device(self, device_str: str) -> str:
        """Resolve device string to actual device."""
        if device_str == "auto":
            if torch.cuda.is_available():
                return "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
            else:
                return "cpu"
        return device_str

    def get_dtype(self, dtype_str: str) -> torch.dtype:
        """Resolve dtype string to torch dtype."""
        if dtype_str == "auto":
            if self.device == "cuda" or self.device == "mps":
                return torch.float16
            else:
                return torch.float32
        dtype_map = {
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
            "float32": torch.float32,
        }
        return dtype_map.get(dtype_str, torch.float32)

    def load_model(
        self,
        model_id: str,
        model_type: str,
        alias: str,
        device: str,
        dtype: str,
        max_length: int,
        labels: list[str] | None,
        trust_remote_code: bool,
        use_flash_attention: bool,
        normalize_embeddings: bool = True,
        chunk_length_s: float = 30.0,
        batch_size: int = 8,
        language: str | None = None,
    ):
        """Load the model based on type."""
        from transformers import AutoTokenizer

        self.model_id = model_id
        self.model_type = model_type
        self.alias = alias
        self.device = self.get_device(device)
        self.max_length = max_length
        self.labels = labels
        self.normalize_embeddings = normalize_embeddings
        self.chunk_length_s = chunk_length_s
        self.batch_size = batch_size
        self.language = language

        model_dtype = self.get_dtype(dtype)

        logger.info(f"Loading model: {model_id}")
        logger.info(f"Model type: {model_type}")
        logger.info(f"Device: {self.device}")
        logger.info(f"Dtype: {model_dtype}")

        # Load tokenizer
        tokenizer = AutoTokenizer.from_pretrained(
            model_id,
            trust_remote_code=trust_remote_code,
        )

        # Ensure pad token exists
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        self.tokenizer = tokenizer

        # Load model based on type
        if model_type == "causal":
            from transformers import AutoModelForCausalLM

            model_kwargs: dict[str, object] = {
                "dtype": model_dtype,
                "trust_remote_code": trust_remote_code,
                "device_map": self.device if self.device != "mps" else None,
            }

            # Add flash attention if supported and requested
            if use_flash_attention and self.device == "cuda":
                model_kwargs["attn_implementation"] = "flash_attention_2"

            model = AutoModelForCausalLM.from_pretrained(model_id, **model_kwargs)

            # For MPS, move model manually
            if self.device == "mps":
                target_device = torch.device(self.device)
                model = model.to(device=target_device)  # type: ignore[call-overload]

            self.model = model

        elif model_type == "classification":
            from transformers import AutoModelForSequenceClassification

            model = AutoModelForSequenceClassification.from_pretrained(
                model_id,
                dtype=model_dtype,
                trust_remote_code=trust_remote_code,
            )
            target_device = torch.device(self.device)
            model = model.to(device=target_device)  # type: ignore[call-overload]
            self.model = model

            # Get labels from model config if not provided
            if self.labels is None and hasattr(model.config, "id2label"):
                id2label = model.config.id2label
                if id2label is not None:
                    self.labels = [id2label[i] for i in range(len(id2label))]

        elif model_type == "embedding":
            from transformers import AutoModel

            model = AutoModel.from_pretrained(
                model_id,
                dtype=model_dtype,
                trust_remote_code=trust_remote_code,
            )
            target_device = torch.device(self.device)
            model = model.to(device=target_device)  # type: ignore[call-overload]
            self.model = model
            logger.info(f"Normalize embeddings: {self.normalize_embeddings}")

        elif model_type == "vision":
            from transformers import AutoProcessor

            try:
                from transformers import AutoModelForImageTextToText as VisionModelCls
            except ImportError:
                from transformers import AutoModelForVision2Seq as VisionModelCls

            processor = AutoProcessor.from_pretrained(
                model_id,
                trust_remote_code=trust_remote_code,
            )
            self.processor = processor

            model_kwargs: dict[str, object] = {
                "dtype": model_dtype,
                "trust_remote_code": trust_remote_code,
                "device_map": self.device if self.device != "mps" else None,
            }
            if use_flash_attention and self.device == "cuda":
                model_kwargs["attn_implementation"] = "flash_attention_2"

            model = VisionModelCls.from_pretrained(model_id, **model_kwargs)
            if self.device == "mps":
                target_device = torch.device(self.device)
                model = model.to(device=target_device)  # type: ignore[call-overload]
            self.model = model

        elif model_type == "audio":
            from transformers import (
                AutoModelForSpeechSeq2Seq,
                AutoProcessor,
                pipeline,
            )

            processor = AutoProcessor.from_pretrained(
                model_id,
                trust_remote_code=trust_remote_code,
            )
            model = AutoModelForSpeechSeq2Seq.from_pretrained(
                model_id,
                dtype=model_dtype,
                trust_remote_code=trust_remote_code,
            )
            target_device = torch.device(self.device)
            model = model.to(device=target_device)  # type: ignore[call-overload]
            self.processor = processor
            self.tokenizer = processor.tokenizer
            self.model = model
            # The components must be passed explicitly: the pipeline cannot infer
            # a tokenizer from a loaded model object, and a Whisper processor
            # owns both halves the ASR pipeline needs.
            pipeline_kwargs: dict[str, Any] = {}
            feature_extractor = getattr(processor, "feature_extractor", None)
            if feature_extractor is not None:
                pipeline_kwargs["feature_extractor"] = feature_extractor
            self.pipeline = pipeline(
                "automatic-speech-recognition",
                model=model,
                tokenizer=processor.tokenizer,
                device=self.device,
                **pipeline_kwargs,
            )

        if self.model is not None:
            self.model.eval()
        logger.info(f"Model loaded successfully on {self.device}")


state = ServerState()
security = HTTPBearer(auto_error=False)


# ============================================================================
# Auth
# ============================================================================


def _transcription_error(
    message: str,
    param: str | None,
    code: str,
    status_code: int = 400,
) -> JSONResponse:
    """OpenAI-shaped error envelope for the transcription endpoint."""
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


def _word_timeout_error(timeout_s: float) -> JSONResponse:
    """The dedicated 504 for a word-timestamp pass that ran out of time."""
    return _transcription_error(
        f"word-level timestamps did not finish within {timeout_s:g}s on "
        "the HuggingFace audio backend; word timestamps are "
        "prohibitively slow there, so request "
        "timestamp_granularities[]=segment (or omit it) instead",
        "timestamp_granularities[]",
        "timeout",
        504,
    )


async def _run_transcription_pass(
    samples: numpy.ndarray,
    generate_kwargs: dict[str, object],
    return_timestamps: bool | str,
    timeout_s: float,
    *,
    return_language: bool = False,
) -> dict[str, Any]:
    """Run one pipeline pass, bounded so the HTTP request always answers."""
    assert state.pipeline is not None
    pipeline_kwargs: dict[str, Any] = {
        "chunk_length_s": state.chunk_length_s,
        "batch_size": state.batch_size,
        "generate_kwargs": generate_kwargs,
        "return_timestamps": return_timestamps,
    }
    if return_language and getattr(state.pipeline, "type", "") == "seq2seq_whisper":
        # The detected language is only returned when the pipeline is asked for
        # it, and the parameter is Whisper-only: transformers raises
        # "Only Whisper can return language for now." for any other type.
        pipeline_kwargs["return_language"] = True
    return await asyncio.wait_for(
        asyncio.to_thread(state.pipeline, samples, **pipeline_kwargs),
        timeout=timeout_s,
    )


async def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Verify API key from Authorization header."""
    if not state.api_key:
        return True

    # Check Authorization header
    if credentials and credentials.credentials == state.api_key:
        return True

    # Check X-API-Key header as fallback
    api_key = request.headers.get("X-API-Key")
    if api_key and api_key == state.api_key:
        return True

    raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ============================================================================
# Lifespan
# ============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("HuggingFace server starting...")
    yield
    logger.info("HuggingFace server shutting down...")


# ============================================================================
# App Setup
# ============================================================================


# Custom JSONResponse that ensures UTF-8 encoding
class UTF8JSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")


app = FastAPI(
    title="HuggingFace Server",
    description="OpenAI-compatible API for HuggingFace models",
    version="1.0.0",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)


# ============================================================================
# Endpoints
# ============================================================================


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "model": state.alias,
        "model_type": state.model_type,
        "device": state.device,
    }


@app.get("/v1/models")
async def list_models(_: bool = Depends(verify_api_key)):
    """List available models.

    Returns both an Ollama-style ``models`` array and an OpenAI-style ``data``
    array (matching llama.cpp's shape), each carrying ``capabilities`` so that
    downstream apps (e.g. solar-control's gateway, the orchestrator) can
    differentiate text vs multimodal models without trial-and-error.
    """
    capabilities = MODEL_TYPE_CAPABILITIES.get(state.model_type, ["completion"])
    return {
        "models": [
            {
                "name": state.alias,
                "model": state.alias,
                "modified_at": "",
                "size": "",
                "digest": "",
                "type": "model",
                "description": "",
                "tags": [""],
                "capabilities": capabilities,
                "parameters": "",
                "details": {
                    "parent_model": "",
                    "format": "huggingface",
                    "family": "",
                    "families": [""],
                    "parameter_size": "",
                    "quantization_level": "",
                },
            }
        ],
        "object": "list",
        "data": [
            {
                "id": state.alias,
                "object": "model",
                "created": state.created_at,
                "owned_by": "huggingface",
                "capabilities": capabilities,
            }
        ],
    }


def _load_image_from_url(url: str):
    """Load a PIL Image from a data URI or http(s) URL.

    Imports PIL lazily so non-vision deployments don't pay the cost.
    """
    from PIL import Image

    if url.startswith("data:"):
        try:
            _header, b64 = url.split(",", 1)
        except ValueError as exc:
            raise ValueError(f"Malformed data URI: {url[:60]}...") from exc
        raw = base64.b64decode(b64)
        return Image.open(io.BytesIO(raw)).convert("RGB")

    parsed = urlparse(url)
    if parsed.scheme in ("http", "https"):
        import requests

        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content)).convert("RGB")

    raise ValueError(f"Unsupported image URL scheme: {parsed.scheme!r}")


def _normalize_messages_for_processor(
    messages: list[ChatMessage],
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Convert OpenAI-style messages into processor-friendly form + image list.

    Each `image_url` content part is downloaded into a PIL image and replaced
    by ``{"type": "image"}`` (the convention most multimodal HF processors expect
    for ``apply_chat_template``).
    """
    norm_messages: list[dict[str, Any]] = []
    images: list[Any] = []

    for m in messages:
        if isinstance(m.content, str):
            norm_messages.append({"role": m.role, "content": m.content})
            continue

        parts: list[dict[str, Any]] = []
        for part in m.content:
            ptype = part.get("type")
            if ptype == "text":
                parts.append({"type": "text", "text": part.get("text", "")})
            elif ptype == "image_url":
                image_url = part.get("image_url", {})
                url = image_url.get("url") if isinstance(image_url, dict) else image_url
                if not url:
                    raise ValueError("image_url part missing 'url'")
                images.append(_load_image_from_url(url))
                parts.append({"type": "image"})
            else:
                raise ValueError(f"Unsupported content part type: {ptype!r}")
        norm_messages.append({"role": m.role, "content": parts})

    return norm_messages, images


async def _chat_completion_causal(
    request: ChatCompletionRequest,
) -> ChatCompletionResponse:
    """Chat completion implementation for causal (text-only) models."""
    state.ensure_loaded()
    model = state.model
    tokenizer = state.tokenizer
    assert model is not None and tokenizer is not None  # Type narrowing

    start_time = time.time()
    logger.info(f"[REQUEST] model={state.alias} endpoint=/v1/chat/completions")

    # Causal text-only path expects plain string content.
    plain_messages: list[dict[str, str]] = []
    for m in request.messages:
        if not isinstance(m.content, str):
            raise HTTPException(
                status_code=400,
                detail="Causal model received non-text content; use a vision model",
            )
        plain_messages.append({"role": m.role, "content": m.content})

    prompt: str
    if hasattr(tokenizer, "apply_chat_template"):
        template_result = tokenizer.apply_chat_template(
            plain_messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        prompt = str(template_result)
    else:
        prompt = "\n".join([f"{m['role']}: {m['content']}" for m in plain_messages])
        prompt += "\nassistant:"

    encoded = tokenizer(
        prompt,
        return_tensors="pt",
        truncation=True,
        max_length=state.max_length,
    )
    inputs = encoded.to(state.device)

    input_ids: torch.Tensor = inputs["input_ids"]  # type: ignore[assignment]
    prompt_tokens: int = int(input_ids.shape[1])
    max_new_tokens = request.max_tokens or (state.max_length - prompt_tokens)

    with torch.no_grad():
        outputs = model.generate(  # type: ignore[operator]
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=request.temperature or 1.0,
            top_p=request.top_p or 1.0,
            do_sample=request.temperature != 0,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )

    generated_ids = outputs[0][prompt_tokens:]
    response_text = tokenizer.decode(generated_ids, skip_special_tokens=True)
    completion_tokens = len(generated_ids)

    elapsed_ms = (time.time() - start_time) * 1000
    logger.info(
        f"[COMPLETE] model={state.alias} tokens={completion_tokens} time_ms={elapsed_ms:.1f}"
    )

    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
        created=int(time.time()),
        model=state.alias,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=response_text),
                finish_reason="stop",
            )
        ],
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )


async def _chat_completion_vision(
    request: ChatCompletionRequest,
) -> ChatCompletionResponse:
    """Chat completion implementation for vision/multimodal models."""
    state.ensure_loaded()
    model = state.model
    processor = state.processor
    if processor is None:
        raise HTTPException(
            status_code=500, detail="Vision model loaded without a processor"
        )
    assert model is not None  # Type narrowing

    start_time = time.time()
    logger.info(f"[REQUEST] model={state.alias} endpoint=/v1/chat/completions")

    norm_messages, images = _normalize_messages_for_processor(request.messages)

    template_result = processor.apply_chat_template(
        norm_messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    prompt = str(template_result)

    proc_kwargs: dict[str, Any] = {
        "text": prompt,
        "return_tensors": "pt",
    }
    if images:
        proc_kwargs["images"] = images

    encoded = processor(**proc_kwargs)
    inputs = encoded.to(state.device)

    input_ids: torch.Tensor = inputs["input_ids"]  # type: ignore[assignment]
    prompt_tokens: int = int(input_ids.shape[1])
    max_new_tokens = request.max_tokens or (state.max_length - prompt_tokens)

    tokenizer = getattr(processor, "tokenizer", state.tokenizer)
    pad_token_id = getattr(tokenizer, "pad_token_id", None) if tokenizer else None
    eos_token_id = getattr(tokenizer, "eos_token_id", None) if tokenizer else None

    with torch.no_grad():
        outputs = model.generate(  # type: ignore[operator]
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=request.temperature or 1.0,
            top_p=request.top_p or 1.0,
            do_sample=request.temperature != 0,
            pad_token_id=pad_token_id,
            eos_token_id=eos_token_id,
        )

    generated_ids = outputs[0][prompt_tokens:]
    if hasattr(processor, "decode"):
        response_text = processor.decode(generated_ids, skip_special_tokens=True)
    elif tokenizer is not None:
        response_text = tokenizer.decode(generated_ids, skip_special_tokens=True)
    else:
        raise HTTPException(
            status_code=500, detail="No decoder available for vision model output"
        )
    completion_tokens = len(generated_ids)

    elapsed_ms = (time.time() - start_time) * 1000
    logger.info(
        f"[COMPLETE] model={state.alias} tokens={completion_tokens} time_ms={elapsed_ms:.1f}"
    )

    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:8]}",
        created=int(time.time()),
        model=state.alias,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=response_text),
                finish_reason="stop",
            )
        ],
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest, _: bool = Depends(verify_api_key)
):
    """Chat completion endpoint (OpenAI compatible)."""
    if state.model_type not in ("causal", "vision"):
        raise HTTPException(
            status_code=400,
            detail="Chat completions only available for causal or vision models",
        )

    try:
        if state.model_type == "vision":
            return await _chat_completion_vision(request)
        return await _chat_completion_causal(request)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"[ERROR] {e!s}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/completions")
async def completions(request: CompletionRequest, _: bool = Depends(verify_api_key)):
    """Text completion endpoint (OpenAI compatible)."""
    if state.model_type != "causal":
        raise HTTPException(
            status_code=400, detail="Completions only available for causal models"
        )

    # Ensure model is loaded
    state.ensure_loaded()
    model = state.model
    tokenizer = state.tokenizer
    assert model is not None and tokenizer is not None  # Type narrowing

    start_time = time.time()
    logger.info(f"[REQUEST] model={state.alias} endpoint=/v1/completions")

    try:
        # Tokenize
        encoded = tokenizer(
            request.prompt,
            return_tensors="pt",
            truncation=True,
            max_length=state.max_length,
        )
        inputs = encoded.to(state.device)

        input_ids: torch.Tensor = inputs["input_ids"]  # type: ignore[assignment]
        prompt_tokens: int = int(input_ids.shape[1])
        max_new_tokens = request.max_tokens or (state.max_length - prompt_tokens)

        # Generate
        with torch.no_grad():
            outputs = model.generate(  # type: ignore[operator]
                **inputs,
                max_new_tokens=max_new_tokens,
                temperature=request.temperature or 1.0,
                top_p=request.top_p or 1.0,
                do_sample=request.temperature != 0,
                pad_token_id=tokenizer.pad_token_id,
                eos_token_id=tokenizer.eos_token_id,
            )

        # Decode response (only the generated part)
        generated_ids = outputs[0][prompt_tokens:]
        response_text = tokenizer.decode(generated_ids, skip_special_tokens=True)
        completion_tokens = len(generated_ids)

        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(
            f"[COMPLETE] model={state.alias} tokens={completion_tokens} time_ms={elapsed_ms:.1f}"
        )

        return CompletionResponse(
            id=f"cmpl-{uuid.uuid4().hex[:8]}",
            created=int(time.time()),
            model=state.alias,
            choices=[
                CompletionChoice(
                    index=0,
                    text=response_text,
                    finish_reason="stop",
                )
            ],
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )

    except Exception as e:  # noqa: BLE001
        logger.error(f"[ERROR] {e!s}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/classify")
async def classify(request: ClassifyRequest, _: bool = Depends(verify_api_key)):
    """Classification endpoint for SequenceClassification models."""
    if state.model_type != "classification":
        raise HTTPException(
            status_code=400,
            detail="Classification only available for classification models",
        )

    # Ensure model is loaded
    state.ensure_loaded()
    model = state.model
    tokenizer = state.tokenizer
    assert model is not None and tokenizer is not None  # Type narrowing

    start_time = time.time()
    logger.info(f"[REQUEST] model={state.alias} endpoint=/v1/classify")

    try:
        # Handle single or batch input
        texts: list[str] = (
            request.input if isinstance(request.input, list) else [request.input]
        )

        # Tokenize
        encoded = tokenizer(
            texts,
            return_tensors="pt",
            truncation=True,
            padding=True,
            max_length=state.max_length,
        )
        inputs = encoded.to(state.device)

        input_ids: torch.Tensor = inputs["input_ids"]  # type: ignore[assignment]
        total_tokens: int = int(input_ids.numel())

        # Inference
        with torch.no_grad():
            outputs = model(**inputs)

        # Get predictions
        logits = outputs.logits
        probs = torch.softmax(logits, dim=-1)

        # Build choices
        choices = []
        for i, prob in enumerate(probs):
            best_idx: int = int(prob.argmax().item())
            best_score: float = float(prob[best_idx].item())

            label = str(best_idx)
            if state.labels and best_idx < len(state.labels):
                label = state.labels[best_idx]

            # Build all_scores if requested
            all_scores: list[ClassifyScoreItem] | None = None
            if request.return_all_scores:
                all_scores = []
                for class_idx in range(prob.shape[0]):
                    class_label = str(class_idx)
                    if state.labels and class_idx < len(state.labels):
                        class_label = state.labels[class_idx]
                    all_scores.append(
                        ClassifyScoreItem(
                            label=class_label,
                            score=float(prob[class_idx].item()),
                        )
                    )

            choices.append(
                ClassifyChoice(
                    index=i,
                    label=label,
                    score=best_score,
                    all_scores=all_scores,
                )
            )

        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(
            f"[COMPLETE] model={state.alias} tokens={total_tokens} time_ms={elapsed_ms:.1f}"
        )

        return ClassifyResponse(
            id=f"clf-{uuid.uuid4().hex[:8]}",
            model=state.alias,
            choices=choices,
            usage={
                "prompt_tokens": total_tokens,
                "total_tokens": total_tokens,
            },
        )

    except Exception as e:  # noqa: BLE001
        logger.error(f"[ERROR] {e!s}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/embeddings")
async def embeddings(request: EmbeddingRequest, _: bool = Depends(verify_api_key)):
    """Embeddings endpoint (OpenAI compatible).

    Extracts the last hidden state from the model and applies mean pooling
    to generate fixed-size embedding vectors.
    """
    if state.model_type != "embedding":
        raise HTTPException(
            status_code=400,
            detail="Embeddings only available for embedding models",
        )

    # Ensure model is loaded
    state.ensure_loaded()
    model = state.model
    tokenizer = state.tokenizer
    assert model is not None and tokenizer is not None  # Type narrowing

    start_time = time.time()
    logger.info(f"[REQUEST] model={state.alias} endpoint=/v1/embeddings")

    try:
        # Handle single or batch input
        texts: list[str] = (
            request.input if isinstance(request.input, list) else [request.input]
        )

        # Tokenize
        encoded = tokenizer(
            texts,
            return_tensors="pt",
            truncation=True,
            padding=True,
            max_length=state.max_length,
        )
        inputs = encoded.to(state.device)

        input_ids: torch.Tensor = inputs["input_ids"]  # type: ignore[assignment]
        attention_mask: torch.Tensor = inputs["attention_mask"]  # type: ignore[assignment]
        total_tokens: int = int(input_ids.numel())

        # Forward pass to get last hidden state
        with torch.no_grad():
            outputs = model(**inputs)

        # Get last hidden state: (batch_size, sequence_length, hidden_size)
        last_hidden_state = outputs.last_hidden_state

        # Mean pooling with attention mask
        # Expand attention mask to match hidden state dimensions
        input_mask_expanded = (
            attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
        )
        sum_embeddings = torch.sum(last_hidden_state * input_mask_expanded, dim=1)
        sum_mask = input_mask_expanded.sum(dim=1)
        sum_mask = torch.clamp(sum_mask, min=1e-9)
        embeddings_tensor = sum_embeddings / sum_mask

        # Optionally L2 normalize
        if state.normalize_embeddings:
            embeddings_tensor = torch.nn.functional.normalize(
                embeddings_tensor, p=2, dim=1
            )

        # Optional dimension truncation
        if request.dimensions is not None and request.dimensions > 0:
            embeddings_tensor = embeddings_tensor[:, : request.dimensions]

        # Convert to list
        embeddings_list = embeddings_tensor.cpu().tolist()

        # Build response data
        data = [
            EmbeddingData(
                embedding=emb,
                index=i,
            )
            for i, emb in enumerate(embeddings_list)
        ]

        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(
            f"[COMPLETE] model={state.alias} tokens={total_tokens} time_ms={elapsed_ms:.1f}"
        )

        return EmbeddingResponse(
            data=data,
            model=state.alias,
            usage={
                "prompt_tokens": total_tokens,
                "total_tokens": total_tokens,
            },
        )

    except Exception as e:  # noqa: BLE001
        logger.error(f"[ERROR] {e!s}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Main
# ============================================================================


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    _: bool = Depends(verify_api_key),
):
    """OpenAI-compatible speech-to-text for a HuggingFace audio model.

    The upload is decoded to 16 kHz mono float32 with ffmpeg (no new Python
    dependency), then transcribed through the pipeline. ``srt`` and ``vtt`` are
    rendered from the pipeline segments; ``diarized_json`` is refused because the
    HuggingFace path cannot label speakers. ``compression_ratio``, ``tokens``,
    ``avg_logprob`` and ``no_speech_prob`` are not available and are omitted.
    """
    if state.model_type != "audio":
        return _transcription_error(
            "Transcriptions only available for audio models",
            None,
            "invalid_request_error",
        )

    try:
        form = await request.form()
    except (MultipartParseError, MultiPartException, StarletteHTTPException):
        # Three layers can surface a bad body: python-multipart's own
        # MultipartParseError, Starlette's MultiPartException, and - because
        # Starlette converts the latter to HTTPException(400) when the request
        # runs inside an app - an HTTPException. None carries the OpenAI
        # envelope, so all are answered with one here.
        return _transcription_error(
            "malformed multipart body", None, "invalid_request_error"
        )

    try:
        upload = form.get("file")
        if not isinstance(upload, StarletteUploadFile):
            return _transcription_error(
                "file is required", "file", "invalid_request_error"
            )

        values: dict[str, str] = {}
        for name, value in form.multi_items():
            if name == "file" or not isinstance(value, str):
                continue
            values[name] = value

        response_format = values.get("response_format", "json")
        if values.get("stream", "false").lower() in ("1", "true", "yes"):
            return _transcription_error(
                "streaming transcription is not supported by this endpoint; "
                "omit 'stream' or set it to false",
                "stream",
                "invalid_request_error",
            )
        if response_format not in RESPONSE_FORMATS:
            return _transcription_error(
                f"response_format '{response_format}' is not supported; must be one "
                f"of: {', '.join(RESPONSE_FORMATS)}",
                "response_format",
                "invalid_request_error",
            )
        if response_format == "diarized_json":
            return _transcription_error(
                "response_format 'diarized_json' is not supported by the HuggingFace "
                "audio backend; supported formats are json, text, srt, verbose_json, vtt",
                "response_format",
                "invalid_request_error",
            )
        for name in values:
            if name not in ACCEPTED_FIELDS:
                # The audio shim and this route accept the same field set, so a
                # client gets the same 400 for an unknown field on all three
                # backends. A backend-specific field is accepted and dropped by a
                # runtime that does not own it.
                return _transcription_error(
                    f"field '{name}' is not supported by /v1/audio/transcriptions",
                    name,
                    "invalid_request_error",
                )
        # The audio shim and the control plane refuse an unknown granularity, so a
        # direct request must get the same 400 here instead of the value being
        # silently dropped by this route.
        for name, value in form.multi_items():
            if (
                name in ("timestamp_granularities[]", "timestamp_granularities")
                and isinstance(value, str)
                and value not in TIMESTAMP_GRANULARITIES
            ):
                return _transcription_error(
                    "timestamp_granularities must contain only 'word' or 'segment'",
                    "timestamp_granularities",
                    "invalid_request_error",
                )
        if "model" not in values:
            return _transcription_error(
                "model is required", "model", "invalid_request_error"
            )
        if values["model"] != state.alias:
            return _transcription_error(
                f"model '{values['model']}' is not served by this instance; it "
                f"serves '{state.alias}'",
                "model",
                "model_not_found",
                404,
            )
        if not shutil.which("ffmpeg"):
            return _transcription_error(
                "ffmpeg is required to decode the upload; install ffmpeg on the host",
                None,
                "server_error",
                500,
            )

        # A non-numeric temperature is client input, so it must be a 400 envelope
        # (like the shim answers) rather than an unhandled ValueError.
        temperature_value: float | None = None
        temperature = values.get("temperature")
        if temperature is not None:
            try:
                temperature_value = float(temperature)
            except ValueError:
                return _transcription_error(
                    f"temperature '{temperature}' is not a number",
                    "temperature",
                    "invalid_request_error",
                )

        with tempfile.NamedTemporaryFile(delete=False) as temp:
            temp_path = temp.name
            try:
                upload.file.seek(0)
                await asyncio.to_thread(
                    shutil.copyfileobj, upload.file, temp, 1024 * 1024
                )
            except BaseException:
                # The decode try/finally below owns the unlink for a successful
                # copy; a failed seek/copy never reaches it, so the temp file
                # would leak. Remove it here and let the failure surface.
                Path(temp_path).unlink(missing_ok=True)
                raise
    finally:
        # FormData.close() closes every UploadFile part. Starlette keeps only
        # the last value per key, so closing the selected ``upload`` alone would
        # leak the earlier ``file`` parts' spooled temp files until GC.
        await form.close()

    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-v",
            "error",
            "-i",
            temp_path,
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            "16000",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            samples_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=DECODE_TIMEOUT_S
            )
        except TimeoutError:
            # A crafted upload must not hold the request open: kill the decoder
            # and answer the same envelope as a failed decode. A child that
            # exited at the deadline is already reaped, so only signal a live one.
            if process.returncode is None:
                process.kill()
            await process.wait()
            return _transcription_error(
                f"could not decode the uploaded audio: ffmpeg did not finish "
                f"within {DECODE_TIMEOUT_S:g}s",
                "file",
                "invalid_request_error",
            )
        if process.returncode != 0:
            tail = stderr_bytes.decode("utf-8", "replace").strip()[-300:]
            return _transcription_error(
                f"could not decode the uploaded audio: {tail}",
                "file",
                "invalid_request_error",
            )
    finally:
        Path(temp_path).unlink(missing_ok=True)

    samples = numpy.frombuffer(samples_bytes, dtype=numpy.float32)
    duration = float(len(samples)) / 16000.0
    language = (
        values.get("language")
        or single_language(
            text_parts(form.getlist("languages"))
            + text_parts(form.getlist("languages[]"))
        )
        or state.language
    )
    granularities = form.getlist("timestamp_granularities[]") or form.getlist(
        "timestamp_granularities"
    )

    # `task`, `language` and `prompt_ids` are Whisper-only generate arguments:
    # another seq2seq checkpoint raises when they reach model.generate. The
    # pipeline type is the same signal that gates `return_language` above.
    whisper_pipeline = getattr(state.pipeline, "type", "") == "seq2seq_whisper"
    generate_kwargs: dict[str, object] = {}
    if whisper_pipeline:
        generate_kwargs["task"] = "transcribe"
        if language and language.strip().lower() != "auto":
            # Whisper's lang_to_id has no 'auto' token; omitting the language
            # is how auto-detection is requested.
            generate_kwargs["language"] = language
    if temperature_value is not None and temperature_value > 0:
        generate_kwargs["temperature"] = temperature_value
        generate_kwargs["do_sample"] = True
    prompt = (
        values["prompt"]
        if "prompt" in values
        else keywords_prompt(
            text_parts(form.getlist("keywords"))
            + text_parts(form.getlist("keywords[]"))
        )
    )
    if prompt:
        get_prompt_ids = getattr(state.tokenizer, "get_prompt_ids", None)
        if get_prompt_ids is None or not whisper_pipeline:
            return _transcription_error(
                "prompt is not supported by this checkpoint's tokenizer",
                "prompt",
                "invalid_request_error",
            )
        generate_kwargs["prompt_ids"] = get_prompt_ids(prompt, return_tensors="pt").to(
            state.device
        )

    # Only verbose_json carries words. A word granularity on json/text/srt/vtt
    # cannot reach the client, so it must not pay for the extra alignment pass
    # (which is slow enough to time out on CPU).
    start_time = time.time()
    logger.info(f"[REQUEST] model={state.alias} endpoint=/v1/audio/transcriptions")
    word_granularity = "word" in granularities and response_format == "verbose_json"
    if word_granularity:
        # Word timestamps come from the expensive alignment pass; run it first
        # so its dedicated timeout still answers with the word-specific 504.
        try:
            word_result = await _run_transcription_pass(
                samples, generate_kwargs, "word", WORD_TIMESTAMP_TIMEOUT_S
            )
        except TimeoutError:
            # A bare TimeoutError stringifies to "", and the runner's
            # "[ERROR] (.+)" regex needs text after the tag, so the timeout is
            # logged explicitly: the instance must leave `generating`.
            logger.error(
                f"[ERROR] word-level timestamps did not finish within "
                f"{WORD_TIMESTAMP_TIMEOUT_S:g}s"
            )
            return _word_timeout_error(WORD_TIMESTAMP_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ERROR] {exc!s}")
            return _transcription_error(
                f"transcription failed: {exc}", None, "server_error", 500
            )
        # A word pass yields one chunk per word, which is not a segment. A
        # second ordinary pass supplies the model's own segment boundaries, and
        # each word is attached to the segment that contains it.
        try:
            result = await _run_transcription_pass(
                samples,
                generate_kwargs,
                True,
                TRANSCRIPTION_TIMEOUT_S,
                return_language=True,
            )
        except TimeoutError:
            logger.error(
                f"[ERROR] transcription did not finish within "
                f"{TRANSCRIPTION_TIMEOUT_S:g}s"
            )
            return _transcription_error(
                f"transcription did not finish within "
                f"{TRANSCRIPTION_TIMEOUT_S:g}s on the HuggingFace audio backend",
                None,
                "timeout",
                504,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[ERROR] {exc!s}")
            return _transcription_error(
                f"transcription failed: {exc}", None, "server_error", 500
            )

        # The runner derives the idle state from this line; an audio request
        # has no token count, so the field is zero rather than invented.
        elapsed_ms = (time.time() - start_time) * 1000
        logger.info(f"[COMPLETE] model={state.alias} tokens=0 time_ms={elapsed_ms:.1f}")

        text = str(result.get("text") or "").strip()
        segments = segments_from_chunks(result.get("chunks"), duration, text)
        words = words_from_chunks(word_result.get("chunks"))
        segments = attach_words_to_segments(segments, words)
        return {
            "task": "transcribe",
            "language": normalize_language(result.get("language")),
            "duration": duration,
            "text": text,
            "segments": segments,
            "words": words,
        }

    return_timestamps = response_format in ("verbose_json", "srt", "vtt")

    try:
        result = await _run_transcription_pass(
            samples,
            generate_kwargs,
            return_timestamps,
            TRANSCRIPTION_TIMEOUT_S,
            return_language=response_format == "verbose_json",
        )
    except TimeoutError:
        logger.error(
            f"[ERROR] transcription did not finish within "
            f"{TRANSCRIPTION_TIMEOUT_S:g}s"
        )
        return _transcription_error(
            f"transcription did not finish within {TRANSCRIPTION_TIMEOUT_S:g}s on the "
            "HuggingFace audio backend",
            None,
            "timeout",
            504,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[ERROR] {exc!s}")
        return _transcription_error(
            f"transcription failed: {exc}", None, "server_error", 500
        )

    # The runner derives the idle state from this line; an audio request has no
    # token count, so the field is zero rather than invented.
    elapsed_ms = (time.time() - start_time) * 1000
    logger.info(f"[COMPLETE] model={state.alias} tokens=0 time_ms={elapsed_ms:.1f}")

    text = str(result.get("text") or "").strip()
    segments = segments_from_chunks(result.get("chunks"), duration, text)

    if response_format == "json":
        return {"text": text}
    if response_format == "text":
        return PlainTextResponse(text)
    if response_format == "srt":
        return PlainTextResponse(
            render_cues(segments, False), media_type="application/x-subrip"
        )
    if response_format == "vtt":
        return PlainTextResponse(render_cues(segments, True), media_type="text/vtt")
    return {
        "task": "transcribe",
        "language": normalize_language(result.get("language")),
        "duration": duration,
        "text": text,
        "segments": segments,
    }


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="HuggingFace Model Server")
    parser.add_argument(
        "--model-id", required=True, help="HuggingFace model ID or path"
    )
    parser.add_argument(
        "--model-type",
        choices=["causal", "classification", "embedding", "vision", "audio"],
        required=True,
    )
    parser.add_argument("--alias", required=True, help="Model alias")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, required=True, help="Port to bind to")
    parser.add_argument(
        "--api-key",
        default="",
        help="API key for authentication (deprecated: prefer the SOLAR_API_KEY env var)",
    )
    parser.add_argument("--device", default="auto", help="Device: auto, cuda, mps, cpu")
    parser.add_argument(
        "--dtype", default="auto", help="Data type: auto, float16, bfloat16, float32"
    )
    parser.add_argument(
        "--max-length", type=int, default=4096, help="Max sequence length"
    )
    parser.add_argument("--labels", default=None, help="Comma-separated label names")
    parser.add_argument(
        "--trust-remote-code", action="store_true", help="Trust remote code"
    )
    parser.add_argument(
        "--use-flash-attention", action="store_true", help="Use Flash Attention 2"
    )
    parser.add_argument(
        "--normalize-embeddings",
        action="store_true",
        help="L2 normalize embedding vectors",
    )
    parser.add_argument(
        "--chunk-length-s",
        type=float,
        default=30.0,
        help="Chunk length for long-form audio transcription",
    )
    parser.add_argument(
        "--batch-size", type=int, default=8, help="Inference batch size"
    )
    parser.add_argument("--language", default=None, help="Language code (audio models)")
    return parser.parse_args(argv)


def main():
    args = parse_args()

    # Parse labels
    labels = None
    if args.labels:
        labels = [label.strip() for label in args.labels.split(",")]

    # Set API key
    state.api_key = resolve_api_key(args.api_key, os.environ.get("SOLAR_API_KEY", ""))

    # Load model
    state.load_model(
        model_id=args.model_id,
        model_type=args.model_type,
        alias=args.alias,
        device=args.device,
        dtype=args.dtype,
        max_length=args.max_length,
        labels=labels,
        trust_remote_code=args.trust_remote_code,
        use_flash_attention=args.use_flash_attention,
        normalize_embeddings=args.normalize_embeddings,
        chunk_length_s=args.chunk_length_s,
        batch_size=args.batch_size,
        language=args.language,
    )

    # Run server
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
