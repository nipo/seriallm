from __future__ import annotations

import re

import anyio
from mcp.server import FastMCP

from serial_mcp.serial_io import serial_send
from serial_mcp.state import AppState, PortState

mcp = FastMCP("serial-mcp")

_app_state: AppState | None = None


def set_app_state(state: AppState) -> None:
    global _app_state
    _app_state = state


def _get_port(port_id: str = "default") -> PortState:
    assert _app_state is not None, "AppState not initialized"
    if port_id not in _app_state.ports:
        raise ValueError(f"Unknown port: {port_id!r}")
    return _app_state.ports[port_id]


@mcp.tool()
def read_serial(
    since: int = 0,
    up_to: int | None = None,
    port_id: str = "default",
) -> dict:
    """Read data received from the serial port.

    This server maintains a ring buffer of all bytes received. Each byte has an
    absolute offset that starts at 0 when the server launches and only increases
    (it never resets or wraps). The response `end` value is the offset to pass
    as `since` on the next call to get only new data.

    IMPORTANT: To follow the stream without re-reading or missing data, always
    use the `end` value from the previous response as `since` for the next call.

    Parameters:
    - since: read data starting from this absolute byte offset (default: 0 = from start).
    - up_to: stop reading at this absolute byte offset (exclusive). Omit to get
      everything available.

    Returns {data, start, end}:
    - data: the received text (UTF-8, lossy).
    - start: actual start offset of returned data. If start > since, older data
      was evicted from the buffer.
    - end: offset just past the last byte returned. Use this as `since` next time.
    """
    port = _get_port(port_id)
    data, start, end = port.buffer.read(since, up_to)
    return {
        "data": data.decode("utf-8", errors="replace"),
        "start": start,
        "end": end,
    }


@mcp.tool()
async def send(data: str, port_id: str = "default") -> str:
    """Send a UTF-8 string to the serial port.

    The sent data will be echoed back by read_serial only if the device echoes it.
    """
    port = _get_port(port_id)
    await serial_send(port, data.encode("utf-8"))
    return "ok"


@mcp.tool()
async def send_bytes(hex_data: str, port_id: str = "default") -> str:
    """Send raw bytes to the serial port.

    `hex_data` is a hex-encoded string, e.g. "0d0a" sends CR LF.
    """
    port = _get_port(port_id)
    await serial_send(port, bytes.fromhex(hex_data))
    return "ok"


@mcp.tool()
async def wait_for(
    pattern: str,
    since: int = 0,
    timeout: float = 10.0,
    port_id: str = "default",
) -> dict:
    """Wait for a regex pattern to appear in serial output.

    Blocks until the pattern matches or timeout expires. The search considers
    all buffered data from absolute byte offset `since` onward, including data
    that has already been received AND data that arrives while waiting.

    IMPORTANT: `since` uses the same absolute byte offset as read_serial. Use
    the `end` value from a previous read_serial call to only search new data,
    or 0 to search from the beginning of the buffer.

    Returns {offset, end, match}:
    - offset: absolute byte offset where the match starts.
    - end: absolute byte offset just past the match.
    - match: the matched text.

    Use read_serial(since=..., up_to=...) to retrieve context around the match.
    After processing, use `end` from read_serial as `since` for subsequent calls.
    """
    port = _get_port(port_id)
    regex = re.compile(pattern)

    with anyio.fail_after(timeout):
        async with port.condition:
            while True:
                data, start, end = port.buffer.read(since)
                if data:
                    text = data.decode("utf-8", errors="replace")
                    m = regex.search(text)
                    if m:
                        # Byte offsets: encode matched prefix to get byte position
                        prefix_bytes = len(text[: m.start()].encode("utf-8", errors="replace"))
                        match_bytes = len(m.group(0).encode("utf-8", errors="replace"))
                        return {
                            "offset": start + prefix_bytes,
                            "end": start + prefix_bytes + match_bytes,
                            "match": m.group(0),
                        }
                await port.condition.wait()


@mcp.tool()
def set_control_lines(
    dtr: bool | None = None,
    rts: bool | None = None,
    port_id: str = "default",
) -> str:
    """Set DTR and/or RTS control lines on the serial port."""
    port = _get_port(port_id)
    if port.serial_port is None or not port.connected:
        raise RuntimeError("Port not connected")
    if dtr is not None:
        port.serial_port.dtr = dtr
    if rts is not None:
        port.serial_port.rts = rts
    return "ok"


@mcp.tool()
async def send_break(
    duration: float = 0.25,
    port_id: str = "default",
) -> str:
    """Send a break signal on the serial port."""
    port = _get_port(port_id)
    if port.serial_port is None or not port.connected:
        raise RuntimeError("Port not connected")
    ser = port.serial_port
    await anyio.to_thread.run_sync(lambda: ser.send_break(duration))
    return "ok"


@mcp.tool()
def get_port_info(port_id: str = "default") -> dict:
    """Get serial port status: baud rate, control lines, buffer offsets, connection state.

    The buffer_end value is the current absolute byte offset (total bytes
    received since server start). Use it as `since` in read_serial to start
    reading from "now" (ignoring past data).
    """
    port = _get_port(port_id)
    info: dict = {
        "url": port.url,
        "baudrate": port.baudrate,
        "connected": port.connected,
        "buffer_start": port.buffer.start_offset,
        "buffer_end": port.buffer.end_offset,
    }
    if port.serial_port is not None and port.connected:
        try:
            info.update(
                {
                    "cts": port.serial_port.cts,
                    "dsr": port.serial_port.dsr,
                    "ri": port.serial_port.ri,
                    "cd": port.serial_port.cd,
                    "dtr": port.serial_port.dtr,
                    "rts": port.serial_port.rts,
                }
            )
        except Exception:
            pass  # some port types don't support control lines
    return info


@mcp.tool()
def get_port_events(
    since: int = 0,
    port_id: str = "default",
) -> list[dict]:
    """Get connection/disconnection events for a serial port.

    Returns a list of events (oldest first), each with an absolute byte offset
    and an event type ("connected" or "disconnected"). The offset corresponds to
    the buffer position at the time of the event — use it with read_serial to
    split data across reconnection boundaries.

    Only events within the current buffer range are retained.
    """
    port = _get_port(port_id)
    return [
        {"offset": offset, "event": event}
        for offset, event in port.events
        if offset >= since
    ]


@mcp.tool()
def set_baudrate(baudrate: int, port_id: str = "default") -> str:
    """Change the baud rate of the serial port at runtime."""
    port = _get_port(port_id)
    port.baudrate = baudrate
    if port.serial_port is not None and port.connected:
        port.serial_port.baudrate = baudrate
    return "ok"


@mcp.tool()
def list_ports() -> list[dict]:
    """List all configured serial ports and their status."""
    assert _app_state is not None, "AppState not initialized"
    return [
        {
            "port_id": pid,
            "url": p.url,
            "connected": p.connected,
            "baudrate": p.baudrate,
        }
        for pid, p in _app_state.ports.items()
    ]
