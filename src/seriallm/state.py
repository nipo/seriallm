from __future__ import annotations

import bisect
import dataclasses
import time

import anyio
import anyio.abc
import serial


class RingBuffer:
    """Byte buffer addressed by absolute monotonic offsets.

    Each appended batch is stamped with its receive time. A mark records the
    offset of the batch's first byte and its timestamp; all bytes up to the
    next mark share that timestamp.
    """

    def __init__(self, max_size: int = 1_000_000) -> None:
        self._buf = bytearray()
        self._start_offset: int = 0
        self._max_size = max_size
        # Marks before mark_head are stale. The mark at mark_head is the
        # last one at or before start_offset, it covers the first byte.
        self.mark_offsets: list[int] = []
        self.mark_times: list[float] = []
        self.mark_head: int = 0

    @property
    def start_offset(self) -> int:
        return self._start_offset

    @property
    def end_offset(self) -> int:
        return self._start_offset + len(self._buf)

    def append(self, data: bytes, timestamp: float) -> None:
        assert data
        self.mark_offsets.append(self.end_offset)
        self.mark_times.append(timestamp)
        self._buf.extend(data)
        if len(self._buf) > self._max_size:
            trim = len(self._buf) - self._max_size
            del self._buf[:trim]
            self._start_offset += trim
            self.__trim_marks()

    def __trim_marks(self) -> None:
        self.mark_head = self.__mark_index(self._start_offset)
        # Compact lazily so that trimming stays amortized O(1) per mark.
        if self.mark_head > len(self.mark_offsets) // 2:
            del self.mark_offsets[:self.mark_head]
            del self.mark_times[:self.mark_head]
            self.mark_head = 0

    def __mark_index(self, offset: int) -> int:
        return bisect.bisect_right(self.mark_offsets, offset, lo=self.mark_head) - 1

    def __clamp(self, since: int, up_to: int | None) -> tuple[int, int]:
        actual_start = max(since, self._start_offset)
        actual_end = self.end_offset if up_to is None else min(up_to, self.end_offset)
        return min(actual_start, actual_end), actual_end

    def read(
        self, since: int = 0, up_to: int | None = None
    ) -> tuple[bytes, int, int]:
        actual_start, actual_end = self.__clamp(since, up_to)
        buf_start = actual_start - self._start_offset
        buf_end = actual_end - self._start_offset
        return bytes(self._buf[buf_start:buf_end]), actual_start, actual_end

    def read_segments(
        self, since: int = 0, up_to: int | None = None
    ) -> list[tuple[int, float, bytes]]:
        """Read a range split at mark boundaries.

        Returns (offset, timestamp, data) tuples, where every byte of `data`
        was received at `timestamp`.
        """
        actual_start, actual_end = self.__clamp(since, up_to)
        segments: list[tuple[int, float, bytes]] = []
        offset = actual_start
        index = self.__mark_index(offset)
        while offset < actual_end:
            if index + 1 < len(self.mark_offsets):
                seg_end = min(self.mark_offsets[index + 1], actual_end)
            else:
                seg_end = actual_end
            data = self._buf[offset - self._start_offset:seg_end - self._start_offset]
            segments.append((offset, self.mark_times[index], bytes(data)))
            offset = seg_end
            index += 1
        return segments

    def time_at(self, offset: int) -> float | None:
        """Receive time of the byte at `offset`, None if not in the buffer."""
        if not self._start_offset <= offset < self.end_offset:
            return None
        return self.mark_times[self.__mark_index(offset)]


@dataclasses.dataclass
class PortState:
    url: str
    baudrate: int
    buffer: RingBuffer
    lock: anyio.Lock
    condition: anyio.Condition
    serial_port: serial.Serial | None = None
    connected: bool = False
    events: list[tuple[int, float, str]] = dataclasses.field(default_factory=list)

    def record_event(self, event: str) -> None:
        self.events.append((self.buffer.end_offset, time.time(), event))
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
