"""Load and validate the firmware evidence supplied by Part 1."""

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any, cast


class ContractError(ValueError):
    """An evidence file is invalid or a required capability is unavailable."""


@dataclass(frozen=True)
class Field:
    """How to decode a single status field."""

    key: str
    label: str
    kind: str
    values: tuple[str, ...]
    scaling: int | float | None
    section: str | None


@dataclass(frozen=True)
class ReportSchema:
    """Version 1 report framing and field definitions."""

    trigger: str | None
    headers: tuple[str, ...]
    terminator: str
    fields: tuple[Field, ...]
    raw_label: str
    raw_bits: dict[int, str]
    require_complete_report: bool = False
    raw_sections: tuple[str, ...] = ()
    # Which raw word each bit belongs to: the section header, or None for the
    # legacy single-word shape. Required for every bit when the report carries
    # more than one raw word.
    bit_sections: dict[int, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class Evidence:
    """Available contracts and actionable messages for files that failed to load."""

    directory: Path
    commands: dict[str, dict[str, Any]]
    image_hash: str | None
    faults: dict[str, dict[str, Any]]
    report: ReportSchema | None
    problems: tuple[str, ...]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _object(value: Any) -> dict:
    _require(isinstance(value, dict), "Expected a JSON object")
    return value


def _text(value: Any) -> str:
    _require(isinstance(value, str) and bool(value), "Expected a nonempty string")
    return value


def _array(value: Any) -> list:
    _require(isinstance(value, list), "Expected a JSON array")
    return value


def _entries(value: Any, key: str, *, required: str) -> dict:
    entries = {}
    for value in _array(value):
        entry = _object(value)
        entry_key = _text(entry[key])
        _require(entry_key not in entries, f"Duplicate {key} {entry_key!r}")
        _require(all(field in entry for field in required.split()), f"Missing fields in {entry_key}")
        _require(type(entry["verified"]) is bool, f"Invalid verification flag in {entry_key}")
        entries[entry_key] = entry
    return entries


def _read(path: Path) -> dict:
    result = _object(json.loads(path.read_text(encoding="utf-8")))
    _require(type(result["version"]) is int and result["version"] == 1, "Expected schema version 1")
    return result


def _load_commands(data: dict) -> tuple[dict, str]:
    source = _object(data["source"])
    _text(source["board"])
    _text(source["image"])
    digest = _text(source["sha256"]).lower()
    _require(re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "Invalid firmware SHA-256")
    commands = _entries(
        data["commands"], "code",
        required="summary board cmd_type params danger side_effects response evidence confidence verified verified_by",
    )
    for code, command in commands.items():
        _require(re.fullmatch(r"M[0-9]+", code) is not None, f"Invalid command code {code!r}")
        _require(command["cmd_type"] in ("get", "set", "action"), f"Invalid type for {code}")
        _require(command["danger"] in ("none", "motion", "emission", "destructive"), f"Invalid danger for {code}")
        _require(command["confidence"] in ("high", "medium", "low", "unknown"), f"Invalid confidence for {code}")
        if command.get("readback") is not None:
            _require(type(command["readback"]) is bool, f"Invalid readback flag for {code}")
        _text(command["board"])
        _text(command["summary"])
        for param in _array(command["params"]):
            _text(_object(param)["name"])
            _text(param["type"])
        for effect in _array(command["side_effects"]):
            _text(effect)
        _text(_object(command["response"])["kind"])
        _text(_object(command["evidence"])["offset"])
        _require(command["verified_by"] is None or isinstance(command["verified_by"], str), f"Invalid verification source for {code}")
    return commands, digest


def _load_faults(data: dict) -> dict:
    allowed = set(map(_text, _array(data["severity_enum"])))
    faults = _entries(
        data["faults"], "id",
        required="source_board source_string severity explanation bit evidence verified",
    )
    for fault_id, fault in faults.items():
        _require(_text(fault["severity"]) in allowed, f"Invalid severity for {fault_id}")
        for field in ("source_board", "source_string", "explanation"):
            _text(fault[field])
        _text(_object(fault["evidence"])["offset"])
        if fault["bit"] is not None:
            bit = _object(fault["bit"])
            _text(bit["word"])
            _require(type(bit["index"]) is int and 0 <= bit["index"] < 32, f"Invalid bit index for {fault_id}")
            _require(type(bit["active"]) is int and bit["active"] in (0, 1), f"Invalid polarity for {fault_id}")
        if fault.get("popup") is not None:
            popup = _object(fault["popup"])
            for name in ("id", "page"):
                _require(type(popup[name]) is int and popup[name] >= 0, f"Invalid popup {name} for {fault_id}")
        if fault.get("screen_text") is not None:
            screen_text = _object(fault["screen_text"])
            _text(screen_text["title"])
            _require(isinstance(screen_text.get("body"), str), f"Invalid screen_text body for {fault_id}")
        for list_name in ("causes", "actions", "firmware"):
            if fault.get(list_name) is not None:
                _require(
                    isinstance(fault[list_name], list)
                    and all(isinstance(item, str) and item for item in fault[list_name]),
                    f"Invalid {list_name} for {fault_id}",
                )
    return faults


def _load_report(data: dict) -> ReportSchema:
    trigger = _object(data["trigger"])["command"]
    if trigger is not None:
        _text(trigger)
    report = _object(data["report"])
    headers = tuple(map(_text, _array(report["headers"])))
    _require(bool(headers) and len(set(headers)) == len(headers), "Report headers must be distinct")
    terminator = _text(report["terminator"])
    _require(terminator not in headers, "Report terminator conflicts with a header")
    require_complete = report.get("require_complete_report", False)
    _require(type(require_complete) is bool, "require_complete_report must be a boolean")
    fields = []
    keys = set()
    labels = set()
    ordered = []
    raw_status = _object(report["raw_status"])
    raw_label = _text(raw_status["label"])
    raw_sections: list[str] = []
    _require(raw_status.get("format", "0x%08X") in ("0x%08X", "0x%08x"), "Unsupported status word format")
    for raw_field in _array(report["fields"]):
        row = _object(raw_field)
        _require(type(row["order"]) is int, "Report field order must be an integer")
        ordered.append(row)
    for row in sorted(ordered, key=lambda field: field["order"]):
        key, label, kind = (_text(row[name]) for name in ("key", "label", "type"))
        _require(kind in ("enum", "int", "float", "bool", "string", "hex"), f"Unsupported field type {kind}")
        section = row.get("section")
        _require(section is None or section in headers, f"Unknown section for {key}")
        # The same raw-word label can appear under more than one section.
        # The parser keys those words by the enclosing header.
        if label == raw_label and kind == "hex":
            if require_complete:
                _require(section is not None, f"Missing raw word section for {key}")
                _require(section not in raw_sections, f"Duplicate raw word in {section}")
                raw_sections.append(cast(str, section))
            continue
        _require(key not in keys and (section, label) not in labels, f"Duplicate field {key}")
        keys.add(key)
        labels.add((section, label))
        scaling = row.get("scaling")
        if scaling is not None:
            _require(
                kind in ("int", "float", "hex") and type(scaling) in (int, float)
                and math.isfinite(scaling), f"Invalid scaling for {key}",
            )
        values = tuple(map(_text, _array(row.get("values") or [])))
        fields.append(Field(key, label, kind, values, scaling, section))
    bits = {}
    bit_sections = {}
    for entry in _array(raw_status["bits"]):
        bit = _object(entry)
        index = bit["index"]
        _require(type(index) is int and 0 <= index < 32 and index not in bits, "Invalid or duplicate raw-status bit")
        section = bit.get("section")
        _require(section is None or section in headers, f"Unknown section for raw-status bit {index}")
        bits[index] = _text(bit["fault_id"])
        bit_sections[index] = section
    if bits and require_complete and len(raw_sections) > 1:
        # Two raw words cannot be decoded against flat bits: every bit must name
        # its owning section, or the parser would misattribute or raise a
        # spurious issue on every complete report.
        unsectioned = [index for index, section in bit_sections.items() if section is None]
        _require(not unsectioned, f"Raw-status bits missing a section: {unsectioned}")
    return ReportSchema(
        trigger, headers, terminator, tuple(fields), raw_label, bits, require_complete,
        tuple(raw_sections), bit_sections,
    )


def _merge_overlay(overlay: dict, groups: dict[str, dict], problems: list[str]) -> None:
    updates = []
    for group_name, entries in groups.items():
        for key, raw_value in _object(overlay.get(group_name, {})).items():
            if key not in entries:
                problems.append(f"verified-overlay.json references unknown {group_name} key {key}")
                continue
            value = _object(raw_value)
            update = {}
            if "verified" in value:
                _require(type(value["verified"]) is bool, f"Invalid overlay verification for {key}")
                update["verified"] = value["verified"]
            if "verified_by" in value:
                update["verified_by"] = _text(value["verified_by"])
            updates.append((entries[key], update))
    for entry, update in updates:
        entry.update(update)


def load_evidence(directory: Path) -> Evidence:
    """Load each contract independently so capture and replay can run early."""
    problems: list[str] = []

    def load(name, parse, default):
        try:
            return parse(_read(directory / name))
        except FileNotFoundError:
            problems.append(f"{name} is missing")
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, OverflowError) as error:
            problems.append(f"{name}: {error}")
        return default

    commands, digest = load("commands.json", _load_commands, ({}, None))
    faults = load("faults.json", _load_faults, None)
    report = load("status.json", _load_report, None)
    if faults is not None and report is not None:
        # A fault whose bit points at an unknown raw-word section is a data
        # inconsistency, never a reason to refuse replay.
        for fault_id, fault in faults.items():
            bit = fault.get("bit")
            if bit is None or bit["word"] == "raw_status":
                continue
            if bit["word"] not in report.raw_sections:
                problems.append(f"faults.json {fault_id} bit.word {bit['word']!r} is not a raw-status section")
    if (directory / "verified-overlay.json").exists():
        # An unavailable registry cannot tell us whether an overlay key is unknown.
        groups = {}
        if digest is not None:
            groups["commands"] = commands
        if faults is not None:
            groups["faults"] = faults
        load("verified-overlay.json", lambda data: _merge_overlay(data, groups, problems), None)
    return Evidence(directory, commands, digest, faults or {}, report, tuple(problems))
