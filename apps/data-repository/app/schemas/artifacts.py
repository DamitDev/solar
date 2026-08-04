"""Shared Pydantic schemas for cross-cutting artifact catalog responses."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from app.types import ArtifactCategory

T = TypeVar("T")


class ArtifactSummary(BaseModel):
    """Summary of an artifact for list endpoints."""

    name: str
    category: ArtifactCategory
    description: str | None = None
    versions_count: int
    latest_version: str | None = None
    created_at: datetime


class ArtifactListResponse(BaseModel, Generic[T]):
    """Paginated list response for artifacts."""

    total: int = Field(..., ge=0)
    items: list[T]


class ResolveUriResponse(BaseModel):
    """Response for URI resolution."""

    category: ArtifactCategory
    name: str
    version: str
    harbor_ref: str
    size_bytes: int | None
    checksum: str | None
    metadata: dict[str, Any]
    created_at: datetime
