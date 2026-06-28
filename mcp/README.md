# papertrail MCP server

A standalone [MCP](https://modelcontextprotocol.io) server that lets an agent push
screens to a **papertrail** e-paper display. papertrail is a webhook -> e-paper
bridge: you POST an event, the bridge stores it and resolves the *current screen*
the device shows on its next poll. This server wraps that one endpoint as MCP
tools so Claude (Code or Desktop) can "send a screen" directly.

It builds the frozen `pico-paper.v1` envelope and POSTs it to
`{PAPERTRAIL_URL}/api/devices/{device}/events` with a Bearer ingest token. The
full wire contract is in [`../SCHEMA.md`](../SCHEMA.md) and
[`../docs/for-agents.md`](../docs/for-agents.md).

## Tools

| tool | what it does |
|------|--------------|
| `send_screen(device, channel, layout, content, kind="base", ttl_seconds=None, id=None)` | Generic: send any of the 5 layouts. Auto-generates a unique `id` if none given. `ttl_seconds` is only sent for `kind="interrupt"`. |
| `send_status_card(device, channel, title, status="", subtitle="", lines=None, footer="", kind="base", ttl_seconds=None)` | Convenience wrapper for the `status_card` layout. |
| `send_alert(device, channel, title, message, severity="high", footer="", ttl_seconds=600)` | Send an `alert` as a temporary `interrupt` overlay that auto-clears after the TTL. |
| `send_metric(device, channel, label, value, unit="", trend="", footer="", kind="base", ttl_seconds=None)` | Convenience wrapper for the `metric` layout (one big number). |
| `list_devices()` | List known devices. Admin-gated: needs `PAPERTRAIL_ADMIN_TOKEN` (an ingest token cannot list devices). |

### base vs interrupt

- **`base`** (default) = a persistent screen. It **sticks until replaced** by a
  newer base on the same channel. `ttl_seconds` is ignored for base, so the
  server is not even sent one.
- **`interrupt`** = a temporary overlay that **auto-clears** after `ttl_seconds`
  (omitted/`0` -> 300s default; capped at 604800 = 7 days), then the screen falls
  back to the newest base. Use this for alerts and transient notices.

The 5 layouts are `status_card`, `alert`, `list`, `metric`, `qr`. Each has its own
`content` shape and render caps -- see [`../SCHEMA.md`](../SCHEMA.md) section 3.

> ASCII only: the device's 8x8 font is ASCII-only. Non-ASCII (e.g. `19°C`, em
> dashes, smart quotes) is sanitized/dropped on the device -- `"19°C"` renders as
> `"19C"`. Prefer plain ASCII in every string you send.

## Environment

| var | required | meaning |
|-----|----------|---------|
| `PAPERTRAIL_URL` | yes | base URL of the bridge, e.g. `http://192.168.1.50:8000` |
| `PAPERTRAIL_TOKEN` | yes | an **ingest** token (may be channel-scoped) used for sending screens |
| `PAPERTRAIL_ADMIN_TOKEN` | no | only needed for `list_devices()`; an admin token |

No secrets are hardcoded -- everything is read from the environment.

## Install & run

Requires Python 3.10+. Install the two runtime dependencies (`mcp`, `httpx`):

```bash
pip install -r requirements.txt
```

Run the server over stdio:

```bash
PAPERTRAIL_URL=http://192.168.1.50:8000 \
PAPERTRAIL_TOKEN=your-ingest-token \
python mcp/papertrail_mcp.py
```

An MCP client (Claude Code / Claude Desktop) normally launches it for you using
the config below -- you rarely run it by hand.

## Add it to a Claude Code / Claude Desktop MCP config

Add this `mcpServers` entry to your client config (Claude Desktop:
`claude_desktop_config.json`; Claude Code: `~/.claude.json` or a project
`.mcp.json`). Use an **absolute** path to `papertrail_mcp.py`:

```json
{
  "mcpServers": {
    "papertrail": {
      "command": "python",
      "args": ["/absolute/path/to/papertrail/mcp/papertrail_mcp.py"],
      "env": {
        "PAPERTRAIL_URL": "http://192.168.1.50:8000",
        "PAPERTRAIL_TOKEN": "your-ingest-token",
        "PAPERTRAIL_ADMIN_TOKEN": "optional-admin-token"
      }
    }
  }
}
```

Drop `PAPERTRAIL_ADMIN_TOKEN` if you don't need `list_devices()`. A copy of this
stanza is also in [`papertrail.mcp.json`](papertrail.mcp.json).

## Examples (what the agent calls)

```text
send_status_card(device="kitchen-01", channel="home.status",
                 title="Home Server", status="OK",
                 subtitle="All services nominal",
                 lines=["CPU 12%", "RAM 41%", "Disk 63%"],
                 footer="updated 14:02")

send_alert(device="kitchen-01", channel="home.alerts",
           title="Water Leak",
           message="Moisture under the sink. Shut the valve.",
           severity="high", ttl_seconds=600)

send_screen(device="hallway-01", channel="guest", layout="qr",
            content={"title": "Guest WiFi",
                     "qr_data": "WIFI:T:WPA;S:GuestNet;P:welcome123;;",
                     "caption": "Scan to join GuestNet."})
```
