"""whisper.cpp backend configuration model."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from solar_host.models.huggingface import _strip_api_key

_MODEL_FILE_DESCRIPTION = (
    "Model file selector inside the pulled artifact (filename, relative path or "
    "glob, e.g. 'ggml-tiny.bin'). Required for a pull source (repo:// or "
    "huggingface://) and when model names a directory; an explicit local file "
    "needs no selector."
)


class WhisperCppConfig(BaseModel):
    """Configuration for a whisper.cpp (whisper-server) instance.

    Note: api_key is NOT a config parameter - instances always use the host's API key.
    """

    model_config = ConfigDict(protected_namespaces=())

    @model_validator(mode="before")
    @classmethod
    def strip_api_key(cls, data: Any) -> Any:
        return _strip_api_key(data)

    backend_type: Literal["whispercpp"] = Field(
        default="whispercpp", description="Backend type identifier"
    )
    model_source: str | None = Field(
        default=None, description="Model source URI (e.g. huggingface://org/repo)"
    )
    model: str | None = Field(
        default=None,
        description="Whisper GGML model path (a file, or the pulled directory)",
    )
    model_file: str | None = Field(default=None, description=_MODEL_FILE_DESCRIPTION)
    file_filters: list[str] | None = Field(
        default=None,
        description=(
            "HuggingFace download filters (allow_patterns) applied when pulling "
            "the model, e.g. ['ggml-tiny.bin']"
        ),
    )

    @model_validator(mode="after")
    def check_model_or_source(self) -> "WhisperCppConfig":
        if not self.model and not self.model_source:
            raise ValueError("Either 'model' or 'model_source' must be provided")
        return self

    @model_validator(mode="after")
    def check_model_file_for_source(self) -> "WhisperCppConfig":
        """A pull or a directory model needs a selector; a file does not.

        A blank selector is as unusable as a missing one and answers with the
        same error, so a client sees one rule for the field.
        """
        message = self._missing_model_file_message()
        if self.model_file is not None and not self.model_file.strip():
            raise ValueError(message or "model_file is required and must not be blank")
        if not self.model_file and message is not None:
            raise ValueError(message)
        return self

    def _missing_model_file_message(self) -> str | None:
        """Why a selector is needed, or None when model already names a file."""
        if isinstance(self.model_source, str) and self.model_source.startswith(
            ("repo://", "huggingface://")
        ):
            return (
                "model_file is required when model_source is set; the pull "
                "returns a directory"
            )
        if self.model and Path(self.model).is_dir():
            return (
                "model_file is required when model is a directory; select the "
                "GGML file inside it"
            )
        return None

    alias: str = Field(..., description="Model alias (e.g. whisper-tiny)")
    language: str = Field(
        default="auto",
        description="Spoken language code, or 'auto' for whisper.cpp language detection",
    )
    threads: int = Field(default=4, ge=1, description="CPU threads for decoding")
    device: Literal["auto", "cpu"] = Field(
        default="auto",
        description=(
            "auto uses the build's default device; cpu forces --no-gpu. "
            "whisper-server has no enforceable cuda/mps selector."
        ),
    )
    host: str = Field(default="0.0.0.0", description="Host to bind to")
    port: int | None = Field(
        default=None, description="Port (auto-assigned if not specified)"
    )
