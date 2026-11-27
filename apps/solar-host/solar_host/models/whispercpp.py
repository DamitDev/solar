"""whisper.cpp backend configuration model."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from solar_host.models.huggingface import _strip_api_key

_MODEL_FILE_DESCRIPTION = (
    "Model file selector inside the pulled artifact (filename, relative path or "
    "glob, e.g. 'ggml-tiny.bin'). Required when model_source is set: a "
    "HuggingFace pull returns the snapshot directory, so the GGML file has to be "
    "selected from it. Optional when model already points at an explicit file."
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
        """A pull returns a directory, so the GGML file must be selected from it.

        A blank selector is as unusable as a missing one and answers with the
        same error, so a client sees one rule for the field.
        """
        if self.model_file is not None and not self.model_file.strip():
            raise ValueError(
                "model_file is required when model_source is set; the pull "
                "returns a directory"
            )
        if self.model_source and not self.model_file:
            raise ValueError(
                "model_file is required when model_source is set; the pull "
                "returns a directory"
            )
        return self

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
