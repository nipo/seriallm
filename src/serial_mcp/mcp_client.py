"""MCP stdio client that proxies tool calls to the serial-mcp server via WebSocket."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import anyio
import websockets.asyncio.client
import websockets.exceptions
from mcp.server import FastMCP

if TYPE_CHECKING:
    from serial_mcp.config import Config

mcp = FastMCP("serial-mcp")


class McpProxy:
    """Sends JSON-RPC requests over WebSocket and awaits responses."""

    def __init__(self, ws: websockets.asyncio.client.ClientConnection) -> None:
        self._ws = ws
        self._next_id = 0
        self._pending: dict[int, anyio.Event] = {}
        self._results: dict[int, Any] = {}

    async def call(self, method: str, **params: Any) -> Any:
        self._next_id += 1
        req_id = self._next_id
        self._pending[req_id] = anyio.Event()

        await self._ws.send(
            json.dumps({"id": req_id, "method": method, "params": params})
        )

        await self._pending[req_id].wait()
        del self._pending[req_id]

        result = self._results.pop(req_id)
        if isinstance(result, Exception):
            raise result
        return result

    async def receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    continue
                msg = json.loads(raw)
                req_id = msg.get("id")
                if req_id is None or req_id not in self._pending:
                    continue
                if "error" in msg:
                    self._results[req_id] = RuntimeError(
                        msg["error"].get("message", "Unknown error")
                    )
                else:
                    self._results[req_id] = msg.get("result")
                self._pending[req_id].set()
        except (websockets.exceptions.ConnectionClosed, OSError):
            # Signal all pending calls
            for req_id, event in self._pending.items():
                self._results[req_id] = RuntimeError("Server connection lost")
                event.set()


_proxy: McpProxy | None = None


# --- Tool definitions (proxy to server) ---


@mcp.tool()
async def read_serial(
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
    assert _proxy is not None
    return await _proxy.call("read_serial", since=since, up_to=up_to, port_id=port_id)


@mcp.tool()
async def send(data: str, port_id: str = "default") -> str:
    """Send a UTF-8 string to the serial port.

    The sent data will be echoed back by read_serial only if the device echoes it.
    """
    assert _proxy is not None
    return await _proxy.call("send", data=data, port_id=port_id)


@mcp.tool()
async def send_bytes(hex_data: str, port_id: str = "default") -> str:
    """Send raw bytes to the serial port.

    `hex_data` is a hex-encoded string, e.g. "0d0a" sends CR LF.
    """
    assert _proxy is not None
    return await _proxy.call("send_bytes", hex_data=hex_data, port_id=port_id)


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
    assert _proxy is not None
    return await _proxy.call(
        "wait_for", pattern=pattern, since=since, timeout=timeout, port_id=port_id
    )


@mcp.tool()
async def set_control_lines(
    dtr: bool | None = None,
    rts: bool | None = None,
    port_id: str = "default",
) -> str:
    """Set DTR and/or RTS control lines on the serial port."""
    assert _proxy is not None
    return await _proxy.call(
        "set_control_lines", dtr=dtr, rts=rts, port_id=port_id
    )


@mcp.tool()
async def send_break(
    duration: float = 0.25,
    port_id: str = "default",
) -> str:
    """Send a break signal on the serial port."""
    assert _proxy is not None
    return await _proxy.call("send_break", duration=duration, port_id=port_id)


@mcp.tool()
async def get_port_info(port_id: str = "default") -> dict:
    """Get serial port status: baud rate, control lines, buffer offsets, connection state.

    The buffer_end value is the current absolute byte offset (total bytes
    received since server start). Use it as `since` in read_serial to start
    reading from "now" (ignoring past data).
    """
    assert _proxy is not None
    return await _proxy.call("get_port_info", port_id=port_id)


@mcp.tool()
async def get_port_events(
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
    assert _proxy is not None
    return await _proxy.call("get_port_events", since=since, port_id=port_id)


@mcp.tool()
async def set_baudrate(baudrate: int, port_id: str = "default") -> str:
    """Change the baud rate of the serial port at runtime."""
    assert _proxy is not None
    return await _proxy.call("set_baudrate", baudrate=baudrate, port_id=port_id)


@mcp.tool()
async def list_ports() -> list[dict]:
    """List all configured serial ports and their status."""
    assert _proxy is not None
    return await _proxy.call("list_ports")


# --- Entry point ---


async def run_mcp_stdio(config: Config) -> None:
    from serial_mcp.spawn import connect_or_spawn

    global _proxy

    ws = await connect_or_spawn(config, path="/ws/mcp")

    _proxy = McpProxy(ws)

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_proxy.receive_loop)
            await mcp.run_stdio_async()
            tg.cancel_scope.cancel()
    finally:
        _proxy = None
        try:
            await ws.close()
        except Exception:
            pass
