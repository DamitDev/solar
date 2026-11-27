"""Shared response formatting for the audio transcription endpoints.

Both ``hf_server`` (the HuggingFace audio mode) and ``audio_server`` (the shim in
front of the GGML runtimes) answer ``POST /v1/audio/transcriptions``. The formats
that are translated host-side (SRT, WebVTT, verbose segments) live here so the two
servers cannot drift apart.
"""

from typing import Any

RESPONSE_FORMATS: tuple[str, ...] = (
    "json",
    "text",
    "srt",
    "verbose_json",
    "vtt",
    "diarized_json",
)

# The media type each response format is served with. NeMo-Speech.cpp answers
# srt/vtt as text/plain from its own route, so the shim must not pass that
# through: a client chooses a subtitle format by its media type.
FORMAT_MEDIA_TYPES: dict[str, str] = {
    "json": "application/json",
    "text": "text/plain",
    "srt": "application/x-subrip",
    "verbose_json": "application/json",
    "vtt": "text/vtt",
    "diarized_json": "application/json",
}

# whisper.cpp reports the detected language as an English name ("hungarian"), and
# NeMo-Speech.cpp reports an empty string when no language was requested. The
# OpenAI contract wants the ISO-639-1 code, so both are normalised through this
# table. It holds the whisper.cpp language list (the superset all three runtimes
# share), keyed by both the code and the full name.
LANGUAGE_CODES: dict[str, str] = {
    "en": "en",
    "english": "en",
    "zh": "zh",
    "chinese": "zh",
    "de": "de",
    "german": "de",
    "es": "es",
    "spanish": "es",
    "ru": "ru",
    "russian": "ru",
    "ko": "ko",
    "korean": "ko",
    "fr": "fr",
    "french": "fr",
    "ja": "ja",
    "japanese": "ja",
    "pt": "pt",
    "portuguese": "pt",
    "tr": "tr",
    "turkish": "tr",
    "pl": "pl",
    "polish": "pl",
    "ca": "ca",
    "catalan": "ca",
    "nl": "nl",
    "dutch": "nl",
    "ar": "ar",
    "arabic": "ar",
    "sv": "sv",
    "swedish": "sv",
    "it": "it",
    "italian": "it",
    "id": "id",
    "indonesian": "id",
    "hi": "hi",
    "hindi": "hi",
    "fi": "fi",
    "finnish": "fi",
    "vi": "vi",
    "vietnamese": "vi",
    "he": "he",
    "hebrew": "he",
    "uk": "uk",
    "ukrainian": "uk",
    "el": "el",
    "greek": "el",
    "ms": "ms",
    "malay": "ms",
    "cs": "cs",
    "czech": "cs",
    "ro": "ro",
    "romanian": "ro",
    "da": "da",
    "danish": "da",
    "hu": "hu",
    "hungarian": "hu",
    "ta": "ta",
    "tamil": "ta",
    "no": "no",
    "norwegian": "no",
    "th": "th",
    "thai": "th",
    "ur": "ur",
    "urdu": "ur",
    "hr": "hr",
    "croatian": "hr",
    "bg": "bg",
    "bulgarian": "bg",
    "lt": "lt",
    "lithuanian": "lt",
    "la": "la",
    "latin": "la",
    "mi": "mi",
    "maori": "mi",
    "ml": "ml",
    "malayalam": "ml",
    "cy": "cy",
    "welsh": "cy",
    "sk": "sk",
    "slovak": "sk",
    "te": "te",
    "telugu": "te",
    "fa": "fa",
    "persian": "fa",
    "lv": "lv",
    "latvian": "lv",
    "bn": "bn",
    "bengali": "bn",
    "sr": "sr",
    "serbian": "sr",
    "az": "az",
    "azerbaijani": "az",
    "sl": "sl",
    "slovenian": "sl",
    "kn": "kn",
    "kannada": "kn",
    "et": "et",
    "estonian": "et",
    "mk": "mk",
    "macedonian": "mk",
    "br": "br",
    "breton": "br",
    "eu": "eu",
    "basque": "eu",
    "is": "is",
    "icelandic": "is",
    "hy": "hy",
    "armenian": "hy",
    "ne": "ne",
    "nepali": "ne",
    "mn": "mn",
    "mongolian": "mn",
    "bs": "bs",
    "bosnian": "bs",
    "kk": "kk",
    "kazakh": "kk",
    "sq": "sq",
    "albanian": "sq",
    "sw": "sw",
    "swahili": "sw",
    "gl": "gl",
    "galician": "gl",
    "mr": "mr",
    "marathi": "mr",
    "pa": "pa",
    "punjabi": "pa",
    "si": "si",
    "sinhala": "si",
    "km": "km",
    "khmer": "km",
    "sn": "sn",
    "shona": "sn",
    "yo": "yo",
    "yoruba": "yo",
    "so": "so",
    "somali": "so",
    "af": "af",
    "afrikaans": "af",
    "oc": "oc",
    "occitan": "oc",
    "ka": "ka",
    "georgian": "ka",
    "be": "be",
    "belarusian": "be",
    "tg": "tg",
    "tajik": "tg",
    "sd": "sd",
    "sindhi": "sd",
    "gu": "gu",
    "gujarati": "gu",
    "am": "am",
    "amharic": "am",
    "yi": "yi",
    "yiddish": "yi",
    "lo": "lo",
    "lao": "lo",
    "uz": "uz",
    "uzbek": "uz",
    "fo": "fo",
    "faroese": "fo",
    "ht": "ht",
    "haitian creole": "ht",
    "ps": "ps",
    "pashto": "ps",
    "tk": "tk",
    "turkmen": "tk",
    "nn": "nn",
    "nynorsk": "nn",
    "mt": "mt",
    "maltese": "mt",
    "sa": "sa",
    "sanskrit": "sa",
    "lb": "lb",
    "luxembourgish": "lb",
    "my": "my",
    "myanmar": "my",
    "bo": "bo",
    "tibetan": "bo",
    "tl": "tl",
    "tagalog": "tl",
    "mg": "mg",
    "malagasy": "mg",
    "as": "as",
    "assamese": "as",
    "tt": "tt",
    "tatar": "tt",
    "haw": "haw",
    "hawaiian": "haw",
    "ln": "ln",
    "lingala": "ln",
    "ha": "ha",
    "hausa": "ha",
    "ba": "ba",
    "bashkir": "ba",
    "jw": "jw",
    "javanese": "jw",
    "su": "su",
    "sundanese": "su",
    "yue": "yue",
    "cantonese": "yue",
}


def normalize_language(value: Any) -> str:
    """Normalise a runtime's language value to an ISO-639-1 code.

    whisper.cpp reports the full English name ("hungarian"), NeMo-Speech.cpp
    reports "" when no language was requested, and the HuggingFace pipeline can
    echo whatever the caller passed. The OpenAI contract wants the ISO-639-1 code
    and ``und`` when the language is undetected, so every backend funnels its
    answer through here. A regional tag ("en-US") keeps its primary code.
    """
    if value is None:
        return "und"
    text = str(value).strip()
    if not text or text.lower() == "auto":
        return "und"
    primary = text.replace("_", "-").split("-", 1)[0].lower()
    return LANGUAGE_CODES.get(primary, "und")


# Granularities upstream requires verbose_json; the other formats cannot carry them.
TIMESTAMP_GRANULARITIES: frozenset[str] = frozenset({"word", "segment"})


def srt_timestamp(seconds: float) -> str:
    """``HH:MM:SS,mmm`` for an SRT cue."""
    millis = max(0, round(float(seconds) * 1000))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def vtt_timestamp(seconds: float) -> str:
    """``HH:MM:SS.mmm`` for a WebVTT cue."""
    return srt_timestamp(seconds).replace(",", ".")


def render_cues(segments: list[dict[str, Any]], vtt: bool) -> str:
    """Render segments as SRT or WebVTT cues."""
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or start)
        text = str(segment.get("text") or "").strip()
        if vtt:
            blocks.append(f"{vtt_timestamp(start)} --> {vtt_timestamp(end)}\n{text}")
        else:
            blocks.append(
                f"{index}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}"
            )
    body = "\n\n".join(blocks)
    if vtt:
        return "WEBVTT\n\n" + body + ("\n" if body else "")
    return body + ("\n" if body else "")


def segments_from_chunks(
    chunks: list[dict[str, Any]] | None, duration: float
) -> list[dict[str, Any]]:
    """Build ``verbose_json`` segments from pipeline chunks.

    A chunked pipeline result carries ``chunks`` with a ``(start, end)`` timestamp
    and the chunk text. Without chunks the whole transcript is one segment.
    """
    segments: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks or []):
        timestamp = chunk.get("timestamp")
        if isinstance(timestamp, (list, tuple)) and len(timestamp) == 2:
            start, end = timestamp
        else:
            start, end = 0.0, duration
        segments.append(
            {
                "id": index,
                "start": float(start or 0.0),
                "end": float(end if end is not None else duration),
                "text": str(chunk.get("text") or "").strip(),
            }
        )
    if not segments:
        segments.append(
            {
                "id": 0,
                "start": 0.0,
                "end": duration,
                "text": "",
            }
        )
    return segments


def words_from_chunks(
    chunks: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Build OpenAI ``verbose_json`` words from word-level pipeline chunks.

    A word-timestamped pipeline returns one chunk per word, each carrying a
    ``(start, end)`` timestamp. A chunk without a usable timestamp cannot be
    placed on the timeline, so it is skipped rather than given a made-up time.
    The word text is kept as the pipeline returned it; Whisper words carry a
    leading space, which is what the OpenAI word array uses too.
    """
    words: list[dict[str, Any]] = []
    for chunk in chunks or []:
        timestamp = chunk.get("timestamp")
        if not (isinstance(timestamp, (list, tuple)) and len(timestamp) == 2):
            continue
        start, end = timestamp
        text = str(chunk.get("text") or "")
        if not text.strip():
            continue
        words.append(
            {
                "word": text,
                "start": float(start or 0.0),
                "end": float(end if end is not None else start or 0.0),
            }
        )
    return words


def attach_words_to_segments(
    segments: list[dict[str, Any]], words: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach each word to the segment whose time range contains it.

    ``segments`` come from a separate segment-timestamp pass, so their boundaries
    are the model's own rather than a host-side guess. A word that starts before
    the first segment or after the last one (the two passes can disagree by a few
    milliseconds) is kept on the edge segment instead of being dropped.
    """
    if not segments:
        return segments
    buckets: list[list[dict[str, Any]]] = [[] for _ in segments]
    for word in words:
        start = float(word.get("start") or 0.0)
        index = 0
        for position, segment in enumerate(segments):
            if start < float(segment.get("start") or 0.0):
                break
            index = position
        buckets[index].append(word)
    return [
        {**segment, "words": bucket}
        for segment, bucket in zip(segments, buckets, strict=True)
    ]
