from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import serial

if TYPE_CHECKING:
    from seriallm.state import PortState


async def serial_send(port: PortState, data: bytes) -> None:
    if not port.connected or port.serial_port is None:
        raise RuntimeError("Port not connected")
    ser = port.serial_port
    async with port.lock:
        await anyio.to_thread.run_sync(lambda: ser.write(data))


async def serial_reader_task(
    port: PortState,
    shutdown_event: anyio.Event,
) -> None:
    while not shutdown_event.is_set():
        # Try to open
        try:
            ser = serial.serial_for_url(
                port.url, baudrate=port.baudrate, timeout=0.1
            )
        except (serial.SerialException, OSError):
            await anyio.sleep(1.0)
            continue

        port.serial_port = ser
        port.connected = True
        port.record_event("connected")
        async with port.condition:
            port.condition.notify_all()

        # Read loop
        try:
            while not shutdown_event.is_set():
                data = await anyio.to_thread.run_sync(
                    lambda: ser.read(ser.in_waiting or 1),
                    abandon_on_cancel=True,
                )
                if data:
                    data = data.replace(b"\x00", b"")
                if data:
                    async with port.condition:
                        port.buffer.append(data)
                        port.condition.notify_all()
        except (serial.SerialException, OSError):
            pass
        finally:
            port.connected = False
            port.serial_port = None
            port.record_event("disconnected")
            async with port.condition:
                port.condition.notify_all()
            try:
                ser.close()
            except Exception:
                pass

        await anyio.sleep(1.0)
