"""Domain exceptions for the data-repository service.

These are HTTP-agnostic; callers (route handlers) are responsible for mapping
them to appropriate HTTP status codes.
"""


class InvalidArtifactNameError(Exception):
    """Raised when an artifact name fails format or length validation."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ArtifactNotFoundInHarborError(Exception):
    """Raised when a Harbor artifact reference cannot be resolved."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class HarborVerificationError(Exception):
    """Raised when Harbor returns an auth, connection, or API-level error."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ArtifactCategoryConflictError(Exception):
    """Raised when an artifact already exists with a different category."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class VersionAlreadyExistsError(Exception):
    """Raised when the requested version already exists for the artifact."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ModelNotFoundError(Exception):
    """Raised when a model artifact with the requested name does not exist."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ModelVersionNotFoundError(Exception):
    """Raised when the requested model version does not exist."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class DatasetNotFoundError(Exception):
    """Raised when a dataset artifact with the requested name does not exist."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class DatasetVersionNotFoundError(Exception):
    """Raised when the requested dataset version does not exist."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
