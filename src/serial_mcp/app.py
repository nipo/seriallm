from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute

from serial_mcp.ws_handler import ws_serial_handler
from serial_mcp.ws_mcp_handler import ws_mcp_handler

if TYPE_CHECKING:
    from serial_mcp.state import AppState


def create_app(app_state: AppState) -> Starlette:
    app = Starlette(
        routes=[
            WebSocketRoute("/ws", ws_serial_handler),
            WebSocketRoute("/ws/mcp", ws_mcp_handler),
        ],
    )
    app.state.app_state = app_state
    return app
