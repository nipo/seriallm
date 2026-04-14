from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import anyio
import anyio.abc
import websockets.asyncio.client
import websockets.exceptions

from seriallm.terminal import (
    QUIT_KEY,
    OutputFilter,
    raw_terminal,
    write_output,
    write_status,
)

if TYPE_CHECKING:
    from seriallm.config import Config


def _make_ws_connect(
    server_url: str, path: str, config: Config | None = None
) -> websockets.asyncio.client.connect:
    """Build a websockets connect object for the given server and path."""
    ws_kwargs = dict(ping_interval=5, ping_timeout=10, close_timeout=2)

    if server_url.startswith("uds:"):
        uds_path = server_url[4:]
        return websockets.asyncio.client.unix_connect(
            uri=f"ws://localhost{path}", path=uds_path, **ws_kwargs
        )

    # Normalize http(s) → ws(s)
    url = server_url.rstrip("/")
    if url.startswith("http://"):
        url = "ws://" + url[7:]
    elif url.startswith("https://"):
        url = "wss://" + url[8:]
    elif not url.startswith(("ws://", "wss://")):
        url = "ws://" + url

    return websockets.asyncio.client.connect(f"{url}{path}", **ws_kwargs)


async def _stdin_reader(
    stdin_send: anyio.abc.ObjectSendStream[bytes],
    quit_event: anyio.Event,
) -> None:
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
    connector: websockets.asyncio.client.connect,
    output_filter: OutputFilter,
    stdin_recv: anyio.abc.ObjectReceiveStream[bytes],
    quit_event: anyio.Event,
) -> None:
    """Run one WebSocket session. Returns on disconnect (for reconnect)."""
    try:
        async with connector as ws:
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
    config: Config | None = None,
) -> None:
    params = urlencode({"url": serial_url, "baudrate": baudrate, "name": name})
    path = f"/ws?{params}"

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

            connector = _make_ws_connect(server_url, path, config)
            await _ws_session(connector, output_filter, stdin_recv, quit_event)

        write_status("\r\n")

    if is_tty:
        with raw_terminal():
            write_status(f"\r\nAttaching as {name!r}\r\n")
            write_status(f"Serial: {serial_url} @ {baudrate}\r\n")
            write_status("Quit: Ctrl+]\r\n")
            async with anyio.create_task_group() as tg:
                tg.start_soon(_stdin_reader, stdin_send, quit_event)
                tg.start_soon(_connection_loop)
                await quit_event.wait()
                tg.cancel_scope.cancel()
        write_status("\n")
    else:
        write_status(f"Attaching as {name!r}\n")
        write_status(f"Serial (headless): {serial_url} @ {baudrate}\n")
        async with anyio.create_task_group() as tg:
            tg.start_soon(_connection_loop)
            await quit_event.wait()
            tg.cancel_scope.cancel()
