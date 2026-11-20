"""Validate the Part 1 data files against docs/data-schema.md.

Exits non-zero on the first contract violation so it can gate a handoff.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import sys
from pathlib import Path
from typing import NoReturn

from build_command_table import MACHINE_IMAGE

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

CMD_FIELDS = {
    "code", "summary", "board", "cmd_type", "params", "danger",
    "side_effects", "response", "evidence", "confidence", "verified",
    "verified_by",
}
FAULT_FIELDS = {
    "id", "source_board", "source_string", "severity", "explanation",
    "bit", "evidence", "verified",
}
STATUS_FIELDS = {
    "order", "key", "label", "type", "values", "unit", "scaling",
}
DANGER = {"none", "motion", "emission", "destructive"}
CONFIDENCE = {"high", "medium", "low", "unknown"}
SEVERITY = {"info", "warning", "fault", "severe"}


def fail(message: str) -> NoReturn:
    print(f"FAIL: {message}")
    sys.exit(1)


def load(name: str) -> dict:
    path = DATA / name
    try:
        data = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 - surfaced as a handoff gate
        fail(f"{name}: not valid JSON ({exc})")
    if data.get("version") != 1:
        fail(f"{name}: missing or wrong version")
    return data


def check_commands() -> int:
    """Validate the registry and its link to the bundled machine image."""
    data = load("commands.json")
    source = data.get("source")
    if not isinstance(source, dict) or source.get("board") != "machine" or source.get("image") != MACHINE_IMAGE.name:
        fail("commands.json: source must name the bundled machine image")
    try:
        actual_digest = sha256(MACHINE_IMAGE.read_bytes()).hexdigest()
    except OSError:
        # The vendor images are not redistributed; validation still gates the schema.
        print("commands.json: firmware image not present; skipping the digest check")
        actual_digest = None
    if actual_digest is not None and source.get("sha256") != actual_digest:
        fail("commands.json: source.sha256 must match the bundled machine image")
    codes = set()
    for entry in data.get("commands", []):
        missing = CMD_FIELDS - set(entry)
        if missing:
            fail(f"commands.json: {entry.get('code')} missing {sorted(missing)}")
        if entry["code"] in codes:
            fail(f"commands.json: duplicate code {entry['code']}")
        codes.add(entry["code"])
        if entry["danger"] not in DANGER:
            fail(f"commands.json: {entry['code']} bad danger {entry['danger']!r}")
        if entry["confidence"] not in CONFIDENCE:
            fail(f"commands.json: {entry['code']} bad confidence {entry['confidence']!r}")
        if entry["cmd_type"] not in {"get", "set", "action"}:
            fail(f"commands.json: {entry['code']} bad cmd_type {entry['cmd_type']!r}")
    return len(codes)


def check_faults() -> tuple[int, dict[str, dict]]:
    data = load("faults.json")
    enum = set(data.get("severity_enum", []))
    if enum != SEVERITY:
        fail(f"faults.json: severity_enum {sorted(enum)} != {sorted(SEVERITY)}")
    seen = {}
    for entry in data.get("faults", []):
        missing = FAULT_FIELDS - set(entry)
        if missing:
            fail(f"faults.json: {entry.get('id')} missing {sorted(missing)}")
        if entry["id"] in seen:
            fail(f"faults.json: duplicate id {entry['id']}")
        seen[entry["id"]] = entry
        if entry["severity"] not in enum:
            fail(f"faults.json: {entry['id']} bad severity {entry['severity']!r}")
        if entry.get("popup") is not None:
            popup = entry["popup"]
            if not isinstance(popup, dict) or not isinstance(popup.get("id"), int) \
                    or not isinstance(popup.get("page"), int):
                fail(f"faults.json: {entry['id']} bad popup {popup!r}")
        if entry.get("screen_text") is not None:
            screen_text = entry["screen_text"]
            if not isinstance(screen_text, dict) or not isinstance(screen_text.get("title"), str) \
                    or not screen_text["title"] or not isinstance(screen_text.get("body"), str):
                fail(f"faults.json: {entry['id']} bad screen_text")
        if entry["bit"] is not None:
            bit = entry["bit"]
            if not isinstance(bit, dict) or not isinstance(bit.get("word"), str) \
                    or not isinstance(bit.get("index"), int) or not 0 <= bit["index"] < 32 \
                    or bit.get("active") not in (0, 1):
                fail(f"faults.json: {entry['id']} bad bit {bit!r}")
        for list_name in ("causes", "actions", "firmware"):
            if entry.get(list_name) is not None:
                if not isinstance(entry[list_name], list) \
                        or not all(isinstance(item, str) and item for item in entry[list_name]):
                    fail(f"faults.json: {entry['id']} bad {list_name}")
    return len(seen), seen


def check_status(faults: dict[str, dict]) -> int:
    data = load("status.json")
    report = data.get("report", {})
    if "headers" not in report or "terminator" not in report:
        fail("status.json: report missing headers/terminator")
    headers = report["headers"]
    fields = report.get("fields", [])
    if not fields:
        fail("status.json: no fields")
    orders = []
    for entry in fields:
        missing = STATUS_FIELDS - set(entry)
        if missing:
            fail(f"status.json: field {entry.get('key')} missing {sorted(missing)}")
        orders.append(entry["order"])
    if orders != sorted(orders) or len(set(orders)) != len(orders):
        fail("status.json: field order is not strictly increasing")
    raw_status = report.get("raw_status", {})
    if not raw_status.get("format"):
        fail("status.json: raw_status missing format")
    raw_sections = {entry["section"] for entry in fields
                    if entry.get("label") == raw_status.get("label") and entry.get("type") == "hex"}
    bits = raw_status.get("bits", [])
    seen_bits = set()
    for bit in bits:
        if not isinstance(bit, dict) or not isinstance(bit.get("index"), int) \
                or not 0 <= bit["index"] < 32 or bit["index"] in seen_bits:
            fail(f"status.json: bad or duplicate raw-status bit {bit!r}")
        seen_bits.add(bit["index"])
        fault_id = bit.get("fault_id")
        if not isinstance(fault_id, str) or fault_id not in faults:
            fail(f"status.json: bit {bit['index']} names unknown fault {fault_id!r}")
        section = bit.get("section")
        if section is not None and section not in headers:
            fail(f"status.json: bit {bit['index']} has unknown section {section!r}")
        if report.get("require_complete_report") and len(raw_sections) > 1 and section is None:
            fail(f"status.json: bit {bit['index']} needs a section with multiple raw words")
        if section is not None:
            fault_bit = faults[fault_id].get("bit")
            if not fault_bit or fault_bit.get("word") != section \
                    or fault_bit.get("index") != bit["index"]:
                fail(f"status.json: bit {bit['index']} does not match faults.json {fault_id}")
    return len(fields)


def main(argv: list[str] | None = None) -> None:
    global DATA  # noqa: PLW0603 - CLI override for tests
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA, help="data directory (default: repo data/)")
    args = parser.parse_args(argv)
    DATA = args.data
    commands = check_commands()
    faults, fault_entries = check_faults()
    fields = check_status(fault_entries)
    print(f"OK: {commands} commands, {faults} faults, {fields} status fields")


if __name__ == "__main__":
    main()
