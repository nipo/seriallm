from __future__ import annotations

import json
import sys
from urllib.parse import urlencode

import anyio
import websockets.asyncio.client
import websockets.exceptions

from serial_mcp.terminal import (
    QUIT_KEY,
    OutputFilter,
    raw_terminal,
    write_output,
    write_status,
)


async def _ws_reader(
    ws: websockets.asyncio.client.ClientConnection,
    output_filter: OutputFilter,
    shutdown_event: anyio.Event,
) -> None:
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
                if msg_type == "connected":
                    write_status(
                        f"\r\n[Connected: {msg.get('url')} @ {msg.get('baudrate')}]\r\n"
                    )
                elif msg_type == "reconnecting":
                    write_status("\r\n[Disconnected, reconnecting...]\r\n")
    except websockets.exceptions.ConnectionClosed:
        pass
    shutdown_event.set()


async def _stdin_sender(
    ws: websockets.asyncio.client.ClientConnection,
    shutdown_event: anyio.Event,
) -> None:
    def _read_one() -> bytes:
        return sys.stdin.buffer.read(1)

    while not shutdown_event.is_set():
        data = await anyio.to_thread.run_sync(_read_one, abandon_on_cancel=True)
        if not data:
            shutdown_event.set()
            return
        if data[0] == QUIT_KEY:
            shutdown_event.set()
            return
        try:
            await ws.send(data)
        except websockets.exceptions.ConnectionClosed:
            shutdown_event.set()
            return


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

    try:
        async with websockets.asyncio.client.connect(ws_url) as ws:
            shutdown_event = anyio.Event()

            write_status(f"\r\nAttached to {server_url} as {name!r}\r\n")

            if is_tty:
                with raw_terminal():
                    write_status(f"Serial: {serial_url} @ {baudrate}\r\n")
                    write_status("Quit: Ctrl+]\r\n")
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(_ws_reader, ws, output_filter, shutdown_event)
                        tg.start_soon(_stdin_sender, ws, shutdown_event)
                        await shutdown_event.wait()
                        tg.cancel_scope.cancel()
                write_status("\n")
            else:
                write_status(f"Serial (headless): {serial_url} @ {baudrate}\n")
                async with anyio.create_task_group() as tg:
                    tg.start_soon(_ws_reader, ws, output_filter, shutdown_event)
                    await shutdown_event.wait()
                    tg.cancel_scope.cancel()
    except OSError as e:
        write_status(f"Connection failed: {e}\n")
        sys.exit(1)
    except websockets.exceptions.InvalidStatus as e:
        write_status(f"Server rejected connection: {e}\n")
        sys.exit(1)
