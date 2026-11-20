"""Read, format, and compare MetalFab settings snapshots."""

from difflib import unified_diff
import json
from pathlib import Path
from typing import Iterator


def settings_snapshot(firmware: str, values: dict[str, str]) -> dict:
    """Build a version 1 snapshot from reviewed setting values."""
    return {"version": 1, "firmware": firmware, "settings": values}


def _snapshot_lines(path: Path) -> list[str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("firmware"), str) or not isinstance(value.get("settings"), dict)):
        raise ValueError(f"Expected a version 1 settings snapshot in {path}")
    return json.dumps(value, indent=2, sort_keys=True).splitlines()


def settings_diff(before: Path, after: Path) -> Iterator[str]:
    """Compare two version 1 snapshots after normalizing their JSON formatting."""
    yield from unified_diff(_snapshot_lines(before), _snapshot_lines(after), str(before), str(after), lineterm="")
