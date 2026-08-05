"""Artifact upload API routes (under /api/uploads) — S-047.

Five endpoints from spec §4.2:

- ``POST /api/uploads`` — validate + pre-flight conflict check, create session
- ``PUT /api/uploads/{id}/files?path=<rel>`` — stream one file into Harbor
- ``GET /api/uploads/{id}`` — session status with per-file progress
- ``POST /api/uploads/{id}/complete`` — push manifest + register version
- ``DELETE /api/uploads/{id}`` — abort the session

The file PUT reads the request body with ``Request.stream()`` so FastAPI
never materialises it; the OCI push client forwards it to Harbor in fixed
chunks while computing the sha256 (peak memory: one chunk).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request

from app.models.uploads import (
    CompleteUploadResponse,
    CreateUploadRequest,
    CreateUploadResponse,
    UploadFileResult,
    UploadStatusResponse,
)
from app.services.uploads import build_upload_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("", status_code=201, response_model=CreateUploadResponse)
async def create_upload(request: CreateUploadRequest) -> CreateUploadResponse:
    """Validate the request and create an upload session."""
    return await build_upload_service().create(request)


@router.put("/{upload_id}/files", response_model=UploadFileResult)
async def put_file(
    upload_id: str,
    request: Request,
    path: str = Query(..., description="Artifact-relative POSIX path of the file"),
) -> UploadFileResult:
    """Stream one declared file's bytes into Harbor."""
    # Request.stream() yields the body in chunks without buffering it;
    # request.body() would materialise the whole file in memory.
    return await build_upload_service().put_file(
        upload_id,
        path,
        request.stream(),
    )


@router.get("/{upload_id}", response_model=UploadStatusResponse)
async def get_status(upload_id: str) -> UploadStatusResponse:
    """Return session state and per-file progress."""
    return await build_upload_service().get_status(upload_id)


@router.post("/{upload_id}/complete", response_model=CompleteUploadResponse)
async def complete_upload(upload_id: str) -> CompleteUploadResponse:
    """Push the manifest and register the version with the Data Repository."""
    return await build_upload_service().complete(upload_id)


@router.delete("/{upload_id}", status_code=204)
async def abort_upload(upload_id: str) -> None:
    """Abort the session; subsequent file uploads are rejected with 409."""
    await build_upload_service().abort(upload_id)
