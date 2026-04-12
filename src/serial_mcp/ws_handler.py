from __future__ import annotations

import json

import anyio
from starlette.websockets import WebSocket, WebSocketDisconnect

from serial_mcp.serial_io import serial_reader_task, serial_send
from serial_mcp.state import AppState, PortState, RingBuffer


async def _buffer_follower(port: PortState, websocket: WebSocket) -> None:
    cursor = port.buffer.end_offset
    was_connected: bool | None = None  # sentinel: always report initial state

    while True:
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
                msg = {"type": "connected", "url": port.url, "baudrate": port.baudrate}
            else:
                msg = {"type": "reconnecting"}
            await websocket.send_text(json.dumps(msg))

        if data:
            cursor = new_cursor
            await websocket.send_bytes(data)


async def _ws_receive_loop(
    port: PortState, websocket: WebSocket, shutdown_event: anyio.Event
) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                shutdown_event.set()
                return
            if "bytes" in message and message["bytes"]:
                try:
                    await serial_send(port, message["bytes"])
                except RuntimeError:
                    pass  # port disconnected, drop input
    except WebSocketDisconnect:
        shutdown_event.set()


async def ws_serial_handler(websocket: WebSocket) -> None:
    app_state: AppState = websocket.app.state.app_state

    url = websocket.query_params.get("url")
    if not url:
        await websocket.close(code=4000, reason="Missing 'url' query parameter")
        return

    baudrate = int(websocket.query_params.get("baudrate", "115200"))
    name = websocket.query_params.get("name", url)

    if name in app_state.ports:
        await websocket.close(code=4001, reason=f"Port name {name!r} already in use")
        return

    await websocket.accept()

    port = PortState(
        url=url,
        baudrate=baudrate,
        buffer=RingBuffer(app_state.buffer_size),
        lock=anyio.Lock(),
        condition=anyio.Condition(),
    )
    app_state.ports[name] = port

    shutdown_event = anyio.Event()

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(serial_reader_task, port, shutdown_event)
            tg.start_soon(_buffer_follower, port, websocket)
            tg.start_soon(_ws_receive_loop, port, websocket, shutdown_event)
            await shutdown_event.wait()
            tg.cancel_scope.cancel()
    finally:
        app_state.ports.pop(name, None)
        if port.serial_port is not None:
            try:
                port.serial_port.close()
            except Exception:
                pass
        port.connected = False
        port.serial_port = None
