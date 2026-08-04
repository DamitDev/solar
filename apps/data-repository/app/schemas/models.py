"""Pydantic schemas for model version registration and lookup."""

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ArtifactReference = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9][a-z0-9._-]{0,254}:[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    ),
]


class RegisterModelVersionRequest(BaseModel):
    harbor_ref: str = Field(
        ...,
        min_length=1,
        description="Full OCI reference in Harbor (e.g. imgrepo.damit.hu/supernova/iris-osl:v3)",
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
        description="Optional version metadata (training_config, eval_metrics, lineage, etc.)",
    )


class RegisterModelVersionResponse(BaseModel):
    name: str
    version: str
    harbor_ref: str
    category: str


class ModelVersionListItem(BaseModel):
    version: str
    harbor_ref: str
    created_at: datetime
    size_bytes: int | None
    checksum: str | None
    training_config: dict[str, Any] | None
    eval_metrics: dict[str, Any] | None


class ListModelVersionsResponse(BaseModel):
    versions: list[ModelVersionListItem]


class GetModelVersionResponse(BaseModel):
    name: str
    version: str
    category: str
    harbor_ref: str
    size_bytes: int | None
    checksum: str | None
    created_at: datetime
    metadata: dict[str, Any]


class LineageMetadata(BaseModel):
    source_trainer: str | None = Field(
        None,
        description="SuperNova training job ID that produced this version (opaque string; not an artifact name:version reference).",
    )
    source_dataset: ArtifactReference | None = Field(
        None,
        description="Source dataset artifact as name:version (must exist when validated on PUT).",
    )
    parent_model: ArtifactReference | None = Field(
        None,
        description="Parent model artifact as name:version (must exist when validated on PUT).",
    )


class GetModelMetadataResponse(BaseModel):
    name: str
    category: str
    description: str | None
    training_config: dict[str, Any] | None
    eval_metrics: dict[str, Any] | None
    lineage: LineageMetadata | None
    created_at: datetime
    versions_count: int


class UpdateModelMetadataRequest(BaseModel):
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


class UpdateModelVersionRequest(BaseModel):
    metadata: dict[str, Any] = Field(
        ..., description="Partial or full update of the metadata JSONB object."
    )


class UpdateModelVersionResponse(BaseModel):
    name: str
    version: str
    updated_at: datetime
    metadata: dict[str, Any]
    status: str = "updated"
    model_config = ConfigDict(from_attributes=True)
