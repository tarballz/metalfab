"""Exercise the screen-bundle unpacker against the shipped image."""

import importlib.util
from pathlib import Path
import pytest


REPO = Path(__file__).resolve().parent.parent


pytestmark = pytest.mark.skipif(
    not (REPO / "firmware").exists(),
    reason="vendor firmware not present; obtain the HJ004 images first",
)
SCREEN_IMAGE = REPO / "firmware" / "40.71.003" / "2.Screen" / "800W_Screen_V40.71.003.2540.B03.bin"


def _load_tool():
    """Import tools/screen_unpack.py without it being a package."""
    spec = importlib.util.spec_from_file_location(
        "screen_unpack", REPO / "tools" / "screen_unpack.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_screen_image_is_present():
    assert SCREEN_IMAGE.is_file()


def test_inflates_nineteen_streams():
    tool = _load_tool()
    streams = tool.inflate_streams(SCREEN_IMAGE.read_bytes())
    assert len(streams) == 19
    # Three streams are SPI-flash images of exactly 1,572,864 bytes.
    assert sum(1 for stream in streams if len(stream) == 1572864) == 3


def test_page_bundle_located_at_known_offset():
    data = SCREEN_IMAGE.read_bytes()
    assert data.find(b"DC10") == 0x2508DD


def test_page_39_contains_known_wire_feeding_text():
    """Page 39 is the proven wire-feeding alert: its title must match the photo."""
    tool = _load_tool()
    pages = tool.extract_pages(SCREEN_IMAGE.read_bytes())
    assert 39 in pages
    joined = "\n".join(pages[39]["segments"])
    assert "Wire feeding module anomaly detected" in joined
