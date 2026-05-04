"""MCP stdio client that proxies tool calls to the seriallm server via WebSocket."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import anyio
import websockets.asyncio.client
import websockets.exceptions
from mcp.server import FastMCP

if TYPE_CHECKING:
    from seriallm.config import Config

mcp = FastMCP("seriallm")

OFFSET_DOC = """\
Offset parameters (`since`, `up_to`) accept an integer or an expression object.

Integers:
  - Positive: absolute byte offset in the buffer.
  - Negative: relative to buffer end (-100 = 100 bytes before current end).

Expression objects (resolved server-side, avoiding round-trips):
  {"method": "last_reconnect"}
      Byte offset of the most recent port connection event. Returns 0 if no
      reconnect has happened. Use this to scope queries to the current device
      session (e.g. after a reboot).

  {"method": "last_disconnect"}
      Byte offset of the most recent port disconnection event.

  {"method": "first_match", "pattern": "<regex>", "edge": "start"|"end"}
      Byte offset of the first regex match in the buffer. Error if not found.
      `edge` selects the start or end of the matched text.

  {"method": "latest_match", "pattern": "<regex>", "edge": "start"|"end"}
      Byte offset of the last regex match in the buffer. Error if not found.

  {"method": "wait_for_match", "pattern": "<regex>", "edge": "start"|"end", "timeout": <seconds>}
      Blocks until the pattern appears in the buffer, then resolves to the
      match offset. Errors on timeout. Use this as `up_to` to wait for a
      delimiter before reading/grepping.

Constraining with `since`:
  Match expressions accept an optional `since` field (itself an offset
  expression) that limits the search to data after the resolved offset.

  Implicit defaulting: when an expression is used as the tool's `up_to`, its
  `since` defaults to the tool's resolved `since`. So in:
    grep(pattern="ERROR",
         since={"method": "last_reconnect"},
         up_to={"method": "wait_for_match", "pattern": "done", "timeout": 30})
  the `up_to` searches for "done" starting from the resolved `last_reconnect`
  offset, not from the buffer start. No need to repeat the constraint.

  Override the implicit default by setting `since` explicitly on the inner
  expression.

Validation: unknown fields and invalid `edge` values produce errors — typos
won't be silently ignored.

PREFER offset expressions over manual offset tracking. Instead of calling
get_port_events to find the reconnect offset, then calling read_serial with
that offset, use a single call with {"method": "last_reconnect"} as `since`."""


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
            for req_id, event in self._pending.items():
                self._results[req_id] = RuntimeError("Server connection lost")
                event.set()


_proxy: McpProxy | None = None


# --- Tool definitions (proxy to server) ---


@mcp.tool()
async def read_serial(
    since: int | dict | None = 0,
    up_to: int | dict | None = None,
    port_id: str = "default",
) -> dict:
    f"""Read data received from the serial port.

    Returns {{data, start, end}} where `start` and `end` are the actual byte
    offsets of the returned data. Use `end` as `since` on the next call to
    continue reading without gaps.

    Prefer offset expressions to minimize round-trips. For example, to read
    everything since the device last rebooted:
        read_serial(since={{"method": "last_reconnect"}})

    To read a command response (between the command echo and the next prompt):
        read_serial(
            since={{"method": "first_match", "pattern": "my_command", "edge": "end",
                    "since": -200}},
            up_to={{"method": "wait_for_match", "pattern": ">", "timeout": 5}})
    {OFFSET_DOC}"""
    assert _proxy is not None
    return await _proxy.call("read_serial", since=since, up_to=up_to, port_id=port_id)


@mcp.tool()
async def send(data: str, port_id: str = "default") -> str:
    """Send a UTF-8 string to the serial port.

    Common patterns:
    - Send a command: send(data="help\\r\\n")
    - Send Ctrl+C: send(data="\\x03")

    The sent data will be echoed back by read_serial only if the device echoes.
    """
    assert _proxy is not None
    return await _proxy.call("send", data=data, port_id=port_id)


@mcp.tool()
async def send_bytes(hex_data: str, port_id: str = "default") -> str:
    """Send raw bytes to the serial port.

    `hex_data` is a hex-encoded string, e.g. "0d0a" sends CR LF.
    Use this for binary protocols or non-UTF-8 data.
    """
    assert _proxy is not None
    return await _proxy.call("send_bytes", hex_data=hex_data, port_id=port_id)


@mcp.tool()
async def set_control_lines(
    dtr: bool | None = None,
    rts: bool | None = None,
    port_id: str = "default",
) -> str:
    """Set DTR and/or RTS control lines on the serial port.

    Common patterns:
    - Reset an ESP32: set DTR=false,RTS=true then DTR=false,RTS=false.
    - Enter bootloader: toggle DTR/RTS in the device-specific sequence.
    """
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

    Returns buffer_start and buffer_end which are the current buffer boundaries.
    You usually don't need these for offset tracking — prefer offset expressions
    like {{"method": "last_reconnect"}} instead of manually reading buffer_end.
    """
    assert _proxy is not None
    return await _proxy.call("get_port_info", port_id=port_id)


@mcp.tool()
async def get_port_events(
    since: int | dict | None = 0,
    port_id: str = "default",
) -> list[dict]:
    f"""Get connection/disconnection events for a serial port.

    Returns a list of {{offset, event}} objects. Useful for understanding the
    full reconnection history. For simple "since last reboot" queries, prefer
    using {{"method": "last_reconnect"}} as an offset expression directly in
    read_serial or grep — it avoids a round-trip.
    {OFFSET_DOC}"""
    assert _proxy is not None
    return await _proxy.call("get_port_events", since=since, port_id=port_id)


@mcp.tool()
async def set_baudrate(baudrate: int, port_id: str = "default") -> str:
    """Change the baud rate of the serial port at runtime."""
    assert _proxy is not None
    return await _proxy.call("set_baudrate", baudrate=baudrate, port_id=port_id)


@mcp.tool()
async def dump_to_file(
    path: str,
    since: int | dict | None = 0,
    up_to: int | dict | None = None,
    port_id: str = "default",
) -> dict:
    f"""Dump a range of the serial port buffer to a local file.

    Writes raw bytes directly to disk without going through MCP. Use this to
    extract log segments for offline analysis with external tools.

    Returns {{path, start, end, bytes_written}}.

    Example — dump everything since last reboot to a file:
        dump_to_file(path="/tmp/boot.log", since={{"method": "last_reconnect"}})
    {OFFSET_DOC}"""
    assert _proxy is not None
    return await _proxy.call(
        "dump_to_file", path=path, since=since, up_to=up_to, port_id=port_id
    )


@mcp.tool()
async def grep(
    pattern: str,
    since: int | dict | None = 0,
    up_to: int | dict | None = None,
    context: int = 0,
    port_id: str = "default",
) -> list[dict]:
    f"""Search for a regex pattern in the serial port buffer, line by line.

    Returns matching lines with their byte offsets. Use `context` to include
    surrounding lines (like grep -C).

    PREFER combining offset expressions to do complex queries in a single call.

    Example — find all errors between last reboot and test completion:
        grep(pattern="ERROR|FAIL",
             since={{"method": "last_reconnect"}},
             up_to={{"method": "wait_for_match", "pattern": "test complete",
                     "timeout": 30}})

    The `up_to` inherits its search start from the resolved `since`, so it
    waits for the next "test complete" after the reconnect — not stale ones
    from earlier sessions. Single call, no intermediate steps.

    Returns a list of {{line, offset, line_number}} for each matching/context line.
    {OFFSET_DOC}"""
    assert _proxy is not None
    return await _proxy.call(
        "grep", pattern=pattern, since=since, up_to=up_to, context=context, port_id=port_id
    )


@mcp.tool()
async def list_ports() -> list[dict]:
    """List all currently attached serial ports with their connection status and baud rate.

    Returns a list of {port_id, url, connected, baudrate}. Use port_id values
    as the port_id parameter in other tools.
    """
    assert _proxy is not None
    return await _proxy.call("list_ports")


# --- Entry point ---


async def run_mcp_stdio(config: Config) -> None:
    from seriallm.spawn import connect_or_spawn

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
