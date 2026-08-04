"""Shared exception-to-HTTP mapping for registration endpoints."""

from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

from fastapi import HTTPException

from app.exceptions import (
    ArtifactCategoryConflictError,
    ArtifactNotFoundInHarborError,
    HarborVerificationError,
    InvalidArtifactNameError,
    VersionAlreadyExistsError,
)


def handle_registration_errors(
    fn: Callable[..., Coroutine[Any, Any, Any]],
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Map domain exceptions from registration services to HTTP responses."""

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except InvalidArtifactNameError as exc:
            raise HTTPException(status_code=422, detail=exc.detail)
        except ArtifactNotFoundInHarborError as exc:
            raise HTTPException(status_code=404, detail=exc.detail)
        except HarborVerificationError as exc:
            raise HTTPException(status_code=502, detail=exc.detail)
        except (ArtifactCategoryConflictError, VersionAlreadyExistsError) as exc:
            raise HTTPException(status_code=409, detail=exc.detail)

    return wrapper
