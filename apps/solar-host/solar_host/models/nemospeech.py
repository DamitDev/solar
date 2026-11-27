"""NeMo-Speech.cpp backend configuration model."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from solar_host.models.huggingface import _strip_api_key

_MODEL_FILE_DESCRIPTION = (
    "Decoder GGUF selector inside the pulled artifact (filename, relative path "
    "or glob). Required when model_source is set: a HuggingFace pull returns "
    "the snapshot directory, so the decoder has to be selected from it."
)
_SIDECAR_DESCRIPTION = (
    "Optional sidecar GGUF (SALM encoder or Sortformer diarizer). A bare "
    "filename is resolved inside the pulled artifact, like model_file; an "
    "explicit path is used verbatim."
)


class NemoSpeechConfig(BaseModel):
    """Configuration for a NeMo-Speech.cpp (nemo-speech serve) instance.

    Note: api_key is NOT a config parameter - instances always use the host's API key.
    """

    model_config = ConfigDict(protected_namespaces=())

    @model_validator(mode="before")
    @classmethod
    def strip_api_key(cls, data: Any) -> Any:
        return _strip_api_key(data)

    backend_type: Literal["nemospeech"] = Field(
        default="nemospeech", description="Backend type identifier"
    )
    model_source: str | None = Field(
        default=None, description="Model source URI (e.g. huggingface://org/repo)"
    )
    model: str | None = Field(
        default=None, description="Decoder GGUF path (a file, or the pulled directory)"
    )
    model_file: str | None = Field(default=None, description=_MODEL_FILE_DESCRIPTION)
    encoder: str | None = Field(
        default=None,
        description=(
            "Optional SALM encoder override. Empty means the encoder file the "
            "decoder names next to itself."
        ),
    )
    instruction: str | None = Field(
        default=None,
        description=(
            "Optional style instruction (verbatim vs written form, vocabulary "
            "hints); passed to the runtime as --instruction"
        ),
    )
    translate_to: str | None = Field(
        default=None,
        description=(
            "Optional target language for translation; passed to the runtime as "
            "--translate-to"
        ),
    )
    diar_model: str | None = Field(
        default=None,
        description=(
            "Optional Sortformer diarizer GGUF, passed as --diar-model. "
            + _SIDECAR_DESCRIPTION
        ),
    )
    file_filters: list[str] | None = Field(
        default=None,
        description=(
            "HuggingFace download filters (allow_patterns) applied when pulling "
            "the model, e.g. ['*.gguf']"
        ),
    )

    @model_validator(mode="after")
    def check_model_or_source(self) -> "NemoSpeechConfig":
        if not self.model and not self.model_source:
            raise ValueError("Either 'model' or 'model_source' must be provided")
        return self

    @model_validator(mode="after")
    def check_model_file_for_source(self) -> "NemoSpeechConfig":
        """A pull returns a directory, so the decoder must be selected from it.

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

    @model_validator(mode="after")
    def check_diarizer(self) -> "NemoSpeechConfig":
        """Diarization needs a Sortformer model; without one it cannot run."""
        if self.diarize and not (self.diar_model and self.diar_model.strip()):
            raise ValueError("a diarizer model is required for diarization")
        return self

    alias: str = Field(..., description="Model alias (e.g. nemotron-stream-en)")
    language: str | None = Field(
        default=None, description="Language code; omit for the model default"
    )
    diarize: bool = Field(
        default=False,
        description=(
            "Tag words with speakers. Speakers appear only in verbose_json and "
            "diarized_json; the other formats are answered without the runtime "
            "diarization flag."
        ),
    )
    device: Literal["auto", "cpu", "cuda"] = Field(
        default="auto",
        description=(
            "auto picks cuda on an NVIDIA host and cpu otherwise; the value is "
            "passed to nemo-speech as --device"
        ),
    )
    host: str = Field(default="0.0.0.0", description="Host to bind to")
    port: int | None = Field(
        default=None, description="Port (auto-assigned if not specified)"
    )
