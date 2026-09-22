"""Offset expression resolver.

Offset parameters in tools accept an integer (absolute, or relative to the
buffer end when negative) or a dict describing an expression resolved
server-side. `_ALLOWED_FIELDS` below is the authoritative list of methods and
their fields; the agent-facing semantics live in the `seriallm-offsets` skill.
"""

from __future__ import annotations

import re
from typing import Any

import anyio

from seriallm.state import PortState

OffsetExpr = int | dict[str, Any] | None

_COMMON_FIELDS = {"method", "since"}
_EDGE_VALUES = {"start", "end"}

_ALLOWED_FIELDS: dict[str, set[str]] = {
    "last_reconnect": _COMMON_FIELDS,
    "last_disconnect": _COMMON_FIELDS,
    "first_match": _COMMON_FIELDS | {"pattern", "edge"},
    "latest_match": _COMMON_FIELDS | {"pattern", "edge"},
    "wait_for_match": _COMMON_FIELDS | {"pattern", "edge", "timeout"},
}


def _validate_expr(expr: dict, method: str) -> None:
    allowed = _ALLOWED_FIELDS.get(method)
    if allowed is None:
        raise ValueError(f"Unknown offset method: {method!r}")
    extra = set(expr.keys()) - allowed
    if extra:
        raise ValueError(
            f"Offset method {method!r} got unknown field(s): {sorted(extra)}; "
            f"allowed fields: {sorted(allowed)}"
        )
    edge = expr.get("edge", "start")
    if edge not in _EDGE_VALUES:
        raise ValueError(
            f"Invalid edge {edge!r}; must be 'start' or 'end'"
        )


async def resolve_offset(
    expr: OffsetExpr,
    port: PortState,
    default: int | None = None,
    default_since: int | None = None,
) -> int | None:
    """Resolve an offset expression to an absolute byte offset.

    Returns None if expr is None and default is None.

    `default_since` is used as the implicit `since` for the expression when it
    doesn't specify one. Used by tools to make `up_to` expressions search from
    the resolved `since` by default.
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

    _validate_expr(expr, method)

    match method:
        case "last_reconnect":
            return _last_event(port, "connected")
        case "last_disconnect":
            return _last_event(port, "disconnected")
        case "first_match":
            return await _first_match(expr, port, default_since)
        case "latest_match":
            return await _latest_match(expr, port, default_since)
        case "wait_for_match":
            return await _wait_for_match(expr, port, default_since)
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


async def _resolve_since(expr: dict, port: PortState, default_since: int | None) -> int:
    """Resolve the `since` field of an expression. Falls back to default_since,
    then to buffer start."""
    since_expr = expr.get("since")
    if since_expr is None:
        if default_since is not None:
            return default_since
        return port.buffer.start_offset
    result = await resolve_offset(since_expr, port, default=0)
    assert result is not None
    return result


async def _first_match(
    expr: dict, port: PortState, default_since: int | None
) -> int:
    pattern = expr["pattern"]
    edge = expr.get("edge", "start")
    since = await _resolve_since(expr, port, default_since)

    regex = re.compile(pattern)
    data, start, end = port.buffer.read(since)
    if not data:
        raise ValueError(f"No data in buffer from offset {since}; pattern not found")

    text = data.decode("utf-8", errors="replace")
    m = regex.search(text)
    if m is None:
        raise ValueError(f"Pattern {pattern!r} not found in buffer range [{start}, {end})")

    return _match_offset(m, text, start, edge)


async def _latest_match(
    expr: dict, port: PortState, default_since: int | None
) -> int:
    pattern = expr["pattern"]
    edge = expr.get("edge", "start")
    since = await _resolve_since(expr, port, default_since)

    regex = re.compile(pattern)
    data, start, end = port.buffer.read(since)
    if not data:
        raise ValueError(f"No data in buffer from offset {since}; pattern not found")

    text = data.decode("utf-8", errors="replace")
    last_match = None
    for m in regex.finditer(text):
        last_match = m

    if last_match is None:
        raise ValueError(f"Pattern {pattern!r} not found in buffer range [{start}, {end})")

    return _match_offset(last_match, text, start, edge)


async def _wait_for_match(
    expr: dict, port: PortState, default_since: int | None
) -> int:
    pattern = expr["pattern"]
    edge = expr.get("edge", "start")
    timeout = float(expr.get("timeout", 10.0))
    since = await _resolve_since(expr, port, default_since)

    regex = re.compile(pattern)

    with anyio.fail_after(timeout):
        async with port.condition:
            while True:
                data, start, end = port.buffer.read(since)
                if data:
                    text = data.decode("utf-8", errors="replace")
                    m = regex.search(text)
                    if m:
                        return _match_offset(m, text, start, edge)
                await port.condition.wait()
