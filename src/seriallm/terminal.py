from __future__ import annotations

import contextlib
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
    def __init__(self, raw: bool = False) -> None:
        self._raw = raw
        self._prev_cr = False

    def filter(self, data: bytes) -> bytes:
        if self._raw:
            return data

        out = bytearray()
        for byte in data:
            if self._prev_cr:
                self._prev_cr = False
                if byte != ord("\n"):
                    out.append(ord("\r"))
                    out.append(ord("\n"))
                    if byte == ord("\r"):
                        self._prev_cr = True
                        continue
                else:
                    out.append(ord("\r"))
                    out.append(ord("\n"))
                    continue
            elif byte == ord("\r"):
                self._prev_cr = True
                continue
            elif byte == ord("\n"):
                out.append(ord("\r"))
                out.append(ord("\n"))
                continue

            # Filter control codes, keep printable + tab + ESC
            if byte < 0x20 and byte not in (0x09, 0x1B):
                continue
            if byte == 0x7F:
                continue
            out.append(byte)

        return bytes(out)

    def flush_pending(self) -> bytes:
        if self._prev_cr:
            self._prev_cr = False
            return b"\r\n"
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


def write_output(data: bytes, output_filter: OutputFilter) -> None:
    filtered = output_filter.filter(data)
    if filtered:
        sys.stdout.buffer.write(filtered)
        sys.stdout.buffer.flush()


def write_status(msg: str) -> None:
    sys.stderr.write(msg)
    sys.stderr.flush()
