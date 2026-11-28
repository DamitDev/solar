"""Shared parsing for the OpenAI optional transcription fields.

``keywords`` and ``languages`` are arrays in the OpenAI contract, but the audio
shim and the HuggingFace server receive them as multipart parts. Clients spell an
array in four different ways, so every spelling has to collapse to the same list:

- one part per entry: ``keywords=a&keywords=b``
- the array spelling: ``keywords[]=a&keywords[]=b``
- a JSON array in one part: ``keywords=["a", "b"]``
- a comma-separated part: ``keywords=a,b``

Both fields are decoder hints rather than model changes: the runtimes already take a
single ``language`` and a ``prompt`` (or, for NeMo-Speech.cpp, ``speech_contexts``),
so the parsed lists are mapped onto those existing fields by the callers.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

# NeMo-Speech.cpp documents ``speech_contexts`` as
# ``[{"phrases": ["..."], "boost": N}]`` and its ``prompt`` field as one boosted
# phrase at boost 10, so a keyword list maps onto the same default.
DEFAULT_KEYWORD_BOOST = 10.0

# Fields the OpenAI contract lists as optional. ``keywords`` and ``languages`` are
# mapped onto the decoder-hint fields every runtime already takes (see the shim's
# _forward_fields): keywords -> prompt, or NeMo's speech_contexts; a one-entry
# languages set -> language. ``chunking_strategy`` and ``include[]`` are accepted
# and dropped because no runtime implements them; see the README.
OPENAI_OPTIONAL_FIELDS: tuple[str, ...] = (
    "keywords",
    "languages",
    "chunking_strategy",
    "include[]",
)

# The array spellings of the two mapped fields; a client may send either.
OPENAI_ARRAY_FIELDS: tuple[str, ...] = (
    "keywords[]",
    "languages[]",
)

# whisper-server-only tuning fields. Forwarded for whisper.cpp, accepted and
# ignored for NeMo-Speech.cpp (which would reject an unknown key). `vad` is
# accepted but never forwarded when false; `vad=true` is refused with a 400
# because no VAD model is configurable on a shim instance, and whisper-server
# fails with a bare 500 when asked to use one.
WHISPER_ONLY_FIELDS: tuple[str, ...] = (
    "temperature_inc",
    "carry_initial_prompt",
    "suppress_non_speech",
    "vad",
)

# NeMo-Speech.cpp-only fields. Forwarded for NeMo, accepted and ignored for
# whisper.cpp.
NEMO_ONLY_FIELDS: tuple[str, ...] = (
    "diarization",
    "automatic_punctuation",
    "verbatim",
    "profanity_filter",
    "speech_contexts",
    "max_speaker_count",
)

# Every field the transcription contract accepts, shared by the audio shim and the
# HuggingFace audio route so an unknown field is refused identically by all three
# backends. A backend-specific field is accepted and dropped by a runtime that does
# not own it, so one client request works on all three.
ACCEPTED_FIELDS: frozenset[str] = frozenset(
    {
        "file",
        "model",
        "language",
        "prompt",
        "response_format",
        "temperature",
        "timestamp_granularities[]",
        "timestamp_granularities",
        "stream",
        *OPENAI_OPTIONAL_FIELDS,
        *OPENAI_ARRAY_FIELDS,
        *WHISPER_ONLY_FIELDS,
        *NEMO_ONLY_FIELDS,
    }
)


def split_list_part(part: str) -> list[str]:
    """Split one multipart part into the list entries it carries.

    A JSON array (``["a", "b"]``) is parsed, any other value is split on commas.
    A part that starts with ``[`` but is not valid JSON stays a literal phrase.
    """
    text = part.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        if isinstance(parsed, list):
            return [str(entry).strip() for entry in parsed if str(entry).strip()]
        return [text]
    return [piece.strip() for piece in text.split(",") if piece.strip()]


def collect_list(parts: Iterable[str]) -> list[str]:
    """Every entry carried by the parts, in order, without duplicates."""
    entries: list[str] = []
    for part in parts:
        for entry in split_list_part(part):
            if entry not in entries:
                entries.append(entry)
    return entries


def text_parts(parts: Iterable[object]) -> list[str]:
    """The string parts of a Starlette ``getlist`` result (drops uploads)."""
    return [part for part in parts if isinstance(part, str)]


def single_language(parts: Iterable[str]) -> str | None:
    """The language to forward when the request names exactly one."""
    languages = collect_list(parts)
    return languages[0] if len(languages) == 1 else None


def keywords_prompt(parts: Iterable[str]) -> str | None:
    """The prompt text for a keyword list, or None when it carries nothing."""
    keywords = collect_list(parts)
    return ", ".join(keywords) if keywords else None


def speech_contexts_json(
    phrases: Iterable[str], boost: float = DEFAULT_KEYWORD_BOOST
) -> str:
    """NeMo-Speech.cpp's word-boosting payload for a keyword list."""
    return json.dumps([{"phrases": list(phrases), "boost": boost}])
