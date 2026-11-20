"""Capture M808 status reports while physical states change.

Polls M808 once per second, timestamps every line the machine emits, and writes
JSONL under re/state-map/. The named fields identify which condition changed; the
two Raw Status Value words supply the bits to correlate against those changes.
"""

import argparse
import datetime
import json
import pathlib
import time

import serial


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/cu.usbmodem3770DAA904B61")
    parser.add_argument("--duration", type=float, default=480.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    if args.out:
        out = pathlib.Path(args.out)
    else:
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        out = pathlib.Path(__file__).resolve().parent.parent / "re" / "state-map" / f"{stamp}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    port = serial.Serial()
    port.port = args.port
    port.baudrate = 230400
    port.timeout = 0.1
    port.write_timeout = 2
    # DTR/RTS off so opening the port cannot reset a board; exclusive so a second
    # client cannot interleave bytes with the capture.
    port.dsrdtr = False
    port.rtscts = False
    port.exclusive = True
    port.open()

    start = time.time()
    last_poll = 0.0
    buffer = b""
    with out.open("w", encoding="utf-8") as handle:
        def log(record: dict) -> None:
            handle.write(json.dumps(record) + "\n")
            handle.flush()

        try:
            while time.time() - start < args.duration:
                now = time.time()
                if now - last_poll >= args.interval:
                    port.write(b"M808\n")
                    port.flush()
                    last_poll = now
                    log({"t": round(now - start, 3), "kind": "poll"})
                chunk = port.read(port.in_waiting or 1)
                if not chunk:
                    time.sleep(0.02)
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    text = line.decode("ascii", "replace").strip()
                    if text:
                        log({"t": round(time.time() - start, 3), "kind": "line", "text": text})
                        print(f"{time.time() - start:7.2f}  {text}", flush=True)
        finally:
            port.close()
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
