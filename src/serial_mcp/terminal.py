from __future__ import annotations

import contextlib
import sys
import termios
import tty
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from collections.abc import Generator

    from serial_mcp.state import AppState, PortState

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
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, old)


def write_output(data: bytes, output_filter: OutputFilter) -> None:
    filtered = output_filter.filter(data)
    if filtered:
        sys.stdout.buffer.write(filtered)
        sys.stdout.buffer.flush()


def write_status(msg: str) -> None:
    sys.stderr.write(msg)
    sys.stderr.flush()


async def stdin_reader_task(port: PortState, app: AppState) -> None:
    from serial_mcp.serial_io import serial_send

    def _read_one() -> bytes:
        return sys.stdin.buffer.read(1)

    while not app.shutdown_event.is_set():
        data = await anyio.to_thread.run_sync(_read_one, abandon_on_cancel=True)
        if not data:
            app.shutdown_event.set()
            return
        if data[0] == QUIT_KEY:
            app.shutdown_event.set()
            return
        try:
            await serial_send(port, data)
        except RuntimeError:
            pass  # port disconnected, ignore keystroke
