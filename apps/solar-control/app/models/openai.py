from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelInfo(BaseModel):
    """Model information for OpenAI compatibility.

    The ``capabilities`` field is forwarded from llama.cpp / hf_server upstream
    so downstream apps can differentiate text vs multimodal models without
    trial-and-error. Typical values: ``["completion"]``,
    ``["completion", "multimodal"]``, ``["embedding"]``, ``["classification"]``.
    """

    id: str
    object: str = "model"
    created: int = Field(
        default_factory=lambda: int(datetime.now(timezone.utc).timestamp())
    )
    owned_by: str = "solar"
    capabilities: list[str] | None = None


class ModelsResponse(BaseModel):
    """OpenAI /v1/models response.

    Includes both the Ollama-style ``models`` array and the OpenAI-style
    ``data`` array, mirroring llama.cpp's response shape.
    """

    object: str = "list"
    models: list[dict[str, Any]] = Field(default_factory=list)
    data: list[ModelInfo]


class ChatMessage(BaseModel):
    """Chat message"""

    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None
    name: str | None = None


class StreamOptions(BaseModel):
    """OpenAI stream_options for including usage in streaming responses"""

    model_config = ConfigDict(extra="allow")

    include_usage: bool | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI chat completion request"""

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stream: bool | None = False
    stream_options: StreamOptions | None = None
    n: int | None = None
    stop: list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    logit_bias: dict[str, float] | None = None
    user: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None


class CompletionRequest(BaseModel):
    """OpenAI completion request"""

    model_config = ConfigDict(extra="allow")

    model: str
    prompt: str
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool | None = False
    n: int | None = None
    stop: list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    logit_bias: dict[str, float] | None = None
    user: str | None = None


class ProxyRequest(BaseModel):
    """Generic proxy request"""

    endpoint: str
    method: str = "POST"
    data: dict[str, Any] | None = None


class ClassifyRequest(BaseModel):
    """Classification request for HuggingFace classification models"""

    model_config = ConfigDict(extra="allow")

    model: str
    input: Any = Field(..., description="Text or list of texts to classify")
    return_all_scores: bool = Field(
        default=False,
        description="Return scores for all classes, not just top prediction",
    )


class ClassifyScoreItem(BaseModel):
    """Individual class score."""

    label: str
    score: float


class ClassifyChoice(BaseModel):
    """Classification result for a single input"""

    index: int
    label: str
    score: float
    all_scores: list[ClassifyScoreItem] | None = Field(
        default=None, description="Scores for all classes (when return_all_scores=True)"
    )


class ClassifyResponse(BaseModel):
    """Classification response"""

    id: str
    object: str = "classification"
    model: str
    choices: list[ClassifyChoice]
    usage: dict[str, int]


class EmbeddingRequest(BaseModel):
    """OpenAI-compatible embedding request for HuggingFace embedding models"""

    model_config = ConfigDict(extra="allow")

    model: str
    input: Any = Field(..., description="Text or list of texts to embed")
    encoding_format: str | None = Field(
        default="float", description="Encoding format: 'float' or 'base64'"
    )
    dimensions: int | None = Field(
        default=None, description="Optional dimension truncation"
    )


class EmbeddingData(BaseModel):
    """Individual embedding result"""

    object: str = "embedding"
    embedding: list[float]
    index: int


class EmbeddingResponse(BaseModel):
    """OpenAI-compatible embedding response"""

    object: str = "list"
    data: list[EmbeddingData]
    model: str
    usage: dict[str, int]


# OpenAI response formats for POST /v1/audio/transcriptions. diarized_json is in
# scope for NeMo-Speech.cpp only; the other runtimes refuse it explicitly.
AUDIO_RESPONSE_FORMATS: tuple[str, ...] = (
    "json",
    "text",
    "srt",
    "verbose_json",
    "vtt",
    "diarized_json",
)

TIMESTAMP_GRANULARITIES: frozenset[str] = frozenset({"word", "segment"})


class TranscriptionRequest(BaseModel):
    """OpenAI-compatible transcription request (multipart form fields).

    The ``file`` part stays in the route: it is a binary upload, not a field of
    this model. ``keywords`` and ``languages`` are part of the OpenAI contract
    and are forwarded to the shim, which maps them onto the runtime's prompt and
    language fields; ``chunking_strategy`` and ``include[]`` are accepted and
    dropped there because no runtime implements them.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    language: str | None = None
    prompt: str | None = None
    response_format: str = "json"
    temperature: float | None = None
    timestamp_granularities: list[str] | None = None
    stream: bool = False

    @field_validator("model")
    @classmethod
    def check_model(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("model is required")
        return value

    @field_validator("response_format")
    @classmethod
    def check_response_format(cls, value: str) -> str:
        if value not in AUDIO_RESPONSE_FORMATS:
            raise ValueError(
                f"response_format '{value}' is not supported; must be one of: "
                f"{', '.join(AUDIO_RESPONSE_FORMATS)}"
            )
        return value

    @field_validator("temperature")
    @classmethod
    def check_temperature(cls, value: float | None) -> float | None:
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError("temperature must be between 0 and 1")
        return value

    @field_validator("timestamp_granularities")
    @classmethod
    def check_granularities(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            for entry in value:
                if entry not in TIMESTAMP_GRANULARITIES:
                    raise ValueError(
                        "timestamp_granularities must contain only 'word' or 'segment'"
                    )
        return value

    @field_validator("stream")
    @classmethod
    def check_stream(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "streaming transcription is not supported by this endpoint; "
                "omit 'stream' or set it to false"
            )
        return value

    @classmethod
    def from_form(cls, form: Any) -> "TranscriptionRequest":
        """Build the request from a parsed multipart form.

        Repeated ``timestamp_granularities[]`` fields survive because the form is
        read with ``getlist``; an empty value is dropped so the defaults apply.
        """
        fields: dict[str, Any] = {}
        for name in (
            "model",
            "language",
            "prompt",
            "response_format",
            "temperature",
            "stream",
        ):
            value = form.get(name)
            if value is not None:
                fields[name] = value
        granularities = form.getlist("timestamp_granularities[]") or form.getlist(
            "timestamp_granularities"
        )
        if granularities:
            fields["timestamp_granularities"] = granularities
        return cls(**fields)


class RerankRequest(BaseModel):
    """OpenAI-compatible rerank request for reranker models"""

    model_config = ConfigDict(extra="allow")

    model: str
    query: str = Field(..., description="Query text to rank documents against")
    documents: list[str] = Field(..., description="List of documents to rerank")
    top_n: int | None = Field(
        default=None, description="Number of top results to return (default: all)"
    )
    return_documents: bool | None = Field(
        default=True, description="Whether to return document text in results"
    )


class RerankResult(BaseModel):
    """Individual rerank result"""

    index: int
    document: str | None = None
    relevance_score: float


class RerankResponse(BaseModel):
    """OpenAI-compatible rerank response"""

    id: str
    results: list[RerankResult]
    model: str
    usage: dict[str, int]
