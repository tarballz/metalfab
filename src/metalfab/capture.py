"""Record live serial sessions and read them back without opening a port."""

import base64
import binascii
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import os
import time
from typing import BinaryIO, Iterator, Literal
from uuid import uuid4

from .contracts import Evidence
from .reports import Decoder, Event


MAX_RECORD = 64 * 1024


class CaptureError(ValueError):
    """A recording contains invalid or unsupported data."""


@dataclass(frozen=True)
class RecordedChunk:
    """One successful serial read or write, in observed order."""

    elapsed_ns: int
    direction: Literal["rx", "tx"]
    data: bytes


def capture_directory() -> Path:
    """Return the default directory for private live recordings."""
    return Path.home() / "Library" / "Logs" / "MetalFab"


class Capture(AbstractContextManager):
    """Write a private, append-only capture for the lifetime of a live session."""

    def __init__(self, path: Path | None, *, port: str, baud: int, profile: str | None):
        if path is None:
            directory = capture_directory()
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = directory / f"{stamp}-{uuid4().hex[:8]}.jsonl"
        self.path = path
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._stream = os.fdopen(descriptor, "wb")
        self._start = time.monotonic_ns()
        self._sequence = 0
        try:
            self._write(
                kind="session", version=1, port=port, baud=baud, profile=profile,
                started_at=datetime.now(timezone.utc).isoformat(),
            )
        except BaseException:
            self._stream.close()
            raise

    def _write(self, **record: object) -> None:
        self._stream.write(json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n")
        self._stream.flush()

    def chunk(self, direction: Literal["rx", "tx"], data: bytes) -> None:
        """Record raw bytes before the decoder or request matcher sees them."""
        if not data:
            return
        self._sequence += 1
        self._write(
            kind="bytes", seq=self._sequence, elapsed_ns=time.monotonic_ns() - self._start,
            direction=direction, data=base64.b64encode(data).decode("ascii"),
        )

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            self._write(
                kind="end", elapsed_ns=time.monotonic_ns() - self._start,
                reason="closed" if exc_type is None else exc_type.__name__,
            )
        except OSError:
            # Preserve the original serial or capture failure when unwinding.
            if exc_type is None:
                raise
        finally:
            self._stream.close()


def _record(stream: BinaryIO) -> dict | None:
    raw = stream.readline(MAX_RECORD + 1)
    if not raw:
        return None
    if len(raw) > MAX_RECORD or not raw.endswith(b"\n"):
        raise CaptureError("Capture contains an oversized or truncated record")
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise CaptureError("Capture contains invalid JSON") from error
    if not isinstance(record, dict):
        raise CaptureError("Capture records must be objects")
    return record


def read_capture(path: Path) -> Iterator[RecordedChunk]:
    """Stream capture chunks, checking record order and completeness."""
    with path.open("rb") as stream:
        header = _record(stream)
        if not header or header.get("kind") != "session" or header.get("version") != 1:
            raise CaptureError("Expected a MetalFab version 1 capture")
        sequence = 1
        last_elapsed = 0
        while (entry := _record(stream)) is not None:
            elapsed = entry.get("elapsed_ns")
            if type(elapsed) is not int or elapsed < last_elapsed:
                raise CaptureError("Capture timestamps are missing or out of order")
            last_elapsed = elapsed
            if entry.get("kind") == "end":
                if stream.read(1):
                    raise CaptureError("Capture contains records after its end")
                return
            direction = entry.get("direction")
            if entry.get("kind") != "bytes" or entry.get("seq") != sequence or direction not in ("rx", "tx"):
                raise CaptureError(f"Invalid capture entry at sequence {sequence}")
            try:
                payload = base64.b64decode(entry["data"], validate=True)
            except (KeyError, TypeError, ValueError, binascii.Error) as error:
                raise CaptureError(f"Invalid byte encoding at sequence {sequence}") from error
            sequence += 1
            yield RecordedChunk(elapsed, direction, payload)
        raise CaptureError("Capture ended without a session end record")


def replay_capture(evidence: Evidence, path: Path) -> Iterator[tuple[int, Event]]:
    """Decode received traffic offline with timestamps relative to the session start."""
    decoder = Decoder(evidence)
    elapsed = 0
    for chunk in read_capture(path):
        elapsed = chunk.elapsed_ns
        # Transmitted bytes are preserved in the file and excluded from the report decoder.
        if chunk.direction == "rx":
            for event in decoder.feed(chunk.data):
                yield elapsed, event
    for event in decoder.finish():
        yield elapsed, event
