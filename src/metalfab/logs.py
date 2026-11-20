"""Parsers for the M350 job-index and M351 system-log responses.

The record format is the firmware's own literal
('#%03lu [%02lu:%02lu:%02lu.%03lu] [%s] [%s] Param:0x%08lX - %s\n',
re/machine/strings.txt:179), where the two names come from FUN_08009fd8
(SYSTEM/ERROR/WARNING/other/UNKNOWN) and FUN_080098d0 (the 11 event-class
names). Header and terminator forms are the four literals at
strings.txt:177-184.
"""

from dataclasses import dataclass
import re

# One record line: index, h:m:s.ms, level, class name, hex param, free text.
_RECORD_RE = re.compile(
    r"^#(?P<index>\d{3}) "
    r"\[(?P<hours>\d{2}):(?P<minutes>\d{2}):(?P<seconds>\d{2})\.(?P<millis>\d{3})\] "
    r"\[(?P<level>\S+)\] \[(?P<class_name>\S+)\] "
    r"Param:0x(?P<param>[0-9A-Fa-f]{8}) - (?P<message>.*)$"
)
_HEADER_ALL_RE = re.compile(r"^=== System Log Records \(Total: (?P<total>\d+)\) ===$")
_HEADER_TYPE_RE = re.compile(
    r"^=== System Log Records \(Type: (?P<type_name>\S+), Count: (?P<total>\d+)\) ===$"
)
_EMPTY_ALL = "No log records"
_EMPTY_TYPE_RE = re.compile(r"^No log records for type: (?P<type_name>\S+)$")
_TERMINATOR = "=== End of Log Records ==="


@dataclass(frozen=True)
class LogRecord:
    """One decoded ring-buffer record from the M351 dump."""

    index: int
    hours: int
    minutes: int
    seconds: int
    millis: int
    level: str
    class_name: str
    param: int
    message: str


@dataclass(frozen=True)
class SystemLog:
    """A decoded M351 response: all records or one level filter."""

    kind: str  # "all" | "filtered"
    type_name: str | None
    total: int
    records: list[LogRecord]


def parse_job_index(text: str) -> dict:
    """Keep the raw M350 lines until the index-file format is established live.

    The firmware prints 'Index file content: %s' plus per-mode folder listings;
    the embedded content format is unknown until a first live run, so the parser
    deliberately preserves the raw lines instead of inventing a structure.
    """
    lines = [line.strip("\r") for line in text.splitlines() if line.strip()]
    return {"lines": lines, "truncated": False}


def parse_system_log(text: str) -> SystemLog:
    """Decode an M351 response; unknown lines are ignored, bad records raise.

    Async serial lines can interleave with the dump (captures keep the truth);
    they are tolerated so the parser stays usable on live streams, while a
    record-shaped line that fails to parse signals a real format mismatch.
    """
    kind = "all"
    type_name: str | None = None
    total = 0
    records: list[LogRecord] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line == _TERMINATOR:
            continue
        match = _HEADER_ALL_RE.match(line)
        if match:
            kind = "all"
            total = int(match.group("total"))
            continue
        match = _HEADER_TYPE_RE.match(line)
        if match:
            kind = "filtered"
            type_name = match.group("type_name")
            total = int(match.group("total"))
            continue
        if line == _EMPTY_ALL:
            kind = "all"
            total = 0
            continue
        match = _EMPTY_TYPE_RE.match(line)
        if match:
            kind = "filtered"
            type_name = match.group("type_name")
            total = 0
            continue
        if line.startswith("#") and "Param:" in line:
            match = _RECORD_RE.match(line)
            if match is None:
                raise ValueError(f"Malformed log record: {line!r}")
            records.append(LogRecord(
                index=int(match.group("index")),
                hours=int(match.group("hours")),
                minutes=int(match.group("minutes")),
                seconds=int(match.group("seconds")),
                millis=int(match.group("millis")),
                level=match.group("level"),
                class_name=match.group("class_name"),
                param=int(match.group("param"), 16),
                message=match.group("message"),
            ))
    return SystemLog(kind=kind, type_name=type_name, total=total, records=records)
