# serial_mcp

Serial terminal emulator with MCP (Model Context Protocol) integration.
Combines `miniterm`/`tio`-like interactive terminal access with
programmatic control via MCP, so an LLM agent can interact with serial
devices while the user sees everything live.

## Features

- Interactive terminal on stdin/stdout (raw mode, Ctrl+] to quit)
- MCP server over stdio for Claude Code integration
- Supports local serial ports, RFC 2217, and TCP sockets (anything
  `pyserial` supports via `serial_for_url`)
- Auto-reconnect when the port disappears (USB serial going to
  bootloader, etc.)
- Ring buffer with absolute monotonic byte offsets — each MCP client
  tracks its own read position, no data is lost between calls
- Server/client architecture: multiple terminals and MCP clients share
  one server, server auto-spawns on first use and exits when idle
- Config file with port aliases and serial profiles

## Installation

```
pip install serial_mcp
```

## Quick start

Open a terminal to a serial port:

```bash
serial_mcp /dev/ttyUSB0 115200
```

A background server is automatically started. Ctrl+] to quit.

## Configuration

Config file: `~/.config/serial_mcp/config.yaml`

```yaml
server:
  # Unix domain socket (default)
  socket: ~/.config/serial_mcp/server.sock

  # Or TCP socket (for remote access)
  # address: "127.0.0.1:8808"

  # Seconds before idle server exits after last client disconnects
  grace_period: 5

# Port aliases — shortcuts for serial port URLs
alias:
  target:
    url: /dev/ttyUSB0
    profile: embedded
  debug:
    url: rfc2217://192.168.1.10:2217
    profile: fast
  nucleo:
    url: /dev/ttyACM0

# Serial profiles — reusable baud rate / settings
profile:
  default:
    baudrate: 115200
  embedded:
    baudrate: 115200
  fast:
    baudrate: 921600
```

With this config:

```bash
serial_mcp target          # opens /dev/ttyUSB0 at 115200
serial_mcp debug            # opens rfc2217://192.168.1.10:2217 at 921600
serial_mcp nucleo           # opens /dev/ttyACM0 at 115200 (default profile)
serial_mcp /dev/ttyS0 9600  # raw URL with explicit baud rate
```

## Commands

### `serial_mcp [attach] <target> [baudrate]`

Open a terminal to a serial port. `<target>` is an alias name or a
serial port URL. The baud rate is optional (defaults to the profile's
value, or 115200).

The server is auto-spawned if not already running.

```bash
serial_mcp /dev/ttyUSB0 115200
serial_mcp target
serial_mcp attach target --name my-port --raw
serial_mcp attach /dev/ttyACM0 9600 --server http://remote-host:8808
```

| Option | Description |
|---|---|
| `--name NAME` | Port name visible in MCP tools (default: alias name or URL) |
| `--raw` | Raw terminal mode (no output filtering) |
| `--server URL` | Connect to a specific server instead of config/auto-spawn |
| `--config PATH` | Use a custom config file |

#### Terminal

When stdin is a TTY, the tool enters raw terminal mode:
- Everything you type is sent to the serial port
- Everything received is printed to stdout
- Ctrl+] quits

In default mode, output is filtered: CR is mapped to CRLF, bare LF is
mapped to CRLF, non-printable control codes (except tab and ESC) are
stripped, NUL bytes are removed. Use `--raw` to disable filtering.

When stdin is not a TTY (piped), the terminal runs in headless mode
(serial output only, no keyboard input).

### `serial_mcp serve`

Start the server explicitly. Normally not needed — the server
auto-spawns when a client connects.

```bash
serial_mcp serve
serial_mcp serve --background
serial_mcp serve --buffer-size 10000000
```

| Option | Description |
|---|---|
| `--background` | Suppress output (used by auto-spawn) |
| `--buffer-size N` | Max ring buffer per port in bytes (default: 1MB) |
| `--config PATH` | Use a custom config file |

The server listens on a Unix domain socket (default:
`~/.config/serial_mcp/server.sock`) or a TCP address if configured.
It starts with no serial ports — ports are created when terminal
clients attach and removed when they disconnect.

The server exits automatically after a grace period (default: 5s) when
all clients disconnect.

### `serial_mcp mcp`

Run as an MCP server over stdio. This is what you configure Claude Code
to launch.

```bash
serial_mcp mcp
serial_mcp mcp --config /path/to/config.yaml
```

The MCP client connects to the shared server (auto-spawning it if
needed) and exposes all serial port tools over stdio. It stays alive
until the parent process (Claude Code) terminates.

## MCP tools

All tools accept a `port_id` parameter to select which serial port to
operate on. Use `list_ports` to see available ports.

### `read_serial`

Read data from the ring buffer.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `since` | int | 0 | Absolute byte offset to read from |
| `up_to` | int \| null | null | Absolute byte offset to stop at (exclusive) |
| `port_id` | string | "default" | Port to read from |

Returns `{ data, start, end }`. The `end` value is the offset to pass
as `since` on the next call to get only new data. If `start > since`,
older data was evicted from the buffer.

### `send`

Send a UTF-8 string to the serial port.

| Parameter | Type | Description |
|---|---|---|
| `data` | string | The string to send |
| `port_id` | string | Port to send to |

### `send_bytes`

Send raw bytes (hex-encoded) to the serial port.

| Parameter | Type | Description |
|---|---|---|
| `hex_data` | string | Hex string, e.g. `"0d0a"` for CR LF |
| `port_id` | string | Port to send to |

### `wait_for`

Wait for a regex pattern to appear in serial output.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `pattern` | string | | Regex pattern to search for |
| `since` | int | 0 | Search from this absolute byte offset |
| `timeout` | float | 10.0 | Timeout in seconds |
| `port_id` | string | "default" | Port to watch |

Returns `{ offset, end, match }` — the byte offset range and matched
text. Use `read_serial(since=..., up_to=...)` to get surrounding
context.

### `set_control_lines`

Set DTR and/or RTS control lines.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `dtr` | bool \| null | null | Set DTR (null = don't change) |
| `rts` | bool \| null | null | Set RTS (null = don't change) |
| `port_id` | string | "default" | Port to control |

### `send_break`

Send a serial break signal.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `duration` | float | 0.25 | Break duration in seconds |
| `port_id` | string | "default" | Port to send break on |

### `get_port_info`

Returns baud rate, connection status, buffer offsets, and control line
states (CTS, DSR, RI, CD, DTR, RTS). The `buffer_end` value is the
current absolute byte offset — use it as `since` in `read_serial` to
start reading from "now".

### `get_port_events`

Returns connection/disconnection events with their buffer offsets.
Use to detect reconnection boundaries in the data stream.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `since` | int | 0 | Only return events at or after this offset |
| `port_id` | string | "default" | Port to query |

### `set_baudrate`

Change the baud rate at runtime.

| Parameter | Type | Description |
|---|---|---|
| `baudrate` | int | New baud rate |
| `port_id` | string | Port to change |

### `list_ports`

List all currently attached serial ports with their connection status
and baud rate.

## Typical MCP usage patterns

### Following serial output

The ring buffer uses absolute byte offsets that only increase. Track
your read position to avoid re-reading data:

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

### Device reset flow

For devices like ESP32 that use DTR/RTS for bootloader entry:

```
set_control_lines(dtr=False, rts=True)   # assert RESET
set_control_lines(dtr=True, rts=False)   # assert BOOT, release RESET
set_control_lines(dtr=False, rts=False)  # release both
wait_for(pattern="waiting for download", timeout=5.0)
```

### Detecting reconnections

When a USB serial device disappears and reappears (e.g. bootloader
reset), use `get_port_events` to find the boundary:

```
events = get_port_events(since=cursor)
# [{"offset": 1523, "event": "disconnected"}, {"offset": 1523, "event": "connected"}]
```

## Configuring with Claude Code

Add serial_mcp as an MCP server:

```bash
claude mcp add serial_mcp serial_mcp mcp
```

Or add to `.mcp.json`:

```json
{
  "mcpServers": {
    "serial_mcp": {
      "command": "serial_mcp",
      "args": ["mcp"]
    }
  }
}
```

Then open a terminal to your device in a separate shell:

```bash
serial_mcp /dev/ttyUSB0 115200
```

Claude can now use the serial port tools. You see the serial I/O live
in your terminal, and Claude interacts with the same port
programmatically.

If you only need MCP access (no terminal), just configure Claude Code
and start using the tools — the server and MCP client handle everything
automatically. Attach a terminal later with `serial_mcp <port>` to see
live output.

## Architecture

```
serial_mcp serve          (background server, auto-spawned)
    ├── /ws               WebSocket for terminal clients
    └── /ws/mcp           WebSocket for MCP tool clients (JSON-RPC)

serial_mcp <target>       (terminal client, connects via /ws)
serial_mcp mcp            (MCP stdio client, connects via /ws/mcp)
```

The server manages serial ports and ring buffers. Terminal clients
attach via WebSocket for real-time I/O. The MCP stdio client proxies
tool calls to the server via JSON-RPC over WebSocket.

Port lifecycle is tied to terminal clients: when a terminal client
connects, the server opens the serial port; when it disconnects, the
port is closed. MCP clients can access any port that has an active
terminal client.

The server auto-spawns on first client connection and exits after a
configurable grace period when all clients disconnect.

## License

MIT

## Attribution

Claude Code has been used to create this project.  Human did the whole
specification, and offloaded all the boring stuff to LLM.
