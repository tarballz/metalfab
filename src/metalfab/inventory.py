"""Inspect stock firmware files locally, without opening a serial port."""

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import BinaryIO
from zipfile import ZipFile


BOARD_NAMES = {"2425": "machine", "2540": "screen", "2622": "laser_control", "2722": "welding_head", "5035": "wire_feeder"}
HJ004_MACHINE_VERSION = "40.71.003.2425.01.B04"
STOCK_HEAD_VERSION = "40.70.006.2722.01.B01"
_STOCK_HEAD_SHA256 = "24a248b7d290e753c0de7a33f2d2fb5932e8e4dbd5f7301fd6e79fe3d10617cc"
_STOCK_HEAD_SIZE = 24_836
_VERSION = re.compile(r"V(?P<version>\d+\.\d+\.\d+\.(?P<code>\d+)(?:\.(?:\d+|B\d+))*)", re.I)
_MAX_IMAGE = 256 * 1024 * 1024
_APP_VERSION = re.compile(r"(\d{2})\.(\d{2})\.(\d{3})\.(\d{4})\.(\d{2})\.B(\d+)", re.ASCII)


@dataclass(frozen=True)
class StockImage:
    """Board identity and reported version for one publisher-matched image."""

    board: str
    version: str


# Each digest was independently matched against the publisher's HJ004 40.71.003 ZIP.
# Version strings here use the device's reporting format, including the screen's .01 suffix.
_STOCK_IMAGES = {
    "5f312f8cdae9f75f33cb791e277320eed6879ab324c767e85bd123c049c6d65e":
        StockImage("machine", HJ004_MACHINE_VERSION),
    "3ad13a3b45c6d861c53aa0919fb4163c7f7c38c6e2d367d32c2f400bbd597b2b":
        StockImage("screen", "40.71.003.2540.01.B03"),
    "09ba2d0164edb827322447426bd9a8f7e3cd8b6b22dfceeca8a0ded62e543379":
        StockImage("laser_control", "40.71.001.2622.01.B01"),
    _STOCK_HEAD_SHA256: StockImage("welding_head", STOCK_HEAD_VERSION),
}


def _image(name: str, size: int, stream: BinaryIO) -> dict:
    if size > _MAX_IMAGE:
        raise ValueError(f"Firmware image exceeds the inspection limit: {name}")
    digest = hashlib.sha256()
    total = 0
    while block := stream.read(64 * 1024):
        total += len(block)
        if total > _MAX_IMAGE:
            raise ValueError(f"Firmware image exceeds the inspection limit: {name}")
        digest.update(block)
    if total != size:
        raise ValueError(f"Firmware image size changed during inspection: {name}")
    match = _VERSION.search(PurePosixPath(name).name)
    return {
        "file": name,
        "board": BOARD_NAMES.get(match["code"]) if match else None,
        "version": match["version"] if match else None,
        "size": total,
        "sha256": digest.hexdigest(),
    }


def inspect_firmware(path: Path) -> Iterable[dict]:
    """Report filenames, inferred board versions, and hashes without extracting archives."""
    if path.is_file() and path.suffix.lower() == ".zip":
        with ZipFile(path) as archive:
            for member in sorted(archive.infolist(), key=lambda item: item.filename):
                if not member.is_dir() and member.filename.lower().endswith(".bin"):
                    with archive.open(member) as stream:
                        yield _image(member.filename, member.file_size, stream)
        return
    candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix.lower() == ".bin":
            with candidate.open("rb") as stream:
                name = str(candidate.relative_to(path)) if path.is_dir() else candidate.name
                yield _image(name, candidate.stat().st_size, stream)


def stock_images(path: Path) -> dict[str, dict]:
    """Identify official HJ004 images by content before connecting to the device."""
    images = {}
    for record in inspect_firmware(path):
        stock = _STOCK_IMAGES.get(record["sha256"])
        if stock is None:
            raise ValueError(f"Firmware file does not match the reviewed HJ004 bundle: {record['file']}")
        if stock.board in images:
            raise ValueError(f"Multiple firmware images for {stock.board}")
        images[stock.board] = {**record, "board": stock.board, "version": stock.version}
    if not images:
        raise ValueError("No .bin firmware images found")
    return images


def verify_stock_head(payload: bytes) -> None:
    """Require the exact, immutable welding-head application from the HJ004 release."""
    if (type(payload) is not bytes or len(payload) != _STOCK_HEAD_SIZE
            or hashlib.sha256(payload).hexdigest() != _STOCK_HEAD_SHA256):
        raise ValueError("Welding-head image does not match the stock HJ004 release")


def load_stock_head(path: Path) -> bytes:
    """Load the stock head image from a .bin, release directory, or ZIP."""
    head = stock_images(path).get("welding_head")
    if head is None:
        raise ValueError("The input contains no stock HJ004 welding-head image")
    if path.is_file() and path.suffix.lower() == ".zip":
        with ZipFile(path) as archive:
            try:
                with archive.open(head["file"]) as stream:
                    payload = stream.read(_STOCK_HEAD_SIZE + 1)
            except KeyError as error:
                raise ValueError("Welding-head ZIP entry disappeared after inspection") from error
    else:
        image = path / head["file"] if path.is_dir() else path
        with image.open("rb") as stream:
            payload = stream.read(_STOCK_HEAD_SIZE + 1)
    # Verify the bytes actually retained for transfer, even if the file changed
    # after the inventory pass.
    verify_stock_head(payload)
    return payload


def _version_parts(version: str) -> tuple[int, ...]:
    match = _APP_VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"Invalid installed application version {version!r}")
    return tuple(map(int, match.groups()))


def _installed_app(installed: dict, board: str) -> str:
    row = installed.get(board)
    if not isinstance(row, dict) or not isinstance(row.get("app"), str):
        raise ValueError(f"Missing installed application version for {board}")
    return row["app"]


def _board_identity(version: tuple[int, ...]) -> tuple[int, int, int]:
    return version[0], version[1], version[3]


def firmware_plan(images: dict[str, dict], installed: dict) -> list[dict]:
    """Compare exact stock images against board versions reported by the HJ004."""
    if not isinstance(installed, dict):
        raise ValueError("Expected JSON from 'metalfab firmware versions'")
    machine = _version_parts(_installed_app(installed, "machine"))
    if _board_identity(machine) != _board_identity(_version_parts(HJ004_MACHINE_VERSION)):
        raise ValueError("Installed version data must identify a 40.71 HJ004 machine")
    result = []
    for board, image in images.items():
        current = _installed_app(installed, board)
        installed_parts = _version_parts(current)
        target_parts = _version_parts(image["version"])
        # These slots identify the device family and the board before comparison.
        if _board_identity(installed_parts) != _board_identity(target_parts):
            raise ValueError(f"Installed firmware {current!r} is incompatible with the {board} image")
        action = "current"
        if target_parts > installed_parts:
            action = "update"
        elif target_parts < installed_parts:
            action = "downgrade"
        result.append({
            "board": board, "file": image["file"], "sha256": image["sha256"],
            "installed": current, "target": image["version"], "action": action,
        })
    return result
