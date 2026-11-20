#!/usr/bin/env python3
"""Unpack the 55AA screen container and extract multilingual page text.

Layout (verified against 800W_Screen_V40.71.003.2540.B03.bin):
- 55AA container: header, named entries ("3:/bin", "3:/bin/image.bin"), then
  19 consecutive zlib streams from offset 62 (3 inflate to exactly 1,572,864
  bytes = SPI-flash images; 16 are 54-150 KB asset blobs).
- The screen.bin page bundle is stored RAW in the container (its "DC10"
  marker sits directly in the container bytes at file offset 0x2508dd for
  this image), not inside a zlib stream. At DC10+0x88 begins the page index:
  83 entries of (u32 length, u32 offset), offsets relative to the DC10
  marker itself. After the index (664 bytes) comes a 7-entry language table
  (56 bytes); the page payloads follow. Each page payload is NUL-separated
  multilingual UTF-8 text.
"""

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

SPI_IMAGE_SIZE = 1572864
STREAM_START = 62
STREAM_COUNT = 19
PAGE_INDEX_OFFSET = 0x88  # relative to the DC10 marker
PAGE_ENTRY = struct.Struct("<II")  # little-endian u32 length, u32 offset
PAGE_COUNT_OFFSET = 0x10  # u32 page count inside the DC10 header


def inflate_streams(data: bytes) -> list[bytes]:
    """Inflate the 19 consecutive zlib streams starting at STREAM_START.

    Each stream is inflated independently; the decompressor's unused_data
    reports exactly where the stream ended, so the next one begins there.
    """
    streams = []
    position = STREAM_START
    while len(streams) < STREAM_COUNT and position < len(data) - 4:
        try:
            decompressor = zlib.decompressobj()
            stream = decompressor.decompress(data[position:]) + decompressor.flush()
        except zlib.error:
            break
        streams.append(stream)
        position += (len(data) - position) - len(decompressor.unused_data)
    return streams


def extract_pages(data: bytes) -> dict[int, dict]:
    """Extract per-page text from the raw screen.bin bundle inside the container.

    Returns {page_number: {"segments": [str], "offset": int, "length": int}}
    where segments are the page's NUL-separated multilingual strings. Entry
    offsets are relative to the DC10 marker, matching the documented file
    offsets (e.g. page 39's region begins at 0x2725b0).
    """
    marker = data.find(b"DC10")
    if marker < 0:
        raise ValueError("DC10 page bundle not found")
    page_count = struct.unpack_from("<I", data, marker + PAGE_COUNT_OFFSET)[0]
    pages: dict[int, dict] = {}
    for number in range(page_count):
        length, offset = PAGE_ENTRY.unpack_from(
            data, marker + PAGE_INDEX_OFFSET + number * PAGE_ENTRY.size,
        )
        if length == 0:
            continue
        chunk = data[marker + offset:marker + offset + length]
        segments = [part.decode("utf-8", "ignore") for part in chunk.split(b"\x00")]
        segments = [segment.strip() for segment in segments if segment.strip()]
        pages[number] = {
            "segments": segments,
            "offset": marker + offset,
            "length": length,
        }
    return pages


def _report_streams(data: bytes, as_json: bool) -> None:
    streams = inflate_streams(data)
    summary = [{"index": index, "size": len(stream)} for index, stream in enumerate(streams)]
    if as_json:
        print(json.dumps(summary, indent=2))
        return
    for entry in summary:
        print(f"stream {entry['index']:2d}: {entry['size']} bytes")


def _report_pages(data: bytes, as_json: bool) -> None:
    pages = extract_pages(data)
    if as_json:
        print(json.dumps({str(number): page for number, page in pages.items()}, indent=2))
        return
    for number in sorted(pages):
        page = pages[number]
        preview = page["segments"][0][:60] if page["segments"] else ""
        print(f"page {number:2d} @ 0x{page['offset']:x} ({page['length']} bytes): {preview}")


def _report_page(data: bytes, number: int, as_json: bool) -> None:
    pages = extract_pages(data)
    page = pages.get(number)
    if page is None:
        raise SystemExit(f"page {number} not found")
    if as_json:
        print(json.dumps(page, indent=2))
        return
    for segment in page["segments"]:
        print(segment)
        print("---")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="55AA screen container .bin")
    parser.add_argument("--streams", action="store_true", help="list the 19 zlib streams")
    parser.add_argument("--pages", action="store_true", help="list all pages with a preview")
    parser.add_argument("--page", type=int, metavar="N", help="dump one page's full text")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    data = args.image.read_bytes()
    if args.streams:
        _report_streams(data, args.json)
    elif args.page is not None:
        _report_page(data, args.page, args.json)
    elif args.pages:
        _report_pages(data, args.json)
    else:
        parser.print_help()


if __name__ == "__main__":
    main(sys.argv[1:])
