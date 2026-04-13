from __future__ import annotations

import dataclasses

import anyio
import anyio.abc
import serial


class RingBuffer:
    def __init__(self, max_size: int = 1_000_000) -> None:
        self._buf = bytearray()
        self._start_offset: int = 0
        self._max_size = max_size

    @property
    def start_offset(self) -> int:
        return self._start_offset

    @property
    def end_offset(self) -> int:
        return self._start_offset + len(self._buf)

    def append(self, data: bytes) -> None:
        self._buf.extend(data)
        if len(self._buf) > self._max_size:
            trim = len(self._buf) - self._max_size
            del self._buf[:trim]
            self._start_offset += trim

    def read(
        self, since: int = 0, up_to: int | None = None
    ) -> tuple[bytes, int, int]:
        actual_start = max(since, self._start_offset)
        actual_end = self.end_offset if up_to is None else min(up_to, self.end_offset)
        if actual_start >= actual_end:
            return b"", actual_end, actual_end
        buf_start = actual_start - self._start_offset
        buf_end = actual_end - self._start_offset
        return bytes(self._buf[buf_start:buf_end]), actual_start, actual_end


@dataclasses.dataclass
class PortState:
    url: str
    baudrate: int
    buffer: RingBuffer
    lock: anyio.Lock
    condition: anyio.Condition
    serial_port: serial.Serial | None = None
    connected: bool = False
    events: list[tuple[int, str]] = dataclasses.field(default_factory=list)

    def record_event(self, event: str) -> None:
        self.events.append((self.buffer.end_offset, event))
        # Trim events that fell out of the buffer
        start = self.buffer.start_offset
        while self.events and self.events[0][0] < start:
            self.events.pop(0)


@dataclasses.dataclass
class AppState:
    ports: dict[str, PortState]
    shutdown_event: anyio.Event
    buffer_size: int = 1_000_000
    grace_period: float = 5.0
    _client_count: int = dataclasses.field(default=0, repr=False)
    _grace_scope: anyio.CancelScope | None = dataclasses.field(default=None, repr=False)
    _task_group: anyio.abc.TaskGroup | None = dataclasses.field(default=None, repr=False)

    def set_task_group(self, tg: anyio.abc.TaskGroup) -> None:
        self._task_group = tg

    def client_connected(self) -> None:
        self._client_count += 1
        if self._grace_scope is not None:
            self._grace_scope.cancel()
            self._grace_scope = None

    def client_disconnected(self) -> None:
        self._client_count -= 1
        if self._client_count <= 0 and self._task_group is not None and self.grace_period >= 0:
            self._task_group.start_soon(self._grace_timer)

    async def _grace_timer(self) -> None:
        self._grace_scope = anyio.CancelScope()
        with self._grace_scope:
            await anyio.sleep(self.grace_period)
            self.shutdown_event.set()
        self._grace_scope = None
