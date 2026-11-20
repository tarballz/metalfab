"""Exercise the popup-table decoder against the machine image and screen bundle."""

import importlib.util
from pathlib import Path
import pytest


REPO = Path(__file__).resolve().parent.parent


pytestmark = pytest.mark.skipif(
    not (REPO / "firmware").exists(),
    reason="vendor firmware not present; obtain the HJ004 images first",
)
MACHINE_IMAGE = REPO / "firmware" / "40.71.003" / "1.Machine" / "800W_Machine_APP_V40.71.003.2425.01.B04.bin"
SCREEN_IMAGE = REPO / "firmware" / "40.71.003" / "2.Screen" / "800W_Screen_V40.71.003.2540.B03.bin"

# Proven popup-to-page map (docs/protocol-notes.md, wire-feeding alert mapping):
# popup id -> screen page -> expected on-screen title.
KNOWN_MAP = {
    1: (35, "Gas pressure too low"),
    2: (36, "Gas pressure too high"),
    6: (39, "Wire feeding module anomaly detected"),
    8: (41, "Lens protector temperature exceptions"),
    9: (42, "Welding head communication exceptions"),
}


def _load_tool(name: str):
    """Import a tools/*.py script without it being a package."""
    spec = importlib.util.spec_from_file_location(name, REPO / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_images_are_present():
    assert MACHINE_IMAGE.is_file()
    assert SCREEN_IMAGE.is_file()


def test_popup_table_matches_proven_mappings():
    tool = _load_tool("decode_popup_table")
    table = tool.decode_popup_table(MACHINE_IMAGE.read_bytes())
    for popup, (page, _title) in KNOWN_MAP.items():
        assert table.get(popup) == page, f"popup {popup} should map to page {page}"


def test_popup_pages_contain_documented_titles():
    """Cross-verify: the decoded page for each popup must carry the documented text."""
    popups = _load_tool("decode_popup_table")
    screens = _load_tool("screen_unpack")
    table = popups.decode_popup_table(MACHINE_IMAGE.read_bytes())
    pages = screens.extract_pages(SCREEN_IMAGE.read_bytes())
    for popup, (page, title) in KNOWN_MAP.items():
        target = table.get(popup)
        assert target == page
        joined = "\n".join(pages[target]["segments"])
        assert title in joined, f"page {target} (popup {popup}) lacks {title!r}"
