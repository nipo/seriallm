---
name: seriallm-offsets
description: Use when calling seriallm serial tools (read_serial, grep, dump_to_file, get_port_events) and picking a `since`/`up_to` byte range, or writing an offset expression such as last_reconnect, first_match, latest_match or wait_for_match. Covers the ring-buffer offset model, range semantics, match scoping, blocking waits and their failure modes.
---

# Serial buffer ranges and offset expressions

Every byte received from a serial port is appended to a per-port ring
buffer and numbered with a monotonic offset. All range parameters name
those offsets. Resolving a range happens on the server, so one tool call
can express "from the last reboot until the next `DONE`" without any
intermediate round-trip.

## Buffer model

- Offsets count bytes received since the port was first opened. They
  never restart, never repeat, and survive reconnections.
- The buffer holds only the last `buffer_size` bytes (1 MB by default).
  `get_port_info` reports the live window as `buffer_start`/`buffer_end`.
- Offsets older than `buffer_start` are not an error: reads clamp to the
  window. Compare the `start` you asked for with the `start` you got back
  to detect that data was dropped.
- NUL bytes are stripped from the stream before buffering.

## Ranges

`since` is inclusive, `up_to` is exclusive; the range is `[since, up_to)`.
`since` defaults to 0 (buffer start), `up_to` defaults to the buffer end
at the moment the call resolves.

`read_serial`, `grep` and `dump_to_file` take both. `get_port_events`
takes `since` only. `read_serial` and `dump_to_file` report the
`start`/`end` they actually used; feed that `end` back as the next call's
`since` to keep reading without gaps or overlap. `grep` reports an
absolute offset per matched line instead.

An inverted or already-consumed range (`up_to` <= `since`) is not an
error: it yields empty data, or no matches. Before concluding the device
said nothing, check that the range was not empty to begin with.

## Integer offsets

- Positive: an absolute offset.
- Negative: relative to the current buffer end, so `-500` means "the last
  500 bytes", clamped to the buffer start.

## Expression objects

Instead of an integer, pass an object with a `method` field:

| Expression | Resolves to |
|---|---|
| `{"method": "last_reconnect"}` | Offset of the most recent `connected` event |
| `{"method": "last_disconnect"}` | Offset of the most recent `disconnected` event |
| `{"method": "first_match", "pattern": "<regex>"}` | First match in the searched range |
| `{"method": "latest_match", "pattern": "<regex>"}` | Last match in the searched range |
| `{"method": "wait_for_match", "pattern": "<regex>", "timeout": <s>}` | First match, blocking until it appears |

The port records a `connected` event on every successful open and a
`disconnected` event on every loss, so `last_reconnect` is the boundary
of the current device session — the right `since` for anything scoped to
"since the board rebooted". Both event methods resolve to 0 when no such
event is known, which also happens once an old event scrolls out of the
ring buffer.

`wait_for_match` returns immediately if the pattern is already present in
its searched range; it only blocks when the pattern has not arrived yet.
`timeout` defaults to 10 seconds and a timeout is an error, not an empty
result. Use it as `up_to` to make a single call wait for a delimiter and
return the data before it.

Unknown fields and bad `edge` values are rejected, so a typo fails loudly
instead of being ignored.

## Picking the edge of a match

Match expressions take `edge`, either `"start"` (default) or `"end"`,
selecting which side of the matched text the offset lands on. Combined
with the half-open range, this is what includes or excludes the marker
itself:

- `since` with `edge: "end"` starts just after the marker, excluding it.
- `up_to` with `edge: "start"` stops just before the marker, excluding it.
- Swap either to `"start"`/`"end"` respectively to keep the marker.

## Scoping a match

A match expression only searches from its `since` field (itself an offset
expression, nestable) to the current buffer end. Without it, it searches
the whole buffer — which usually finds a stale hit from an earlier
session.

When an expression is used as a tool's `up_to`, its `since` defaults to
the tool's own resolved `since`. This is the common case and needs no
repeating:

```
grep(pattern="ERROR|FAIL",
     since={"method": "last_reconnect"},
     up_to={"method": "wait_for_match", "pattern": "test complete",
            "timeout": 30})
```

The wait starts at the reconnect offset, so it cannot latch onto a
"test complete" printed before the reboot. Set `since` explicitly on the
inner expression to override this. Note that the inheritance is one level
deep: a `since` nested inside another expression falls back to the buffer
start, not to the tool's `since`.

The search is never bounded by the tool's `up_to`; only by `since`.

## Patterns

Patterns are Python regular expressions applied to the range decoded as
UTF-8. No flags are set — use inline `(?i)`, `(?m)`, `(?s)` when needed.
In `grep` the text is split on `\n` first and each line is matched on its
own, so `^` and `$` behave per line there, but a CRLF stream leaves the
`\r` in place: write `ERROR\r?$`, not `ERROR$`.

On data that is not valid UTF-8, undecodable bytes become a replacement
character and the resulting offsets drift. Match expressions are for text
protocols; use integer offsets for binary streams.

## Failure modes

`first_match` and `latest_match` error when the pattern is absent or when
their searched range is empty; `wait_for_match` errors on timeout. All
three abort the whole tool call, so a failed `up_to` returns no data even
if the range start was fine. When a marker is merely likely, prefer a
plain integer `up_to`, or `grep` for the marker first.

## Recipes

Read everything printed since the board last came up:

```
read_serial(since={"method": "last_reconnect"})
```

Send a command and capture just its response, between the local echo and
the next prompt:

```
send(data="version\r\n")
read_serial(since={"method": "first_match", "pattern": "version",
                   "edge": "end", "since": -200},
            up_to={"method": "wait_for_match", "pattern": ">",
                   "timeout": 5})
```

The `since: -200` keeps the echo search in the recent tail instead of
matching the first "version" ever printed.

Reset a device and wait for it to be ready:

```
set_control_lines(dtr=False, rts=True)
set_control_lines(dtr=False, rts=False)
read_serial(since={"method": "last_reconnect"},
            up_to={"method": "wait_for_match", "pattern": "ready>",
                   "timeout": 10})
```

Save the last boot for offline analysis, starting at the banner rather
than at the reconnect:

```
dump_to_file(path="/tmp/boot.log",
             since={"method": "latest_match", "pattern": "Booting",
                    "since": {"method": "last_reconnect"}})
```

## Prefer expressions over bookkeeping

Do not call `get_port_info` or `get_port_events` to find an offset and
then pass it to a second call: the offset is stale by the time it comes
back, and the extra round-trip buys nothing. Describe the boundary
symbolically and let the server resolve it. Reach for `get_port_events`
only when the reconnection history itself is what you want to inspect.
