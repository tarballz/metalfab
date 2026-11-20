"""The verified-overlay writer: record live confirmation of a command or fault.

The overlay is the only data file the tool ever writes. Unknown keys are
refused (the registry is authoritative), existing keys are merged, and the
write is atomic so a crash never leaves a half-written overlay behind.
"""

import json
import os
import tempfile
from pathlib import Path

OVERLAY_NAME = "verified-overlay.json"


def write_verification(directory: Path, group: str, key: str, verified_by: str) -> None:
    """Merge {verified: true, verified_by} for `key` in `group`, atomically.

    `group` is "commands" or "faults"; `key` must exist in the corresponding
    registry file, otherwise the write is refused rather than recording a
    phantom entry.
    """
    registry = _load_json(directory / f"{group}.json")
    entries = registry.get(group, {})
    if isinstance(entries, list):
        entries = {entry.get("code") or entry.get("id"): entry for entry in entries}
    if key not in entries:
        raise ValueError(f"unknown {group} key {key!r}; the registry is authoritative")

    overlay_path = directory / OVERLAY_NAME
    if overlay_path.exists():
        try:
            payload = _load_json(overlay_path)
        except ValueError as error:
            raise ValueError(f"{OVERLAY_NAME} is not valid JSON; refusing to overwrite it ({error})") from error
        if payload.get("version") != 1:
            raise ValueError(f"{OVERLAY_NAME} has an unsupported version; refusing to overwrite it")
    else:
        payload: dict = {"version": 1}

    group_payload = payload.setdefault(group, {})
    if not isinstance(group_payload, dict):
        raise ValueError(f"{OVERLAY_NAME} {group} must be an object")
    entry = group_payload.setdefault(key, {})
    entry["verified"] = True
    entry["verified_by"] = verified_by

    # Atomic replace: write a sibling then rename over the target.
    handle, temporary = tempfile.mkstemp(dir=str(directory), prefix=".overlay-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, overlay_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    return payload
