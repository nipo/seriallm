from __future__ import annotations

import json

from starlette.websockets import WebSocket, WebSocketDisconnect

from serial_mcp.state import AppState
from serial_mcp.tool_executor import ToolExecutor


async def ws_mcp_handler(websocket: WebSocket) -> None:
    """WebSocket handler for MCP tool clients.

    Accepts JSON-RPC text frames:
        {"id": N, "method": "read_serial", "params": {"since": 0}}

    Responds with:
        {"id": N, "result": {...}}
    or:
        {"id": N, "error": {"message": "..."}}
    """
    app_state: AppState = websocket.app.state.app_state

    await websocket.accept()
    app_state.client_connected()

    executor = ToolExecutor(app_state)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(
                    json.dumps({"id": None, "error": {"message": "Invalid JSON"}})
                )
                continue

            req_id = msg.get("id")
            method = msg.get("method")
            params = msg.get("params", {})

            if not method:
                await websocket.send_text(
                    json.dumps({"id": req_id, "error": {"message": "Missing method"}})
                )
                continue

            try:
                result = await executor.dispatch(method, params)
                await websocket.send_text(
                    json.dumps({"id": req_id, "result": result})
                )
            except TimeoutError:
                await websocket.send_text(
                    json.dumps({"id": req_id, "error": {"message": "Timeout"}})
                )
            except Exception as e:
                await websocket.send_text(
                    json.dumps({"id": req_id, "error": {"message": str(e)}})
                )
    except WebSocketDisconnect:
        pass
    finally:
        app_state.client_disconnected()
