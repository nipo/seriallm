from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import serial

if TYPE_CHECKING:
    from serial_mcp.state import AppState, PortState
    from serial_mcp.terminal import OutputFilter


async def serial_send(port: PortState, data: bytes) -> None:
    if not port.connected or port.serial_port is None:
        raise RuntimeError("Port not connected")
    ser = port.serial_port
    async with port.lock:
        await anyio.to_thread.run_sync(lambda: ser.write(data))


async def serial_reader_task(
    port: PortState,
    app: AppState,
    output_filter: OutputFilter,
) -> None:
    from serial_mcp.terminal import write_output, write_status

    while not app.shutdown_event.is_set():
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
        write_status(f"\r\n[Connected: {port.url} @ {port.baudrate}]\r\n")

        # Read loop
        try:
            while not app.shutdown_event.is_set():
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
                    write_output(data, output_filter)
        except (serial.SerialException, OSError) as e:
            write_status(f"\r\n[Disconnected: {e}, reconnecting...]\r\n")
        finally:
            port.connected = False
            port.serial_port = None
            try:
                ser.close()
            except Exception:
                pass

        await anyio.sleep(1.0)
