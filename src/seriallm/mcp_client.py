"""MCP stdio client that proxies tool calls to the seriallm server via WebSocket."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import anyio
import websockets.asyncio.client
import websockets.exceptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

if TYPE_CHECKING:
    from seriallm.config import Config

mcp = MCPServer("seriallm")

# Tools taking a `since`/`up_to` range get their description built from this
# hint, so the range model is documented in exactly one place. It has to be
# passed to @mcp.tool(description=...): an f-string is not a docstring.
RANGE_HINT = """\
`since` and `up_to` select a byte range in the port's ring buffer. Each
accepts an absolute offset, a negative offset relative to the buffer end, or
an offset expression object resolved server-side — {"method":
"last_reconnect"}, "first_match", "latest_match", "wait_for_match". Together
they express things like "from the last reboot until the next DONE" in a
single call, with no offset bookkeeping and no polling.

Read the `seriallm-offsets` skill for the expression reference before
composing anything beyond a plain integer."""


class McpProxy:
    """Sends JSON-RPC requests over WebSocket and awaits responses.

    Failures are reported as ToolError: any other exception type has its
    message withheld from the model, and the server-side text is what tells
    the caller what went wrong.
    """

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
                    self._results[req_id] = ToolError(
                        msg["error"].get("message", "Unknown error")
                    )
                else:
                    self._results[req_id] = msg.get("result")
                self._pending[req_id].set()
        except (websockets.exceptions.ConnectionClosed, OSError):
            for req_id, event in self._pending.items():
                self._results[req_id] = ToolError("Server connection lost")
                event.set()


_proxy: McpProxy | None = None


# --- Tool definitions (proxy to server) ---


@mcp.tool(
    description=f"""Read data received from the serial port.

Returns {{data, start, end, start_time, end_time}} where `start` and `end`
are the byte offsets the returned data actually spans. Pass `end` back as the
next call's `since` to keep reading without gaps or overlap. `start_time` and
`end_time` are the receive times of the first and last returned bytes, in
float epoch seconds, null when no data is returned.

{RANGE_HINT}"""
)
async def read_serial(
    since: int | dict | None = 0,
    up_to: int | dict | None = None,
    port_id: str = "default",
) -> dict[str, Any]:
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
async def get_port_info(port_id: str = "default") -> dict[str, Any]:
    """Get serial port status: baud rate, control lines, buffer offsets, connection state.

    The reported buffer_start and buffer_end tell how much history is still
    available. They are not meant for offset tracking: name the boundaries of
    a range symbolically instead (see the `seriallm-offsets` skill).
    """
    assert _proxy is not None
    return await _proxy.call("get_port_info", port_id=port_id)


@mcp.tool(
    description=f"""Get connection/disconnection events for a serial port.

Returns a list of {{offset, time, event}} objects, for inspecting the
reconnection history itself. `time` is in float epoch seconds. To merely scope a query to the current device session, use
{{"method": "last_reconnect"}} as another tool's `since` rather than reading
an offset from here.

{RANGE_HINT}"""
)
async def get_port_events(
    since: int | dict | None = 0,
    port_id: str = "default",
) -> list[dict[str, Any]]:
    assert _proxy is not None
    return await _proxy.call("get_port_events", since=since, port_id=port_id)


@mcp.tool()
async def set_baudrate(baudrate: int, port_id: str = "default") -> str:
    """Change the baud rate of the serial port at runtime."""
    assert _proxy is not None
    return await _proxy.call("set_baudrate", baudrate=baudrate, port_id=port_id)


@mcp.tool(
    description=f"""Dump a range of the serial port buffer to a local file.

Writes raw bytes straight to disk without shipping them through MCP. Use this
to extract log segments for offline analysis with external tools.

Returns {{path, start, end, bytes_written, start_time, end_time}}, with times
as in read_serial.

{RANGE_HINT}"""
)
async def dump_to_file(
    path: str,
    since: int | dict | None = 0,
    up_to: int | dict | None = None,
    port_id: str = "default",
) -> dict[str, Any]:
    assert _proxy is not None
    return await _proxy.call(
        "dump_to_file", path=path, since=since, up_to=up_to, port_id=port_id
    )


@mcp.tool(
    description=f"""Search for a regex pattern in the serial port buffer, line by line.

Matching runs server-side, so the buffer is not transferred. Returns a list of
{{line, offset, time, line_number}} for each matching line, where `offset` is
the absolute byte offset of the line start and `time` the receive time of its
first byte, in float epoch seconds. `context` includes surrounding lines,
like grep -C.

{RANGE_HINT}"""
)
async def grep(
    pattern: str,
    since: int | dict | None = 0,
    up_to: int | dict | None = None,
    context: int = 0,
    port_id: str = "default",
) -> list[dict[str, Any]]:
    assert _proxy is not None
    return await _proxy.call(
        "grep", pattern=pattern, since=since, up_to=up_to, context=context, port_id=port_id
    )


@mcp.tool(
    description=f"""Resolve an offset expression to an absolute offset and its receive time.

Returns {{offset, time}}, where `time` is the receive time of the byte at
`offset` in float epoch seconds, or null when that byte is not in the buffer
(e.g. `offset` is the buffer end). Use it to timestamp an event without
reading data, e.g. offset={{"method": "first_match", "pattern": "boot done",
"since": X}}.

{RANGE_HINT}"""
)
async def resolve_offset(
    offset: int | dict,
    port_id: str = "default",
) -> dict[str, Any]:
    assert _proxy is not None
    return await _proxy.call("resolve_offset", offset=offset, port_id=port_id)


@mcp.tool()
async def list_ports() -> list[dict[str, Any]]:
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
