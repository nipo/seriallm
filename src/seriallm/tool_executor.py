from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

import anyio

from seriallm.offset import OffsetExpr, resolve_offset
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
        "set_control_lines": "set_control_lines",
        "send_break": "send_break",
        "get_port_info": "get_port_info",
        "get_port_events": "get_port_events",
        "set_baudrate": "set_baudrate",
        "list_ports": "list_ports",
        "dump_to_file": "dump_to_file",
        "grep": "grep",
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

    async def read_serial(
        self,
        since: OffsetExpr = 0,
        up_to: OffsetExpr = None,
        port_id: str = "default",
    ) -> dict:
        port = self._get_port(port_id)
        since_val = await resolve_offset(since, port, default=0)
        assert since_val is not None
        up_to_val = await resolve_offset(
            up_to, port, default=None, default_since=since_val
        )
        data, start, end = port.buffer.read(since_val, up_to_val)
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

    async def get_port_events(
        self, since: OffsetExpr = 0, port_id: str = "default"
    ) -> list[dict]:
        port = self._get_port(port_id)
        since_val = await resolve_offset(since, port, default=0)
        assert since_val is not None
        return [
            {"offset": offset, "event": event}
            for offset, event in port.events
            if offset >= since_val
        ]

    def set_baudrate(self, baudrate: int, port_id: str = "default") -> str:
        port = self._get_port(port_id)
        port.baudrate = baudrate
        if port.serial_port is not None and port.connected:
            port.serial_port.baudrate = baudrate
        return "ok"

    async def dump_to_file(
        self,
        path: str,
        since: OffsetExpr = 0,
        up_to: OffsetExpr = None,
        port_id: str = "default",
    ) -> dict:
        port = self._get_port(port_id)
        since_val = await resolve_offset(since, port, default=0)
        assert since_val is not None
        up_to_val = await resolve_offset(
            up_to, port, default=None, default_since=since_val
        )
        data, start, end = port.buffer.read(since_val, up_to_val)
        out = Path(path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return {"path": str(out), "start": start, "end": end, "bytes_written": len(data)}

    async def grep(
        self,
        pattern: str,
        since: OffsetExpr = 0,
        up_to: OffsetExpr = None,
        context: int = 0,
        port_id: str = "default",
    ) -> list[dict]:
        port = self._get_port(port_id)
        since_val = await resolve_offset(since, port, default=0)
        assert since_val is not None
        up_to_val = await resolve_offset(
            up_to, port, default=None, default_since=since_val
        )
        data, start, end = port.buffer.read(since_val, up_to_val)
        if not data:
            return []

        text = data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        regex = re.compile(pattern)

        # Build line offset table (byte offset of each line start relative to `start`)
        line_offsets: list[int] = []
        offset = 0
        for line in lines:
            line_offsets.append(offset)
            offset += len(line.encode("utf-8", errors="replace")) + 1  # +1 for \n

        matched_lines: set[int] = set()
        for i, line in enumerate(lines):
            if regex.search(line):
                for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                    matched_lines.add(j)

        return [
            {
                "line": lines[i],
                "offset": start + line_offsets[i],
                "line_number": i,
            }
            for i in sorted(matched_lines)
        ]

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
