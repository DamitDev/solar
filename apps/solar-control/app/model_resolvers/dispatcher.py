from .huggingface import resolve_huggingface
from .parser import HuggingFaceURI, LocalURI, RepoURI, parse
from .repo import resolve_repo


async def resolve(
    uri_str: str,
    host_url: str,
    host_api_key: str,
    backend_type: str | None = None,
    file_filters: list[str] | None = None,
) -> str:
    """
    Parses a URI and dispatches to the correct resolver.
    Returns a resolved local:// URI.

    ``backend_type`` is forwarded to the host pull for ``repo://`` URIs so
    llama.cpp artifacts resolve to their largest ``*.gguf`` (the host needs
    a file, not a directory).  ``local://`` and ``huggingface://`` are never
    affected.

    ``file_filters`` only applies to ``huggingface://`` URIs, where it limits
    the downloaded snapshot to matching files.  ORAS cannot filter an artifact.
    """
    parsed = parse(uri_str)

    if isinstance(parsed, LocalURI):
        # local:// is passed through to the host to be validated there
        return uri_str

    elif isinstance(parsed, HuggingFaceURI):
        return await resolve_huggingface(
            parsed, uri_str, host_url, host_api_key, file_filters
        )

    elif isinstance(parsed, RepoURI):
        return await resolve_repo(uri_str, host_url, host_api_key, backend_type)

    else:
        # Should not happen if parser is correct
        return uri_str
