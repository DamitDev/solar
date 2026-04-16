"""Pydantic schemas for dataset version registration and lookup."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DatasetMetadata(BaseModel):
    description: str | None = Field(
        None,
        description="Optional dataset description for this version",
    )
    format: Literal["parquet", "hdf5", "json"] | None = Field(
        None,
        description="Optional dataset format",
    )


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
    metadata: DatasetMetadata | None = Field(
        None,
        description="Optional version metadata (description, format)",
    )


class RegisterDatasetVersionResponse(BaseModel):
    name: str
    version: str
    harbor_ref: str
    category: str


class GetDatasetVersionResponse(BaseModel):
    name: str
    version: str
    category: str
    harbor_ref: str
    size_bytes: int | None
    checksum: str | None
    created_at: datetime
    metadata: dict[str, Any]
