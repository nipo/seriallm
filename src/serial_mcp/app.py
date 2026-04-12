from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.routing import WebSocketRoute

from serial_mcp.mcp_server import mcp
from serial_mcp.ws_handler import ws_serial_handler

if TYPE_CHECKING:
    from starlette.applications import Starlette

    from serial_mcp.state import AppState


def create_app(app_state: AppState) -> Starlette:
    app = mcp.streamable_http_app()
    # Add WebSocket route to the MCP app (must share the same Starlette
    # instance so the MCP lifespan that starts the session manager runs).
    app.routes.insert(0, WebSocketRoute("/ws", ws_serial_handler))
    app.state.app_state = app_state
    return app
