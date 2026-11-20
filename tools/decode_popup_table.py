#!/usr/bin/env python3
"""Decode the popup table from the machine image's scatter-load init data.

Layout (verified against 800W_Machine_APP_V40.71.003.2425.01.B04.bin):

- Image offset 0x34344 holds the scatter-load descriptor table. Each region
  entry is 16 bytes: {data_ptr, exec_addr, size, decompressor_fn} with RUNTIME
  addresses (load base 0x08010000, so image offset = runtime - 0x08010000).
  Region 1 = {0x08044364 -> image 0x34364, exec 0x20000000, size 0xc620,
  fn 0x080114c0 -> image 0x14c0 = FUN_080014c0}. Its compressed stream is the
  bytes from 0x34364 to the image end.

- FUN_080014c0 (re/machine/target_080014C0.c, verified against the actual
  assembly) is an LZ token decoder. Each token byte:
    bits 0-2  literal field: 1-7 copies (field - 1) literals; 0 reads an
              escape byte that copies (byte - 1) literals
    bit 3     back-reference flag
    bits 4-7  match field: 0 reads an escape byte with the raw count;
              otherwise the raw count
    zero branch: emit `match` zero bytes
    backref branch: copy (match + 2) bytes from `dst - next_byte`

- RAM 0x2000c384 (= output offset 0xc384) holds the popup table: 8-byte
  entries of (u16 popup_id, u16 page_id), terminated by (0, 0). Verified
  against the proven mappings popup 6->page 39, 9->42, 8->41, 1->35, 2->36.
"""

import argparse
import json
import struct
import sys
from pathlib import Path

DESCRIPTOR_OFFSET = 0x34344
REGION_ONE = {"src": 0x08044364, "exec": 0x20000000, "size": 0xC620, "fn": 0x080114C0}
POPUP_TABLE_RAM = 0x2000C384
LOAD_BASE = 0x08010000
TABLE_BYTES = 0x400  # scan window past the table start


def decompress(src: bytes, size: int) -> bytes:
    """Emulate FUN_080014c0: LZ tokens -> exactly `size` output bytes."""
    out = bytearray()
    position = 0
    while len(out) < size and position < len(src) - 1:
        token = src[position]
        literal = token & 7
        position += 1
        if literal == 0:
            # Escape byte stores count + 1 (field values 1-7 also store +1).
            literal = src[position] - 1
            position += 1
        else:
            literal -= 1
        match = token >> 4
        if match == 0:
            match = src[position]
            position += 1
        if position + literal > len(src):
            raise ValueError("stream truncated in literal run")
        out += src[position:position + literal]
        position += literal
        if token & 0x08:
            if position >= len(src):
                raise ValueError("stream truncated before distance byte")
            distance = src[position]
            position += 1
            ref = len(out) - distance
            if ref < 0:
                raise ValueError("back-reference before output start")
            for _ in range(match + 2):
                out.append(out[ref])
                ref += 1
        else:
            out += b"\x00" * match
    if len(out) < size:
        raise ValueError(f"stream exhausted at {len(out)} of {size} bytes")
    return bytes(out[:size])


def decode_popup_table(image: bytes) -> dict[int, int]:
    """Return {popup_id: page_id} decoded from the scatter-load init data."""
    descriptor = REGION_ONE
    src_offset = descriptor["src"] - LOAD_BASE
    if image.find(b"", src_offset) < 0:  # pragma: no cover - structural check
        raise ValueError("descriptor source beyond image end")
    ram = decompress(image[src_offset:], descriptor["size"])
    table_offset = POPUP_TABLE_RAM - descriptor["exec"]
    table: dict[int, int] = {}
    for entry_offset in range(0, TABLE_BYTES, 8):
        popup, page = struct.unpack_from("<HH", ram, table_offset + entry_offset)
        if popup == 0 and page == 0:
            break
        table[popup] = page
    if not table:
        raise ValueError("popup table empty - descriptor or offsets are wrong")
    return table


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="machine application .bin")
    parser.add_argument("--json", action="store_true", help="emit data/popups.json shape")
    parser.add_argument("--out", type=Path, help="write JSON to this file (default: stdout)")
    args = parser.parse_args(argv)

    table = decode_popup_table(args.image.read_bytes())
    payload = {
        "version": 1,
        "source": {
            "image": args.image.name,
            "descriptor_offset": DESCRIPTOR_OFFSET,
            "ram_address": POPUP_TABLE_RAM,
        },
        "popups": [{"popup": popup, "page": page} for popup, page in sorted(table.items())],
    }
    if args.out:
        args.out.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {args.out} with {len(table)} popups")
    else:
        print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
