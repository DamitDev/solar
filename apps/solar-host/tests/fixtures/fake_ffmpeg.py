#!/usr/bin/env python3
"""Fake ``ffmpeg`` for the HuggingFace audio route tests.

CI runners have no ffmpeg installed, but the route decodes every upload by
executing::

    ffmpeg -v error -i <upload> -f f32le -ac 1 -ar 16000 pipe:1

This stands in for the real binary. It accepts exactly that argv and decodes a
16 kHz mono PCM16 WAV to the little-endian float32 samples real ffmpeg would
write. A broken upload still exits non-zero with a message on stderr, so the
route's decode-failure envelope stays under test instead of being bypassed.
"""

import array
import sys
import wave
from typing import NoReturn

_EXPECTED = "-v error -i <input> -f f32le -ac 1 -ar 16000 pipe:1"


def _fail(message: str) -> NoReturn:
    print(f"fake ffmpeg: {message}", file=sys.stderr)
    raise SystemExit(1)


def _decode(argv: list[str]) -> None:
    source = None
    sample_format = None
    channels = None
    rate = None
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in ("-v", "-loglevel", "-i", "-f", "-ac", "-ar"):
            if index + 1 == len(argv):
                _fail(f"{argument} needs a value")
            value = argv[index + 1]
            index += 2
            if argument == "-i":
                source = value
            elif argument == "-f":
                sample_format = value
            elif argument == "-ac":
                channels = int(value)
            elif argument == "-ar":
                rate = int(value)
        elif argument in ("-hide_banner", "-nostdin", "pipe:1"):
            index += 1
        else:
            _fail(f"unsupported argument {argument!r}")

    if source is None:
        _fail(f"expected: {_EXPECTED}")
    if sample_format != "f32le" or channels != 1 or rate != 16000:
        _fail(f"expected: {_EXPECTED}")

    frames = b""
    try:
        with wave.open(source, "rb") as handle:
            if handle.getnchannels() != channels:
                _fail(f"{source} has {handle.getnchannels()} channels, not 1")
            if handle.getframerate() != rate:
                _fail(f"{source} is {handle.getframerate()} Hz, not 16000")
            if handle.getsampwidth() != 2:
                _fail(f"{source} is not 16-bit PCM")
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError, OSError) as error:
        _fail(f"cannot read {source}: {error}")

    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    output = array.array("f", (sample / 32768.0 for sample in samples))
    if sys.byteorder == "big":
        output.byteswap()
    sys.stdout.buffer.write(output.tobytes())


if __name__ == "__main__":
    try:
        _decode(sys.argv[1:])
    except ValueError as error:
        _fail(str(error))
