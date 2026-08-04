"""Pydantic schemas for dataset registration, lookup, and metadata."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, ConfigDict

from app.schemas.artifacts import ArtifactListResponse, ArtifactSummary
from app.schemas.models import LineageMetadata

_ALLOWED_DATASET_FORMATS = {"parquet", "hdf5", "json"}

__all__ = [
    "ArtifactListResponse",
    "ArtifactSummary",
    "LineageMetadata",
    "GetDatasetMetadataResponse",
    "UpdateDatasetMetadataRequest",
]


class RegisterDatasetVersionRequest(BaseModel):
    harbor_ref: str = Field(
        ...,
        min_length=1,
        description="Full OCI reference in Harbor (e.g. imgrepo.damit.hu/supernova/iris-tickets:2026-03)",
    )
    version: str | None = Field(
        None,
        description="Version string (e.g. v1, 2026-03); auto-incremented as vN if omitted",
    )
    checksum: str | None = Field(
        None,
        description="OCI manifest digest (e.g. sha256:abc123...); resolved from Harbor if omitted",
    )
    size_bytes: int | None = Field(
        None,
        description="Artifact size in bytes; resolved from Harbor if omitted",
    )
    metadata: dict[str, Any] | None = Field(
        None,
        description="Optional dataset version metadata (format, record_count, columns, etc.).",
    )

    @field_validator("metadata")
    @classmethod
    def validate_dataset_format(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None

        data_format = value.get("format")
        if data_format is None:
            return value

        if not isinstance(data_format, str):
            raise ValueError("metadata.format must be a string")

        if data_format not in _ALLOWED_DATASET_FORMATS:
            allowed = ", ".join(sorted(_ALLOWED_DATASET_FORMATS))
            raise ValueError(f"metadata.format must be one of: {allowed}")

        return value


class RegisterDatasetVersionResponse(BaseModel):
    name: str
    version: str
    harbor_ref: str
    category: str


class DatasetVersionListItem(BaseModel):
    version: str
    harbor_ref: str
    created_at: datetime
    size_bytes: int | None
    checksum: str | None


class ListDatasetVersionsResponse(BaseModel):
    versions: list[DatasetVersionListItem]


class GetDatasetVersionResponse(BaseModel):
    name: str
    version: str
    category: str
    harbor_ref: str
    size_bytes: int | None
    checksum: str | None
    created_at: datetime
    metadata: dict[str, Any]


class GetDatasetMetadataResponse(BaseModel):
    name: str
    category: str
    description: str | None
    training_config: dict[str, Any] | None
    eval_metrics: dict[str, Any] | None
    lineage: LineageMetadata | None
    created_at: datetime
    versions_count: int


class UpdateDatasetMetadataRequest(BaseModel):
    description: str | None = Field(
        None,
        description="Artifact-level description; null removes the description.",
    )
    training_config: dict[str, Any] | None = Field(
        None,
        description="Top-level metadata.training_config object; null removes it.",
    )
    eval_metrics: dict[str, Any] | None = Field(
        None,
        description="Top-level metadata.eval_metrics object; null removes it.",
    )
    lineage: LineageMetadata | None = Field(
        None,
        description="Top-level metadata.lineage object; null removes it.",
    )


class UpdateDatasetVersionRequest(BaseModel):
    metadata: dict[str, Any] = Field(
        ..., description="Partial or full update of the dataset metadata JSONB object."
    )


class UpdateDatasetVersionResponse(BaseModel):
    name: str
    version: str
    updated_at: datetime
    metadata: dict[str, Any]
    status: str = "updated"
    model_config = ConfigDict(from_attributes=True)
