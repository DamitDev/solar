"""Pydantic schemas for model version registration and lookup."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


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
