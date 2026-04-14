from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import anyio
import uvicorn

from seriallm.app import create_app
from seriallm.config import Config, load_config
from seriallm.state import AppState
from seriallm.terminal import write_status


def _silence_logs() -> None:
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)
    logging.getLogger("mcp.server.streamable_http_manager").setLevel(logging.WARNING)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="seriallm",
        description="Serial terminal emulator with MCP server",
    )
    p.add_argument("--config", type=Path, default=None, help="Config file path")
    sub = p.add_subparsers(dest="command")

    # --- serve ---
    sp_serve = sub.add_parser("serve", help="Start server (no serial ports until clients attach)")
    sp_serve.add_argument("--background", action="store_true", help="Run in background (suppress output)")
    sp_serve.add_argument("--buffer-size", type=int, default=1_000_000, help="Max ring buffer per port (default: 1MB)")

    # --- attach ---
    sp_attach = sub.add_parser("attach", help="Attach to server as terminal client")
    sp_attach.add_argument("target", help="Alias name or serial port URL")
    sp_attach.add_argument("baudrate", nargs="?", type=int, default=None)
    sp_attach.add_argument("--name", default=None, help="Port name (default: alias or URL)")
    sp_attach.add_argument("--raw", action="store_true", help="Raw terminal mode")
    sp_attach.add_argument("--server", default=None, help="Server URL (overrides config)")

    # --- mcp ---
    sub.add_parser("mcp", help="Run as MCP stdio server (for Claude Code integration)")

    return p


def parse_args() -> argparse.Namespace:
    subcommands = {"serve", "attach", "mcp"}
    # Find the first positional arg (skip flags and their values)
    first_pos = None
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in ("-h", "--help"):
            break
        if arg == "--config":
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        first_pos = i
        break
    # Bare invocation with a URL/alias maps to "attach"
    if first_pos is not None and sys.argv[first_pos] not in subcommands:
        sys.argv.insert(first_pos, "attach")
    return _build_parser().parse_args()


# --- Subcommand handlers ---

async def _async_serve(args: argparse.Namespace, config: Config) -> None:
    _silence_logs()

    app_state = AppState(
        ports={},
        shutdown_event=anyio.Event(),
        buffer_size=args.buffer_size,
        grace_period=config.server.grace_period,
    )

    starlette_app = create_app(app_state)

    if config.server.is_uds:
        uds_path = config.server.uds_path.expanduser()
        uds_path.parent.mkdir(parents=True, exist_ok=True)
        uv_config = uvicorn.Config(starlette_app, uds=str(uds_path), log_level="critical")
        listen_desc = f"unix:{uds_path}"
    else:
        uv_config = uvicorn.Config(
            starlette_app, host=config.server.host, port=config.server.port, log_level="critical"
        )
        listen_desc = f"http://{config.server.host}:{config.server.port}"

    server = uvicorn.Server(uv_config)

    async def _server_task() -> None:
        try:
            await server.serve()
        except SystemExit:
            if not args.background:
                write_status(f"[Server failed to start on {listen_desc}]\n")
            app_state.shutdown_event.set()

    if not args.background:
        write_status(f"seriallm server: {listen_desc}\n")

    try:
        async with anyio.create_task_group() as tg:
            app_state.set_task_group(tg)
            tg.start_soon(_server_task)
            await app_state.shutdown_event.wait()
            server.should_exit = True
            tg.cancel_scope.cancel()
    finally:
        if config.server.is_uds:
            uds_path = config.server.uds_path.expanduser()
            if uds_path.exists():
                uds_path.unlink()


async def _async_attach(args: argparse.Namespace, config: Config) -> None:
    from seriallm.client import run_client
    from seriallm.spawn import connect_or_spawn

    # Resolve target (alias or raw URL)
    serial_url, default_baudrate = config.resolve_target(args.target)
    baudrate = args.baudrate if args.baudrate is not None else default_baudrate
    name = args.name if args.name else (args.target if args.target in config.aliases else serial_url)

    # Determine server URL
    if args.server:
        server_url = args.server
    else:
        # Auto-spawn server if needed (test connectivity, close immediately)
        try:
            ws = await connect_or_spawn(config, path="/ws/mcp")
            await ws.close()
        except RuntimeError as e:
            write_status(f"{e}\n")
            sys.exit(1)

        if config.server.is_uds:
            server_url = f"uds:{config.server.uds_path.expanduser()}"
        else:
            server_url = f"http://{config.server.host}:{config.server.port}"

    await run_client(server_url, serial_url, baudrate, name, args.raw, config)


async def _async_mcp(config: Config) -> None:
    from seriallm.mcp_client import run_mcp_stdio

    await run_mcp_stdio(config)


def main_sync() -> None:
    args = parse_args()
    config = load_config(args.config)

    match args.command:
        case "serve":
            anyio.run(_async_serve, args, config, backend="asyncio")
        case "attach":
            anyio.run(_async_attach, args, config, backend="asyncio")
        case "mcp":
            anyio.run(_async_mcp, config, backend="asyncio")
        case _:
            _build_parser().print_help()
            sys.exit(1)
