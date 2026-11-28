"""NeMo-Speech.cpp backend configuration model."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from solar_host.models.huggingface import _strip_api_key

_MODEL_FILE_DESCRIPTION = (
    "Decoder GGUF selector inside the pulled artifact (filename, relative "
    "path or glob). Required for a pull source (repo:// or huggingface://) and "
    "when model names a directory; an explicit local file needs no selector."
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
                "decoder GGUF inside it"
            )
        return None

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
