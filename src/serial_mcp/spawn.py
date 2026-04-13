from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import anyio
import websockets.asyncio.client
import websockets.exceptions

if TYPE_CHECKING:
    from serial_mcp.config import Config

RETRY_DELAYS = [0.1, 0.2, 0.5, 1.0, 2.0]


def _build_ws_url(config: Config, path: str) -> tuple[str, dict]:
    """Return (ws_url, connect_kwargs) for the configured server."""
    if config.server.is_uds:
        uds_path = str(config.server.uds_path.expanduser())
        return f"ws://localhost{path}", {"path": uds_path}
    return f"ws://{config.server.host}:{config.server.port}{path}", {}


async def _try_connect(
    config: Config, path: str
) -> websockets.asyncio.client.ClientConnection:
    ws_url, kwargs = _build_ws_url(config, path)
    if "path" in kwargs:
        return await websockets.asyncio.client.unix_connect(
            uri=ws_url, path=kwargs["path"]
        ).__aenter__()
    return await websockets.asyncio.client.connect(ws_url).__aenter__()


def _spawn_server(config: Config) -> None:
    """Spawn the server as a detached background process."""
    if config.server.is_uds:
        sock_path = config.server.uds_path.expanduser()
        if sock_path.exists():
            sock_path.unlink()

    devnull = open(os.devnull, "w")
    cmd = [sys.executable, "-m", "serial_mcp"]
    if config.config_path is not None:
        cmd.extend(["--config", str(config.config_path)])
    cmd.extend(["serve", "--background"])
    subprocess.Popen(
        cmd,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=devnull,
        stderr=devnull,
    )


async def connect_or_spawn(
    config: Config, path: str = "/ws"
) -> websockets.asyncio.client.ClientConnection:
    """Connect to the server, spawning it if necessary.

    Returns an open WebSocket connection. The caller is responsible for
    closing it (use as async context manager or call .close()).
    """
    # First attempt: try connecting directly
    try:
        return await _try_connect(config, path)
    except (OSError, websockets.exceptions.WebSocketException):
        pass

    # Server not running — spawn it
    _spawn_server(config)

    # Retry with backoff
    for delay in RETRY_DELAYS:
        await anyio.sleep(delay)
        try:
            return await _try_connect(config, path)
        except (OSError, websockets.exceptions.WebSocketException):
            pass

    raise RuntimeError(
        "Failed to connect to serial_mcp server after spawning. "
        "Check server logs or start it manually with 'serial_mcp serve'."
    )
