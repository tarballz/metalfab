"""Read, format, and compare MetalFab settings snapshots."""

from difflib import unified_diff
import json
from pathlib import Path
from typing import Iterator

# Provisional human labels for the reviewed settings keys. The offset-named
# entries keep their offsets until live use identifies them; the named ones
# follow the values observed in the machine's own config dumps.
SETTING_LABELS = {
    "laser_power": "Laser power (percent; clamped by the firmware)",
    "feed_speed": "Feed speed (clamped by the firmware)",
    "focus_correction": "Focus correction (clamped by the firmware)",
    "ntc_temperature": "NTC temperature reading",
    "flag_807": "Boolean flag (effect not established)",
    "setting_1ac": "Settings value at +0x1ac",
    "setting_1b4": "Settings value at +0x1b4",
    "setting_188": "Settings value at +0x188",
    "setting_388": "Settings value at +0x388",
    "setting_48": "Settings value at +0x48",
    "setting_38": "Settings value at +0x38",
}


def setting_label(name: str) -> str:
    """Return the provisional label for a settings key, or the key itself."""
    return SETTING_LABELS.get(name, name)


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
