"""The gateway request log's request type for every endpoint (#131)."""

import pytest

from app.database.logs import classify_request_type


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("/v1/audio/transcriptions", "transcription"),
        ("/v1/chat/completions", "chat"),
        ("/v1/completions", "completion"),
        ("/v1/embeddings", "embedding"),
        ("/v1/classify", "classification"),
        ("/v1/rerank", "rerank"),
        ("/v1/tokenize", "tokenize"),
        ("/v1/detokenize", "detokenize"),
        ("/v1/audio/translations", "unknown"),
        (None, "unknown"),
    ],
)
def test_classify_request_type(endpoint, expected):
    assert classify_request_type(endpoint) == expected


def test_the_audio_route_is_not_mistaken_for_a_completion():
    """``/v1/audio/transcriptions`` contains no ``/completions``, but pin the
    order anyway: a broad prefix check must not shadow the audio route."""
    assert classify_request_type("/v1/audio/transcriptions") == "transcription"
