from __future__ import annotations

import argparse
import logging
import sys

import anyio
import uvicorn

from serial_mcp.app import create_app
from serial_mcp.mcp_server import set_app_state
from serial_mcp.serial_io import serial_reader_task
from serial_mcp.state import AppState, PortState, RingBuffer
from serial_mcp.terminal import (
    OutputFilter,
    raw_terminal,
    stdin_reader_task,
    write_output,
    write_status,
)


def _silence_logs() -> None:
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)
    logging.getLogger("mcp.server.streamable_http_manager").setLevel(logging.WARNING)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="serial-mcp",
        description="Serial terminal emulator with MCP server",
    )
    sub = p.add_subparsers(dest="command")

    # --- serve ---
    sp_serve = sub.add_parser("serve", help="Start MCP server (no serial ports until clients attach)")
    sp_serve.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    sp_serve.add_argument("--port", type=int, default=8808, help="HTTP port (default: 8808)")
    sp_serve.add_argument("--buffer-size", type=int, default=1_000_000, help="Max ring buffer per port (default: 1MB)")

    # --- attach ---
    sp_attach = sub.add_parser("attach", help="Attach to a running server as terminal client")
    sp_attach.add_argument("server_url", help="Server URL (e.g. http://localhost:8808)")
    sp_attach.add_argument("serial_url", help="Serial port or URL for the server to open")
    sp_attach.add_argument("baudrate", nargs="?", type=int, default=115200)
    sp_attach.add_argument("--name", default=None, help="Port name (default: serial URL)")
    sp_attach.add_argument("--raw", action="store_true", help="Raw terminal mode")

    # --- standalone (implicit, backward compatible) ---
    sp_standalone = sub.add_parser("standalone", help=argparse.SUPPRESS)
    sp_standalone.add_argument("port_url")
    sp_standalone.add_argument("baudrate", nargs="?", type=int, default=115200)
    sp_standalone.add_argument("--mcp-port", type=int, default=8808)
    sp_standalone.add_argument("--mcp-host", default="127.0.0.1")
    sp_standalone.add_argument("--raw", action="store_true")
    sp_standalone.add_argument("--buffer-size", type=int, default=1_000_000)

    return p


def parse_args() -> argparse.Namespace:
    # If first arg is not a known subcommand, insert "standalone"
    known = {"serve", "attach", "standalone", "-h", "--help"}
    if len(sys.argv) > 1 and sys.argv[1] not in known:
        sys.argv.insert(1, "standalone")
    return _build_parser().parse_args()


# --- Buffer follower for standalone mode ---

async def _buffer_follower(
    port: PortState,
    output_filter: OutputFilter,
    shutdown_event: anyio.Event,
) -> None:
    cursor = port.buffer.end_offset
    was_connected: bool | None = None  # sentinel: always report initial state

    while not shutdown_event.is_set():
        async with port.condition:
            while True:
                data, _, new_cursor = port.buffer.read(cursor)
                connected_changed = port.connected != was_connected
                if data or connected_changed:
                    break
                await port.condition.wait()

        if connected_changed:
            was_connected = port.connected
            if port.connected:
                write_status(f"\r\n[Connected: {port.url} @ {port.baudrate}]\r\n")
            else:
                write_status("\r\n[Disconnected, reconnecting...]\r\n")

        if data:
            cursor = new_cursor
            write_output(data, output_filter)


# --- Subcommand handlers ---

async def _async_serve(args: argparse.Namespace) -> None:
    _silence_logs()

    app_state = AppState(
        ports={},
        shutdown_event=anyio.Event(),
        buffer_size=args.buffer_size,
    )
    set_app_state(app_state)

    starlette_app = create_app(app_state)
    config = uvicorn.Config(starlette_app, host=args.host, port=args.port, log_level="critical")
    server = uvicorn.Server(config)

    async def _server_task() -> None:
        try:
            await server.serve()
        except SystemExit:
            write_status(f"[MCP server failed to start on {args.host}:{args.port}]\n")
            app_state.shutdown_event.set()

    write_status(f"serial-mcp server: http://{args.host}:{args.port}\n")
    write_status(f"MCP endpoint: http://{args.host}:{args.port}/mcp\n")
    write_status(f"WebSocket endpoint: ws://{args.host}:{args.port}/ws\n")

    async with anyio.create_task_group() as tg:
        tg.start_soon(_server_task)
        await app_state.shutdown_event.wait()
        server.should_exit = True
        tg.cancel_scope.cancel()


async def _async_attach(args: argparse.Namespace) -> None:
    from serial_mcp.client import run_client

    name = args.name if args.name else args.serial_url
    await run_client(args.server_url, args.serial_url, args.baudrate, name, args.raw)


async def _async_standalone(args: argparse.Namespace) -> None:
    _silence_logs()

    port_state = PortState(
        url=args.port_url,
        baudrate=args.baudrate,
        buffer=RingBuffer(args.buffer_size),
        lock=anyio.Lock(),
        condition=anyio.Condition(),
    )
    app_state = AppState(
        ports={"default": port_state},
        shutdown_event=anyio.Event(),
        buffer_size=args.buffer_size,
    )
    set_app_state(app_state)

    output_filter = OutputFilter(raw=args.raw)

    starlette_app = create_app(app_state)
    config = uvicorn.Config(
        starlette_app,
        host=args.mcp_host,
        port=args.mcp_port,
        log_level="critical",
    )
    server = uvicorn.Server(config)

    is_tty = sys.stdin.isatty()

    async def _server_task() -> None:
        try:
            await server.serve()
        except SystemExit:
            write_status(
                f"\r\n[MCP server failed to start on {args.mcp_host}:{args.mcp_port}]\r\n"
            )
            app_state.shutdown_event.set()

    async def _run_tasks() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(serial_reader_task, port_state, app_state.shutdown_event)
            tg.start_soon(_buffer_follower, port_state, output_filter, app_state.shutdown_event)
            if is_tty:
                tg.start_soon(stdin_reader_task, port_state, app_state)
            tg.start_soon(_server_task)
            await app_state.shutdown_event.wait()
            server.should_exit = True
            tg.cancel_scope.cancel()

    mcp_url = f"http://{args.mcp_host}:{args.mcp_port}/mcp"

    if is_tty:
        with raw_terminal():
            write_status(f"\r\nserial-mcp: {args.port_url} @ {args.baudrate}\r\n")
            write_status(f"MCP server: {mcp_url}\r\n")
            write_status("Quit: Ctrl+]\r\n")
            await _run_tasks()
        write_status("\n")
    else:
        write_status(f"serial-mcp (headless): {args.port_url} @ {args.baudrate}\n")
        write_status(f"MCP server: {mcp_url}\n")
        await _run_tasks()


def main_sync() -> None:
    args = parse_args()

    match args.command:
        case "serve":
            anyio.run(_async_serve, args, backend="asyncio")
        case "attach":
            anyio.run(_async_attach, args, backend="asyncio")
        case "standalone":
            anyio.run(_async_standalone, args, backend="asyncio")
        case _:
            _build_parser().print_help()
            sys.exit(1)
