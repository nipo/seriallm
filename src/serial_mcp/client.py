from __future__ import annotations

import json
import sys
from urllib.parse import urlencode

import anyio
import anyio.abc
import websockets.asyncio.client
import websockets.exceptions

from serial_mcp.terminal import (
    QUIT_KEY,
    OutputFilter,
    raw_terminal,
    write_output,
    write_status,
)


async def _stdin_reader(
    stdin_send: anyio.abc.ObjectSendStream[bytes],
    quit_event: anyio.Event,
) -> None:
    """Read stdin byte-by-byte. Ctrl+] sets quit_event. Bytes go to stdin_send."""

    def _read_one() -> bytes:
        return sys.stdin.buffer.read(1)

    try:
        while not quit_event.is_set():
            data = await anyio.to_thread.run_sync(_read_one, abandon_on_cancel=True)
            if not data:
                quit_event.set()
                return
            if data[0] == QUIT_KEY:
                quit_event.set()
                return
            await stdin_send.send(data)
    except anyio.ClosedResourceError:
        quit_event.set()


async def _ws_session(
    ws_url: str,
    output_filter: OutputFilter,
    stdin_recv: anyio.abc.ObjectReceiveStream[bytes],
    quit_event: anyio.Event,
) -> None:
    """Run one WebSocket session. Returns on disconnect (for reconnect)."""
    try:
        async with websockets.asyncio.client.connect(
            ws_url,
            ping_interval=5,
            ping_timeout=10,
            close_timeout=2,
        ) as ws:
            session_done = anyio.Event()

            async def _reader() -> None:
                try:
                    async for message in ws:
                        if isinstance(message, bytes):
                            write_output(message, output_filter)
                        else:
                            try:
                                msg = json.loads(message)
                            except json.JSONDecodeError:
                                continue
                            msg_type = msg.get("type")
                            if msg_type == "opened":
                                write_status(
                                    f"\r\n[Port opened: {msg.get('url')} @ {msg.get('baudrate')}]\r\n"
                                )
                            elif msg_type == "waiting":
                                write_status("\r\n[Port lost, waiting...]\r\n")
                except (websockets.exceptions.ConnectionClosed, OSError, anyio.ClosedResourceError):
                    pass
                session_done.set()

            async def _sender() -> None:
                try:
                    async for data in stdin_recv.clone():
                        await ws.send(data)
                except (
                    websockets.exceptions.ConnectionClosed,
                    OSError,
                    anyio.ClosedResourceError,
                    anyio.EndOfStream,
                ):
                    pass
                session_done.set()

            async with anyio.create_task_group() as tg:
                tg.start_soon(_reader)
                tg.start_soon(_sender)

                # Wait for either session end or quit
                while not session_done.is_set() and not quit_event.is_set():
                    with anyio.move_on_after(0.5):
                        await session_done.wait()
                tg.cancel_scope.cancel()

    except (
        OSError,
        websockets.exceptions.InvalidStatus,
        websockets.exceptions.InvalidURI,
        websockets.exceptions.WebSocketException,
    ):
        pass


async def run_client(
    server_url: str,
    serial_url: str,
    baudrate: int,
    name: str,
    raw: bool,
) -> None:
    params = urlencode({"url": serial_url, "baudrate": baudrate, "name": name})

    # Normalize: http(s) → ws(s), strip trailing slash
    ws_url = server_url.rstrip("/")
    if ws_url.startswith("http://"):
        ws_url = "ws://" + ws_url[7:]
    elif ws_url.startswith("https://"):
        ws_url = "wss://" + ws_url[8:]
    elif not ws_url.startswith(("ws://", "wss://")):
        ws_url = "ws://" + ws_url

    ws_url = f"{ws_url}/ws?{params}"

    output_filter = OutputFilter(raw=raw)
    is_tty = sys.stdin.isatty()
    quit_event = anyio.Event()

    stdin_send, stdin_recv = anyio.create_memory_object_stream[bytes](max_buffer_size=64)

    async def _connection_loop() -> None:
        first = True
        while not quit_event.is_set():
            if not first:
                write_status("\r\n[Disconnected, reconnecting...]\r\n")
                await anyio.sleep(2.0)
                if quit_event.is_set():
                    return
            first = False

            await _ws_session(ws_url, output_filter, stdin_recv, quit_event)

        write_status("\r\n")

    if is_tty:
        with raw_terminal():
            write_status(f"\r\nAttaching to {server_url} as {name!r}\r\n")
            write_status(f"Serial: {serial_url} @ {baudrate}\r\n")
            write_status("Quit: Ctrl+]\r\n")
            async with anyio.create_task_group() as tg:
                tg.start_soon(_stdin_reader, stdin_send, quit_event)
                tg.start_soon(_connection_loop)
                await quit_event.wait()
                tg.cancel_scope.cancel()
        write_status("\n")
    else:
        write_status(f"Attaching to {server_url} as {name!r}\n")
        write_status(f"Serial (headless): {serial_url} @ {baudrate}\n")
        async with anyio.create_task_group() as tg:
            tg.start_soon(_connection_loop)
            await quit_event.wait()
            tg.cancel_scope.cancel()
