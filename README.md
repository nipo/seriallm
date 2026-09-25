# SeriaLLM

Serial terminal emulator with MCP (Model Context Protocol) integration.
Combines `miniterm`/`tio`-like interactive terminal access with
programmatic control via MCP, so an LLM agent can interact with serial
devices while the user sees everything live.

## Features

- Interactive terminal on stdin/stdout (raw mode, Ctrl+] to quit)
- MCP server over stdio for LLM integration
- Supports local serial ports, RFC 2217, and TCP sockets (anything
  `pyserial` supports via `serial_for_url`)
- Auto-reconnect when the port disappears (USB serial going to
  bootloader, etc.)
- Server-side offset expressions: the agent can describe data ranges
  symbolically ("from last reconnect to first occurrence of DONE")
  and the server resolves them, avoiding back-and-forth round-trips
- Server/client architecture: multiple terminals and MCP clients share
  one server, server auto-spawns on first use and exits when idle
- Config file with port aliases and serial profiles
- Received data is timestamped: MCP tools report receive times next to
  offsets, and the terminal can prefix lines with absolute or relative
  times

## Installation

```
pip install seriallm
```

## Quick start

Open a terminal to a serial port:

```bash
seriallm /dev/ttyUSB0 115200
```

A background server is automatically started. Ctrl+] to quit.

## Configuring with Claude Code

Add seriallm as an MCP server:

```bash
claude mcp add seriallm seriallm mcp
```

Or add to `.mcp.json`:

```json
{
  "mcpServers": {
    "seriallm": {
      "command": "seriallm",
      "args": ["mcp"]
    }
  }
}
```

Then open a terminal to your device in a separate shell:

```bash
seriallm /dev/ttyUSB0 115200
```

Claude can now use the serial port tools. You see the serial I/O live
in your terminal, and Claude interacts with the same port
programmatically.

If you only need MCP access (no terminal), just configure Claude Code
and start using the tools — the server and MCP client handle everything
automatically. Attach a terminal later with `seriallm <port>` to see
live output.

### Companion skill

`agents/skills/seriallm-offsets/` documents the byte-range and pattern
matching system the tools share: the buffer offset model, `since`/`up_to`
semantics and every offset expression. Copy or symlink it into your
agent's skills directory so the tool descriptions can stay short:

```bash
ln -s "$PWD/agents/skills/seriallm-offsets" ~/.claude/skills/
```

## What can an agent do with it?

An LLM agent connected via MCP can:

- **Read serial output** from an embedded device, MCU, or any serial
  peripheral — boot logs, command responses, debug traces.
- **Send commands** to the device — AT commands, shell input, custom
  protocols.
- **Wait for events** — block until a specific pattern appears in the
  output (device ready, test complete, error detected).
- **Search logs** — grep through buffered output with regex, scoped to
  specific time ranges (e.g. "only after last reboot").
- **Control hardware lines** — toggle DTR/RTS for device reset or
  bootloader entry (ESP32, STM32, etc.).
- **Dump data** — save a range of serial output to a file for offline
  analysis.

### Why this is better than piping serial output to a file

Traditional approaches require the agent to manage raw byte streams,
track read positions, and make multiple round-trips to correlate events.
SeriaLLM handles all of this server-side:

- **No polling**: the agent asks "wait until you see `test OK`" and gets
  a single response when it happens (or a timeout).
- **No offset bookkeeping**: instead of manually tracking cursors, the
  agent says "read everything since the last reconnect" or "grep
  between the boot message and the done marker" — the server resolves
  the offsets.
- **No data transfer overhead**: `grep` and `dump_to_file` process data
  on the server without shipping the entire buffer through MCP.
- **Shared view**: the human sees the exact same serial stream live in
  their terminal. No separate log files, no missed output.

### Typical agent workflows

**Run a test and check results (single call):**

```
grep(pattern="FAIL|ERROR",
     since={"method": "last_reconnect"},
     up_to={"method": "wait_for_match", "pattern": "test complete", "timeout": 30})
```

One call: waits for the test to finish, then returns every error/failure
line from the current boot session. The `up_to` inherits its search start
from the resolved `since`, so it can't match stale "test complete" lines
from earlier sessions.

**Send a command and read the response:**

```
send(data="version\r\n")
read_serial(since={"method": "first_match", "pattern": "version",
                   "edge": "end", "since": -50},
            up_to={"method": "wait_for_match", "pattern": ">", "timeout": 5})
```

**Reset a device and wait for boot:**

```
set_control_lines(dtr=False, rts=True)
set_control_lines(dtr=False, rts=False)
read_serial(since={"method": "last_reconnect"},
            up_to={"method": "wait_for_match", "pattern": "ready>", "timeout": 10})
```

**Measure boot time:**

```
resolve_offset(offset={"method": "first_match", "pattern": "ready>",
                       "since": {"method": "last_reconnect"}})
```

Returns `{offset, time}`: subtract the `time` of the last `connected`
event from `get_port_events` to get the boot duration.

## Configuration

Config file: `~/.config/seriallm/config.yaml`

```yaml
server:
  # Unix domain socket (default)
  socket: ~/.config/seriallm/server.sock

  # Or TCP socket (for remote access)
  # address: "127.0.0.1:18808"

  # Seconds before idle server exits after last client disconnects
  grace_period: 5

  # Max ring buffer size per port (default: 1MB)
  buffer_size: 1000000

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
seriallm target          # opens /dev/ttyUSB0 at 115200
seriallm debug            # opens rfc2217://192.168.1.10:2217 at 921600
seriallm nucleo           # opens /dev/ttyACM0 at 115200 (default profile)
seriallm /dev/ttyS0 9600  # raw URL with explicit baud rate
```

## Commands

### `seriallm [attach] <target> [baudrate]`

Open a terminal to a serial port. `<target>` is an alias name or a
serial port URL. The baud rate is optional (defaults to the profile's
value, or 115200).

The server is auto-spawned if not already running.

| Option | Description |
|---|---|
| `--name NAME` | Port name visible in MCP tools (default: alias name or URL) |
| `--raw` | Raw terminal mode (no output filtering) |
| `-t`, `--timestamps abs\|rel` | Prefix each line with the receive time of its first byte: wall time (`abs`) or delta to the previous line (`rel`). Not compatible with `--raw` |
| `--server URL` | Connect to a specific server instead of config/auto-spawn |
| `--config PATH` | Use a custom config file |

### `seriallm serve`

Start the server explicitly. Normally not needed — the server
auto-spawns when a client connects.

| Option | Description |
|---|---|
| `--background` | Suppress output (used by auto-spawn) |
| `--buffer-size N` | Max ring buffer per port in bytes |
| `--config PATH` | Use a custom config file |

### `seriallm mcp`

Run as an MCP server over stdio (for Claude Code integration).

## Architecture

```
seriallm serve          (background server, auto-spawned)
    ├── /ws               WebSocket for terminal clients
    └── /ws/mcp           WebSocket for MCP tool clients (JSON-RPC)

seriallm <target>       (terminal client, connects via /ws)
seriallm mcp            (MCP stdio client, connects via /ws/mcp)
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

Claude Code has been used to create this project. Human did the whole
specification, and offloaded all the boring stuff to LLM.
