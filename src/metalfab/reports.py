"""Decode serial lines and assemble status reports from live or recorded bytes."""

from dataclasses import dataclass, field
import math
import re
from typing import Iterator

from .contracts import Evidence, Field

MAX_LINE = 16 * 1024


@dataclass(frozen=True)
class Line:
    """A decoded line or a framing problem whose original bytes are in the capture."""

    text: str
    issue: str | None = None


class LineDecoder:
    """Accept LF and CRLF across arbitrary reads, bounding unterminated input."""

    def __init__(self):
        self._pending = bytearray()
        self._discard = False

    def feed(self, data: bytes) -> Iterator[Line]:
        """Yield complete lines in arrival order, preserving undecodable content."""
        start = 0
        while start < len(data):
            delimiter = data.find(b"\n", start)
            stop = len(data) if delimiter < 0 else delimiter
            if not self._discard:
                if len(self._pending) + stop - start > MAX_LINE:
                    self._pending.clear()
                    self._discard = True
                    yield Line("", "Line exceeded the framing limit")
                else:
                    self._pending.extend(data[start:stop])
            if delimiter < 0:
                break
            if not self._discard:
                raw = bytes(self._pending).removesuffix(b"\r")
                self._pending.clear()
                try:
                    value = raw.decode("utf-8")
                    if not all(char.isprintable() or char == "\t" for char in value):
                        yield Line(value, "Line contains control characters")
                    else:
                        yield Line(value)
                except UnicodeDecodeError:
                    yield Line(raw.decode("utf-8", errors="backslashreplace"), "Line contains non-UTF-8 bytes")
            self._discard = False
            start = delimiter + 1

    def finish(self) -> Line | None:
        """Return the trailing fragment, if present, when input ends."""
        if self._pending:
            fragment = self._pending.decode("utf-8", errors="backslashreplace")
            self._pending.clear()
            return Line(fragment, "Input ended mid-line")
        return None

    @property
    def has_fragment(self) -> bool:
        """Whether input is currently between line boundaries."""
        return bool(self._pending) or self._discard


@dataclass
class Report:
    """One status report and any uncertainty encountered while decoding it."""

    sections: list[str] = field(default_factory=list)
    values: dict[str, str | bool | int | float] = field(default_factory=dict)
    raw_status: dict[str, str] = field(default_factory=dict)
    faults: list[dict] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    complete: bool = False

    def issue(self, message: str) -> None:
        """Add a diagnostic once, keeping repeated malformed input bounded."""
        if message not in self.issues and len(self.issues) < 128:
            self.issues.append(message)


@dataclass(frozen=True)
class Event:
    """A serial line together with any report that it started, closed, or abandoned."""

    line: Line | None
    report: Report | None = None
    started_report: bool = False
    fault: dict | None = None


def _value(spec: Field, text: str) -> str | bool | int | float:
    if spec.kind in ("string", "enum"):
        return text
    if spec.kind == "bool":
        choices = {"true": True, "false": False, "1": True, "0": False}
        if text.casefold() not in choices:
            raise ValueError(text)
        return choices[text.casefold()]
    if spec.kind == "float":
        value = float(text)
    else:
        value = int(text, 16 if spec.kind == "hex" else 10)
    if spec.scaling is not None:
        value *= spec.scaling
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(text)
    return value


class ReportParser:
    """Collect schema-defined sections while leaving unrelated lines available to callers."""

    def __init__(self, evidence: Evidence):
        self.schema = evidence.report
        self._fields = {(field.section, field.label): field for field in self.schema.fields} if self.schema else {}
        self._faults = evidence.faults
        self._fault_strings: dict[str, tuple[str, dict]] = {}
        for fault_id, fault in evidence.faults.items():
            self._fault_strings.setdefault(fault["source_string"], (fault_id, fault))
        self._report: Report | None = None
        self._section: str | None = None

    @property
    def in_report(self) -> bool:
        """Whether a report header has arrived without its terminator."""
        return self._report is not None

    def _add_fault(self, fault_id: str, fault: dict) -> None:
        if self._report is not None and not any(entry["id"] == fault_id for entry in self._report.faults):
            self._report.faults.append(self._fault_details(fault_id, fault))

    @staticmethod
    def _fault_details(fault_id: str, fault: dict) -> dict:
        return {
            "id": fault_id, "message": fault["source_string"], "severity": fault["severity"],
            "explanation": fault["explanation"], "verified": fault["verified"],
            "popup": fault.get("popup"),
            "screen_text": fault.get("screen_text"),
        }

    def _finish(self, *, complete: bool) -> Report | None:
        report = self._report
        if report is None or self.schema is None:
            return None
        report.complete = complete
        if not complete:
            report.issue("Status report ended before its terminator")
        elif self.schema.require_complete_report:
            # The terminator proves framing. The reviewed layout also specifies
            # which sections and values must arrive before this report is usable.
            if tuple(report.sections) != self.schema.headers:
                report.issue("Status report has missing or out-of-order sections")
                report.complete = False
            missing = [field.key for field in self.schema.fields if field.key not in report.values]
            if missing:
                report.issue(f"Missing status fields: {', '.join(missing)}")
                report.complete = False
            missing_words = [section for section in self.schema.raw_sections if section not in report.raw_status]
            if missing_words:
                report.issue(f"Missing raw status words: {', '.join(missing_words)}")
                report.complete = False
        if self.schema.raw_bits and len(report.raw_status) > 1:
            # Two raw words: each bit is scoped to its owning section, so a word
            # decodes only against the map for its own section.
            for bit, fault_id in sorted(self.schema.raw_bits.items()):
                section = self.schema.bit_sections.get(bit)
                if section is None or section not in report.raw_status:
                    report.issue(f"Raw-status bit {bit} has no section for this report")
                    continue
                raw = int(report.raw_status[section], 16)
                fault = self._faults.get(fault_id)
                if fault is None:
                    report.issue(f"Unknown fault mapping for status bit {bit}")
                    continue
                flag = fault.get("bit")
                if not flag or flag["word"] != section or flag["index"] != bit:
                    report.issue(f"Unknown fault mapping for status bit {bit}")
                elif (raw >> bit) & 1 == flag["active"]:
                    self._add_fault(fault_id, fault)
        elif len(report.raw_status) == 1:
            raw = int(next(iter(report.raw_status.values())), 16)
            for bit, fault_id in sorted(self.schema.raw_bits.items()):
                fault = self._faults.get(fault_id)
                if fault is None:
                    report.issue(f"Unknown fault mapping for status bit {bit}")
                    continue
                flag = fault.get("bit")
                if not flag or flag["word"] != "raw_status" or flag["index"] != bit:
                    report.issue(f"Unknown fault mapping for status bit {bit}")
                elif (raw >> bit) & 1 == flag["active"]:
                    self._add_fault(fault_id, fault)
        self._report = None
        self._section = None
        return report

    def feed(self, line: Line) -> Event:
        """Decode a single line, preserving unrelated traffic as an event."""
        schema = self.schema
        if line.issue:
            if self._report is not None:
                self._report.issue(line.issue)
            return Event(line)
        text = line.text.strip()
        known_fault = self._fault_strings.get(text)
        fault = self._fault_details(*known_fault) if known_fault else None
        if schema is None:
            return Event(line, fault=fault)
        # Interim contracts used dashed delimiters for the Ruike heading.
        # Treat the literal heading from the firmware as the same section.
        if text == "=== Laser Ruike Status Report ===" and text not in schema.headers:
            text = "--- Laser Ruike Status Report ---"
        if text in schema.headers:
            previous = None
            started = self._report is None or text in self._report.sections
            if started:
                previous = self._finish(complete=False)
                self._report = Report()
            self._report.sections.append(text)
            self._section = text
            return Event(line, previous, started)
        if self._report is None:
            return Event(line, fault=fault)
        if text == schema.terminator:
            return Event(line, self._finish(complete=True))
        label, separator, raw_value = text.partition(":")
        if separator:
            self._field(label.rstrip(), raw_value.strip())
        if known_fault:
            self._add_fault(*known_fault)
        elif text.startswith("Laser Fault "):
            self._report.issue("Status report contains an unmapped laser fault message")
        return Event(line, fault=fault)

    def _field(self, label: str, raw: str) -> None:
        if self._report is None or self.schema is None or self._section is None:
            return
        if label == self.schema.raw_label:
            if not re.fullmatch(r"0[xX][0-9a-fA-F]{8}", raw):
                self._report.issue("Malformed raw status word")
            elif self._section in self._report.raw_status:
                self._report.issue("Repeated raw status word in one section")
            else:
                self._report.raw_status[self._section] = raw
            return
        spec = self._fields.get((self._section, label), self._fields.get((None, label)))
        if spec is None:
            return
        if spec.key in self._report.values:
            self._report.issue(f"Repeated field {spec.key}")
            return
        if spec.kind == "enum" and spec.values and raw not in spec.values:
            self._report.issue(f"Unknown value for {spec.key}")
        try:
            self._report.values[spec.key] = _value(spec, raw)
        except (ValueError, OverflowError):
            self._report.values[spec.key] = raw
            self._report.issue(f"Invalid value for {spec.key}")

    def finish(self) -> Report | None:
        """Return an unfinished report when the stream closes."""
        return self._finish(complete=False)


class Decoder:
    """Run the same byte and report decoding for live input and replay."""

    def __init__(self, evidence: Evidence):
        self.lines = LineDecoder()
        self.reports = ReportParser(evidence)

    def feed(self, data: bytes) -> Iterator[Event]:
        """Yield parsed events for one serial chunk."""
        for line in self.lines.feed(data):
            yield self.reports.feed(line)

    def finish(self) -> Iterator[Event]:
        """Expose trailing partial lines and reports before closing."""
        if (line := self.lines.finish()) is not None:
            yield self.reports.feed(line)
        if (report := self.reports.finish()) is not None:
            yield Event(None, report)
