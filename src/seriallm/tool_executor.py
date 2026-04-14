from __future__ import annotations

import inspect
import re
from typing import Any

import anyio

from seriallm.serial_io import serial_send
from seriallm.state import AppState, PortState


class ToolExecutor:
    """Executes serial port tool operations against an AppState.

    Each public method corresponds to one MCP tool with identical semantics.
    The dispatch() method provides JSON-RPC-style routing by method name.
    """

    METHODS: dict[str, str] = {
        "read_serial": "read_serial",
        "send": "send",
        "send_bytes": "send_bytes",
        "wait_for": "wait_for",
        "set_control_lines": "set_control_lines",
        "send_break": "send_break",
        "get_port_info": "get_port_info",
        "get_port_events": "get_port_events",
        "set_baudrate": "set_baudrate",
        "list_ports": "list_ports",
    }

    def __init__(self, app_state: AppState) -> None:
        self._state = app_state

    def _get_port(self, port_id: str = "default") -> PortState:
        if port_id not in self._state.ports:
            raise ValueError(f"Unknown port: {port_id!r}")
        return self._state.ports[port_id]

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method not in self.METHODS:
            raise ValueError(f"Unknown method: {method!r}")
        func = getattr(self, self.METHODS[method])
        if inspect.iscoroutinefunction(func):
            return await func(**params)
        return func(**params)

    # --- Tool implementations ---

    def read_serial(
        self, since: int = 0, up_to: int | None = None, port_id: str = "default"
    ) -> dict:
        port = self._get_port(port_id)
        data, start, end = port.buffer.read(since, up_to)
        return {
            "data": data.decode("utf-8", errors="replace"),
            "start": start,
            "end": end,
        }

    async def send(self, data: str, port_id: str = "default") -> str:
        port = self._get_port(port_id)
        await serial_send(port, data.encode("utf-8"))
        return "ok"

    async def send_bytes(self, hex_data: str, port_id: str = "default") -> str:
        port = self._get_port(port_id)
        await serial_send(port, bytes.fromhex(hex_data))
        return "ok"

    async def wait_for(
        self,
        pattern: str,
        since: int = 0,
        timeout: float = 10.0,
        port_id: str = "default",
    ) -> dict:
        port = self._get_port(port_id)
        regex = re.compile(pattern)

        with anyio.fail_after(timeout):
            async with port.condition:
                while True:
                    data, start, end = port.buffer.read(since)
                    if data:
                        text = data.decode("utf-8", errors="replace")
                        m = regex.search(text)
                        if m:
                            prefix_bytes = len(
                                text[: m.start()].encode("utf-8", errors="replace")
                            )
                            match_bytes = len(
                                m.group(0).encode("utf-8", errors="replace")
                            )
                            return {
                                "offset": start + prefix_bytes,
                                "end": start + prefix_bytes + match_bytes,
                                "match": m.group(0),
                            }
                    await port.condition.wait()

    def set_control_lines(
        self,
        dtr: bool | None = None,
        rts: bool | None = None,
        port_id: str = "default",
    ) -> str:
        port = self._get_port(port_id)
        if port.serial_port is None or not port.connected:
            raise RuntimeError("Port not connected")
        if dtr is not None:
            port.serial_port.dtr = dtr
        if rts is not None:
            port.serial_port.rts = rts
        return "ok"

    async def send_break(
        self, duration: float = 0.25, port_id: str = "default"
    ) -> str:
        port = self._get_port(port_id)
        if port.serial_port is None or not port.connected:
            raise RuntimeError("Port not connected")
        ser = port.serial_port
        await anyio.to_thread.run_sync(lambda: ser.send_break(duration))
        return "ok"

    def get_port_info(self, port_id: str = "default") -> dict:
        port = self._get_port(port_id)
        info: dict = {
            "url": port.url,
            "baudrate": port.baudrate,
            "connected": port.connected,
            "buffer_start": port.buffer.start_offset,
            "buffer_end": port.buffer.end_offset,
        }
        if port.serial_port is not None and port.connected:
            try:
                info.update(
                    {
                        "cts": port.serial_port.cts,
                        "dsr": port.serial_port.dsr,
                        "ri": port.serial_port.ri,
                        "cd": port.serial_port.cd,
                        "dtr": port.serial_port.dtr,
                        "rts": port.serial_port.rts,
                    }
                )
            except Exception:
                pass
        return info

    def get_port_events(
        self, since: int = 0, port_id: str = "default"
    ) -> list[dict]:
        port = self._get_port(port_id)
        return [
            {"offset": offset, "event": event}
            for offset, event in port.events
            if offset >= since
        ]

    def set_baudrate(self, baudrate: int, port_id: str = "default") -> str:
        port = self._get_port(port_id)
        port.baudrate = baudrate
        if port.serial_port is not None and port.connected:
            port.serial_port.baudrate = baudrate
        return "ok"

    def list_ports(self) -> list[dict]:
        return [
            {
                "port_id": pid,
                "url": p.url,
                "connected": p.connected,
                "baudrate": p.baudrate,
            }
            for pid, p in self._state.ports.items()
        ]
