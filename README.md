# serial-mcp

Serial terminal emulator with an MCP (Model Context Protocol) server.
Combines `miniterm`/`tio`-like interactive terminal access with
programmatic control over Streamable HTTP, so an LLM agent can
interact with serial devices.

## Features

- Interactive terminal on stdin/stdout (raw mode, Ctrl+] to quit)
- MCP server on Streamable HTTP for programmatic access
- Supports local serial ports, RFC 2217, and TCP sockets (anything
  `pyserial` supports via `serial_for_url`)
- Auto-reconnect when the port disappears (USB serial going to
  bootloader, etc.)
- Ring buffer with absolute monotonic byte offsets — each MCP client
  tracks its own read position, no data is lost between calls (up to
  the buffer limit)

## Installation

```
pip install -e .
```

## Usage

```
serial-mcp /dev/ttyUSB0 115200
serial-mcp /dev/ttyACM0
serial-mcp rfc2217://192.168.1.10:2217
serial-mcp socket://192.168.1.10:12345
serial-mcp loop://                        # loopback, for testing
```

### Options

| Option | Default | Description |
|---|---|---|
| `--mcp-port PORT` | 8808 | HTTP port for the MCP server |
| `--mcp-host HOST` | 127.0.0.1 | Bind address for the MCP server |
| `--raw` | off | Raw terminal mode (no output filtering) |
| `--buffer-size N` | 1000000 | Max ring buffer size in bytes |

The baud rate argument is optional and defaults to 115200.

### Terminal

When stdin is a TTY, the tool enters raw terminal mode:
- Everything you type is sent to the serial port
- Everything received is printed to stdout
- Ctrl+] quits

In default mode, output is filtered: lone CR is mapped to CRLF, bare
LF is mapped to CRLF, and non-printable control codes (except tab and
ESC) are stripped. Use `--raw` to disable filtering.

When stdin is not a TTY (e.g. piped), the terminal input is skipped and
the tool runs in headless mode (serial reader + MCP server only).

## MCP Tools

All tools accept an optional `port_id` parameter (default: `"default"`)
for future multi-port support.

### `read_serial`

Read data from the ring buffer.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `since` | int | 0 | Absolute byte offset to read from |
| `up_to` | int \| null | null | Absolute byte offset to read up to (exclusive). Omit for "everything so far". |

Returns `{ data, start, end }` where `start` and `end` are the actual
byte offsets of the returned data. If `since` is before the buffer
start (oldest data was trimmed), `start > since`.

### `send`

Send a UTF-8 string to the serial port.

| Parameter | Type | Description |
|---|---|---|
| `data` | string | The string to send |

### `send_bytes`

Send raw bytes (hex-encoded) to the serial port.

| Parameter | Type | Description |
|---|---|---|
| `hex_data` | string | Hex string, e.g. `"0d0a"` for CR LF |

### `wait_for`

Wait for a regex pattern to appear in serial output.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `pattern` | string | | Regex pattern to search for |
| `since` | int | 0 | Search from this absolute byte offset |
| `timeout` | float | 10.0 | Timeout in seconds |

Returns `{ offset, end, match }` — the byte offset range and matched
text. Use `read_serial(since=..., up_to=...)` to get surrounding
context.

### `set_control_lines`

Set DTR and/or RTS control lines.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `dtr` | bool \| null | null | Set DTR (null = don't change) |
| `rts` | bool \| null | null | Set RTS (null = don't change) |

### `send_break`

Send a serial break signal.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `duration` | float | 0.25 | Break duration in seconds |

### `get_port_info`

Returns baud rate, connection status, buffer offsets, and control line
states (CTS, DSR, RI, CD, DTR, RTS).

### `set_baudrate`

Change the baud rate at runtime.

| Parameter | Type | Description |
|---|---|---|
| `baudrate` | int | New baud rate |

### `list_ports`

List all configured serial ports with their connection status and baud
rate.

## Typical MCP Usage Pattern

The ring buffer uses absolute byte offsets that only increase. A client
tracks its read position to avoid re-reading data:

```
cursor = 0

# Send a command
send(data="version\r\n")

# Wait for the response
result = wait_for(pattern="v\\d+\\.\\d+", since=cursor, timeout=5.0)

# Read everything from cursor to just after the match
response = read_serial(since=cursor, up_to=result["end"])
cursor = response["end"]
```

For device reset flows (e.g. ESP32 bootloader entry):

```
set_control_lines(dtr=False, rts=True)   # assert RESET
set_control_lines(dtr=True, rts=False)   # assert BOOT, release RESET
set_control_lines(dtr=False, rts=False)  # release both
wait_for(pattern="waiting for download", timeout=5.0)
```

## Configuring with Claude Code

Add the MCP server to your project:

```bash
claude mcp add --transport http serial-mcp http://localhost:8808/mcp
```

Or add it to your `.mcp.json`:

```json
{
  "mcpServers": {
    "serial-mcp": {
      "type": "http",
      "url": "http://localhost:8808/mcp"
    }
  }
}
```

Then start `serial-mcp` before your Claude Code session:

```bash
# Terminal 1: start serial-mcp
serial-mcp /dev/ttyUSB0 115200

# Terminal 2: start Claude Code
claude
```

Claude can now use the serial port tools to interact with the device.
You also see everything live in terminal 1.

### Remote access

Since the MCP server uses Streamable HTTP, you can expose it over the
network by binding to `0.0.0.0`:

```bash
serial-mcp /dev/ttyUSB0 115200 --mcp-host 0.0.0.0 --mcp-port 8808
```

Then configure Claude Code on a remote machine:

```bash
claude mcp add --transport http serial-mcp http://serial-host:8808/mcp
```
