from __future__ import annotations

import argparse
import logging
import sys

import anyio
import uvicorn

from serial_mcp.mcp_server import mcp, set_app_state
from serial_mcp.serial_io import serial_reader_task
from serial_mcp.state import AppState, PortState, RingBuffer
from serial_mcp.terminal import (
    OutputFilter,
    raw_terminal,
    stdin_reader_task,
    write_status,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="serial-mcp",
        description="Serial terminal emulator with MCP server",
    )
    p.add_argument(
        "port_url",
        help="Serial port or URL (e.g. /dev/ttyUSB0, rfc2217://host:port, socket://host:port)",
    )
    p.add_argument(
        "baudrate",
        nargs="?",
        type=int,
        default=115200,
    )
    p.add_argument(
        "--mcp-port",
        type=int,
        default=8808,
        help="HTTP port for MCP server (default: 8808)",
    )
    p.add_argument(
        "--mcp-host",
        default="127.0.0.1",
        help="Bind address for MCP server (default: 127.0.0.1)",
    )
    p.add_argument(
        "--raw",
        action="store_true",
        help="Raw terminal mode (no output filtering)",
    )
    p.add_argument(
        "--buffer-size",
        type=int,
        default=1_000_000,
        help="Max ring buffer size in bytes (default: 1MB)",
    )
    return p.parse_args()


async def async_main(args: argparse.Namespace) -> None:
    # Silence MCP session manager and uvicorn internal logs
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)
    logging.getLogger("mcp.server.streamable_http_manager").setLevel(logging.WARNING)

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
        raw_mode=args.raw,
    )
    set_app_state(app_state)

    output_filter = OutputFilter(raw=args.raw)

    starlette_app = mcp.streamable_http_app()
    config = uvicorn.Config(
        starlette_app,
        host=args.mcp_host,
        port=args.mcp_port,
        log_level="critical",
    )
    server = uvicorn.Server(config)

    is_tty = sys.stdin.isatty()

    async def _mcp_server_task() -> None:
        try:
            await server.serve()
        except SystemExit:
            # Uvicorn calls sys.exit(1) on bind failure; it already logged the reason.
            write_status(
                f"\r\n[MCP server failed to start on {args.mcp_host}:{args.mcp_port}]\r\n"
            )
            app_state.shutdown_event.set()

    async def _run_tasks() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(serial_reader_task, port_state, app_state, output_filter)
            if is_tty:
                tg.start_soon(stdin_reader_task, port_state, app_state)
            tg.start_soon(_mcp_server_task)
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
        # After raw_terminal restores settings
        write_status("\n")
    else:
        write_status(f"serial-mcp (headless): {args.port_url} @ {args.baudrate}\n")
        write_status(f"MCP server: {mcp_url}\n")
        await _run_tasks()


def main_sync() -> None:
    args = parse_args()
    anyio.run(async_main, args, backend="asyncio")
