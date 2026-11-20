"""Transfer the one reviewed HJ004 welding-head application over an open serial session."""

from collections.abc import Callable
import time
from zlib import crc32

from .inventory import verify_stock_head


SOURCE_HEAD_VERSION = "40.70.005.2722.01.B02"
HEAD_BOOTLOADER = "40.70.001.2722.00.B01"

_HEAD = 0x27
_SERVICE = 0x61
_ACK = bytes((_HEAD, 0x15, 0x0F))
_CHUNK_SIZE = 64
_MAX_MESSAGE = 4096


class HeadTransferError(RuntimeError):
    """The welding-head transfer stopped before successful completion."""


class _ReplyTimeout(HeadTransferError):
    """No matching reply arrived before the stage deadline."""


def _seven_bit(value: int, width: int) -> bytes:
    """Encode a number as the bootloader's little-endian seven-bit groups."""
    return bytes((value >> shift) & 0x7F for shift in range(0, width * 7, 7))


class _HeadTransfer:
    """Read the mixed line and binary stream while the head is in upgrade mode."""

    def __init__(self, read: Callable[[int], bytes], write: Callable[[bytes], int]):
        self._read = read
        self._write = write
        self._buffer = bytearray()

    def _send(self, data: bytes) -> None:
        if self._write(data) != len(data):
            raise HeadTransferError("Serial write was incomplete")

    def _message(self, deadline: float) -> tuple[bool, bytes]:
        while time.monotonic() < deadline:
            start = self._buffer.find(0xF0)
            newline = self._buffer.find(0x0A)
            if newline >= 0 and (start < 0 or newline < start):
                line = bytes(self._buffer[:newline]).rstrip(b"\r")
                del self._buffer[:newline + 1]
                return False, line
            if start >= 0:
                end = self._buffer.find(0xF7, start + 1)
                if end >= 0:
                    body = bytes(self._buffer[start + 1:end - 1])
                    checksum = self._buffer[end - 1]
                    del self._buffer[:end + 1]
                    if not body or checksum != (sum(body) & 0x7F):
                        raise HeadTransferError("Invalid checksum or framing in the head update reply")
                    return True, body
            if len(self._buffer) > _MAX_MESSAGE:
                raise HeadTransferError("Oversized message on the head update connection")
            self._buffer.extend(self._read(256))
        # A reset can leave a partial binary frame. Drop it before the next
        # bootloader probe or the line-based service exit.
        self._buffer.clear()
        raise _ReplyTimeout("Timed out waiting for the device reply")

    def _line(self, command: bytes, *expected: bytes) -> None:
        self._send(command + b"\n")
        deadline = time.monotonic() + 5
        while True:
            binary, value = self._message(deadline)
            if binary:
                continue
            if value in expected:
                return
            if value.startswith(b"M222 ") and command == b"M222":
                raise HeadTransferError("The welder did not report idle service mode")

    def _exchange(
        self, command: bytes, *, ack: bool = True, timeout: float = 3, attempts: int = 1,
    ) -> bytes:
        # Each binary command is delimited by a newline inside the checksummed frame.
        body = bytes((_HEAD,)) + command + b"\n"
        request = b"\xf0" + body + bytes((sum(body) & 0x7F, 0xF7))
        for attempt in range(attempts):
            self._send(request)
            deadline = time.monotonic() + timeout
            received_ack = not ack
            try:
                while True:
                    binary, reply = self._message(deadline)
                    if not binary or reply[0] != _HEAD:
                        continue
                    if not received_ack:
                        if not reply.startswith(_ACK):
                            raise HeadTransferError("The welding head rejected a service request")
                        received_ack = True
                    elif reply[1:2] != _ACK[1:2]:
                        return reply
            except _ReplyTimeout:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.5)
        raise AssertionError("Exchange requires at least one attempt")

    def run(self, image: bytes) -> None:
        """Write the validated head application, requiring a reply at each stage."""
        verify_stock_head(image)
        stage = "entering service mode"
        try:
            self._line(b"M222", b"M222 S1", b"M222 S3")
            self._line(b"M222 S6", b"M222 S6")
            time.sleep(0.5)

            stage = "identifying the welding head"
            identity = self._exchange(b"\x10\x00", ack=False, attempts=5)
            if len(identity) < 3 or (identity[1] == 0) != (identity[2] == 0):
                raise HeadTransferError("Unrecognized welding-head mode reply")
            if identity[1] != 0:
                try:
                    self._exchange(bytes((_SERVICE, 5, 115, _HEAD)), timeout=2)
                except _ReplyTimeout:
                    # The application can reset before it sends a complete reply.
                    pass
            stage = "confirming the welding-head bootloader"
            self._exchange(bytes((_SERVICE, 17)), attempts=5)
            identity = self._exchange(bytes((_SERVICE, 6)), attempts=5)
            if not identity.startswith(bytes((_HEAD, _SERVICE, 6))):
                raise HeadTransferError("Bootloader reply did not identify the welding-head board")

            stage = "sending the application header"
            prefix = bytes((_SERVICE, 7))
            metadata = _seven_bit(len(image), 5) + _seven_bit(crc32(image), 5)
            self._exchange(prefix + b"\x00\x00" + metadata, timeout=2)
            for index, offset in enumerate(range(0, len(image), _CHUNK_SIZE), start=1):
                stage = f"sending application block {index}"
                number = _seven_bit(index, 2)
                block = image[offset:offset + _CHUNK_SIZE]
                data = b"".join(_seven_bit(byte, 2) for byte in block)
                reply = self._exchange(prefix + number + _seven_bit(len(data), 2) + data)
                # An ACK for a different block cannot confirm progress.
                if reply[3:5] != number:
                    raise HeadTransferError("Welding head acknowledged a different application block")

            stage = "checking the completed transfer"
            status = self._exchange(bytes((_SERVICE, 8, 0)))
            if status[3:4] != b"\x01":
                raise HeadTransferError("The bootloader did not report a successful update")
            stage = "rebooting the welding head"
            try:
                self._exchange(b"\x11\x00", timeout=2)
            except _ReplyTimeout:
                # Reboot can finish the serial exchange before a reply arrives.
                pass
            stage = "exiting service mode"
            self._line(b"M222 S8", b"M222 S8")
        except (HeadTransferError, OSError, KeyboardInterrupt) as error:
            cleanup = "The welder acknowledged the service-mode exit."
            try:
                self._buffer.clear()
                self._line(b"M222 S7", b"M222 S7")
            except (HeadTransferError, OSError, KeyboardInterrupt):
                cleanup = "Service-mode exit could not be confirmed."
            reason = str(error) or type(error).__name__
            raise HeadTransferError(f"Stopped while {stage}. {reason}. {cleanup}") from error


def transfer_stock_head(read: Callable[[int], bytes], write: Callable[[bytes], int], image: bytes) -> None:
    """Send the pinned application to the fixed head target on an existing connection."""
    _HeadTransfer(read, write).run(image)
