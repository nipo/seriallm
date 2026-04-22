"""Offset expression resolver.

Offset parameters in tools accept:
- An integer: absolute byte offset (negative counts from buffer end).
- A dict: an expression resolved server-side.

Expression types:
  {"method": "last_reconnect"}
      Offset of the last "connected" event. Returns 0 if no events.

  {"method": "last_disconnect"}
      Offset of the last "disconnected" event. Returns 0 if no events.

  {"method": "first_match", "pattern": "...", "edge": "start"|"end", "after": <expr>}
      First regex match in the buffer, searching forward from `after`.
      Error if not found.

  {"method": "latest_match", "pattern": "...", "edge": "start"|"end", "after": <expr>}
      Last regex match in the buffer, searching from `after` to end.
      Error if not found.

  {"method": "wait_for_match", "pattern": "...", "edge": "start"|"end",
   "timeout": float, "after": <expr>}
      Like first_match but blocks until found or timeout.

All expressions accept an optional "after" field (itself an offset expression)
that constrains the search to data after the resolved offset.
"""

from __future__ import annotations

import re
from typing import Any

import anyio

from seriallm.state import PortState

OffsetExpr = int | dict[str, Any] | None


async def resolve_offset(
    expr: OffsetExpr, port: PortState, default: int | None = None
) -> int | None:
    """Resolve an offset expression to an absolute byte offset.

    Returns None if expr is None and default is None.
    """
    if expr is None:
        return default

    if isinstance(expr, int):
        if expr < 0:
            return max(port.buffer.start_offset, port.buffer.end_offset + expr)
        return expr

    if not isinstance(expr, dict):
        raise ValueError(f"Invalid offset expression: {expr!r}")

    method = expr.get("method")
    if method is None:
        raise ValueError("Offset expression missing 'method' field")

    match method:
        case "last_reconnect":
            return _last_event(port, "connected")
        case "last_disconnect":
            return _last_event(port, "disconnected")
        case "first_match":
            return await _first_match(expr, port)
        case "latest_match":
            return await _latest_match(expr, port)
        case "wait_for_match":
            return await _wait_for_match(expr, port)
        case _:
            raise ValueError(f"Unknown offset method: {method!r}")


def _last_event(port: PortState, event_type: str) -> int:
    for offset, event in reversed(port.events):
        if event == event_type:
            return offset
    return 0


def _match_offset(m: re.Match, text: str, buf_start: int, edge: str) -> int:
    """Convert a regex match to an absolute byte offset."""
    if edge == "end":
        char_pos = m.end()
    else:
        char_pos = m.start()
    prefix_bytes = len(text[:char_pos].encode("utf-8", errors="replace"))
    return buf_start + prefix_bytes


async def _resolve_after(expr: dict, port: PortState) -> int:
    after_expr = expr.get("after")
    if after_expr is None:
        return port.buffer.start_offset
    result = await resolve_offset(after_expr, port, default=0)
    assert result is not None
    return result


async def _first_match(expr: dict, port: PortState) -> int:
    pattern = expr["pattern"]
    edge = expr.get("edge", "start")
    after = await _resolve_after(expr, port)

    regex = re.compile(pattern)
    data, start, end = port.buffer.read(after)
    if not data:
        raise ValueError(f"No data in buffer from offset {after}; pattern not found")

    text = data.decode("utf-8", errors="replace")
    m = regex.search(text)
    if m is None:
        raise ValueError(f"Pattern {pattern!r} not found in buffer range [{start}, {end})")

    return _match_offset(m, text, start, edge)


async def _latest_match(expr: dict, port: PortState) -> int:
    pattern = expr["pattern"]
    edge = expr.get("edge", "start")
    after = await _resolve_after(expr, port)

    regex = re.compile(pattern)
    data, start, end = port.buffer.read(after)
    if not data:
        raise ValueError(f"No data in buffer from offset {after}; pattern not found")

    text = data.decode("utf-8", errors="replace")
    last_match = None
    for m in regex.finditer(text):
        last_match = m

    if last_match is None:
        raise ValueError(f"Pattern {pattern!r} not found in buffer range [{start}, {end})")

    return _match_offset(last_match, text, start, edge)


async def _wait_for_match(expr: dict, port: PortState) -> int:
    pattern = expr["pattern"]
    edge = expr.get("edge", "start")
    timeout = float(expr.get("timeout", 10.0))
    after = await _resolve_after(expr, port)

    regex = re.compile(pattern)

    with anyio.fail_after(timeout):
        async with port.condition:
            while True:
                data, start, end = port.buffer.read(after)
                if data:
                    text = data.decode("utf-8", errors="replace")
                    m = regex.search(text)
                    if m:
                        return _match_offset(m, text, start, edge)
                await port.condition.wait()
