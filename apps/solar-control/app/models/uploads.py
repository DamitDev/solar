"""Pydantic models for the artifact upload relay (S-047)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

UploadCategory = Literal["model", "dataset"]


class UploadFileDeclaration(BaseModel):
    """One file declared at session creation (spec §4.2)."""

    path: str
    size: int = Field(ge=0)


class CreateUploadRequest(BaseModel):
    """POST /api/uploads body."""

    category: UploadCategory
    name: str
    version: str | None = None
    files: list[UploadFileDeclaration] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateUploadResponse(BaseModel):
    """POST /api/uploads response."""

    upload_id: str
    harbor_ref: str
    name: str
    version: str
    expires_at: datetime


class UploadFileResult(BaseModel):
    """PUT /api/uploads/{id}/files response."""

    path: str
    digest: str
    size: int


class UploadFileStatus(BaseModel):
    """One file's progress within a session."""

    path: str
    size: int
    digest: str | None = None
    uploaded: bool = False


class UploadStatusResponse(BaseModel):
    """GET /api/uploads/{id} response."""

    upload_id: str
    state: str
    files: list[UploadFileStatus]
    bytes_total: int
    bytes_done: int


class CompleteUploadResponse(BaseModel):
    """POST /api/uploads/{id}/complete response."""

    name: str
    version: str
    category: str
    harbor_ref: str
    size_bytes: int
    registration: dict[str, Any]
