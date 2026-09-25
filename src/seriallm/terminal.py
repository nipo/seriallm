from __future__ import annotations

import contextlib
import datetime
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import msvcrt
else:
    import termios
    import tty

QUIT_KEY = 0x1D  # Ctrl+]


class OutputFilter:
    """Normalizes line endings and filters control codes of device output.

    With `timestamps` set to "abs" or "rel", each line is prefixed with the
    receive time of its first byte, as wall time or as a delta against the
    previous line. The prefix is only emitted when that first byte is output,
    so it never carries the time of the preceding line terminator.
    """

    def __init__(self, raw: bool = False, timestamps: str | None = None) -> None:
        assert timestamps in (None, "abs", "rel")
        assert not (raw and timestamps)
        self.raw = raw
        self.timestamps = timestamps
        self.prev_cr = False
        self.prev_cr_time = 0.0
        self.at_line_start = True
        self.prev_line_time: float | None = None

    def mark_line_start(self) -> None:
        """Record that the cursor was moved to a new line by other output."""
        self.at_line_start = True

    def __prefix(self, out: bytearray, timestamp: float) -> None:
        if self.timestamps is None or not self.at_line_start:
            return
        if self.timestamps == "abs":
            dt = datetime.datetime.fromtimestamp(timestamp)
            text = dt.strftime("[%H:%M:%S.") + f"{dt.microsecond // 1000:03d}] "
        else:
            delta = 0.0 if self.prev_line_time is None else timestamp - self.prev_line_time
            text = f"[+{delta:8.3f}] "
        self.prev_line_time = timestamp
        out.extend(text.encode())

    def __emit(self, out: bytearray, byte: int, timestamp: float) -> None:
        self.__prefix(out, timestamp)
        self.at_line_start = False
        out.append(byte)

    def __emit_eol(self, out: bytearray, timestamp: float) -> None:
        self.__prefix(out, timestamp)
        self.at_line_start = True
        out.extend(b"\r\n")

    def filter(self, data: bytes, timestamp: float) -> bytes:
        if self.raw:
            return data

        out = bytearray()
        for byte in data:
            if self.prev_cr:
                self.prev_cr = False
                self.__emit_eol(out, self.prev_cr_time)
                if byte == ord("\n"):
                    continue
                if byte == ord("\r"):
                    self.prev_cr = True
                    self.prev_cr_time = timestamp
                    continue
            elif byte == ord("\r"):
                self.prev_cr = True
                self.prev_cr_time = timestamp
                continue
            elif byte == ord("\n"):
                self.__emit_eol(out, timestamp)
                continue

            # Filter control codes, keep printable + tab + ESC
            if byte < 0x20 and byte not in (0x09, 0x1B):
                continue
            if byte == 0x7F:
                continue
            self.__emit(out, byte, timestamp)

        return bytes(out)

    def flush_pending(self) -> bytes:
        if self.prev_cr:
            self.prev_cr = False
            out = bytearray()
            self.__emit_eol(out, self.prev_cr_time)
            return bytes(out)
        return b""


@contextlib.contextmanager
def raw_terminal() -> Generator[None]:
    if _IS_WINDOWS:
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, old)


def read_stdin_byte() -> bytes:
    """Read a single byte from stdin, blocking. Works in raw mode on both platforms."""
    if _IS_WINDOWS:
        return msvcrt.getch()
    return sys.stdin.buffer.read(1)


def write_output(data: bytes, timestamp: float, output_filter: OutputFilter) -> None:
    filtered = output_filter.filter(data, timestamp)
    if filtered:
        sys.stdout.buffer.write(filtered)
        sys.stdout.buffer.flush()


def write_status(msg: str) -> None:
    sys.stderr.write(msg)
    sys.stderr.flush()
