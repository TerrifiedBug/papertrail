# papertrail — integration guide for AI agents

**Point your coding agent at this file** and ask it to build a "send a screen to my
papertrail display" skill/tool. Everything it needs is here: one endpoint, one auth
header, a declarative `layout` + `content`. No SDK.

papertrail drives a battery e-paper display. You POST an **event**; the bridge stores
it and resolves the **current screen** a device shows. The device polls — you never
talk to it directly.

---

## The one call

```
POST {BASE_URL}/api/devices/{device_id}/events
Authorization: Bearer {INGEST_TOKEN}
Content-Type: application/json
```

- `BASE_URL` — the bridge, e.g. `http://192.168.1.50:8000` on the LAN (or an HTTPS proxy).
- `device_id` — the target display, e.g. `kitchen-01`.
- `INGEST_TOKEN` — an ingest token (may be **channel-scoped**: only accepts its channel).
- Body ≤ **8 KiB** (else `413`).

### Request body (the "envelope")

| field         | req | notes |
|---------------|-----|-------|
| `schema`      | yes | always `"pico-paper.v1"` |
| `id`          | yes | unique string, `1..128` of `[A-Za-z0-9._:-]`. **Re-posting the same id is a no-op (dedup, first write wins)** — generate a fresh id per distinct update. |
| `device`      | yes | must equal `device_id` in the path; unknown device → `404` |
| `channel`     | yes | logical channel the device subscribes to, `1..64` chars. A channel-scoped token that doesn't match → `403` |
| `kind`        | no  | `"base"` (default) or `"interrupt"` — see below |
| `ttl_seconds` | no  | **interrupt only**; omitted/`0` → default **300s**; cap `604800` (7d). Ignored for base. |
| `layout`      | yes | one of `status_card` `alert` `list` `metric` `qr` |
| `content`     | yes | per-layout shape (below); extra/unknown fields → `422` |

### base vs interrupt (this is the whole model)

- **`base`** — a persistent screen. Stays until a newer base on the same channel replaces
  it, or it's deleted. Ignores `ttl_seconds`. Use for ambient status, a wifi QR, a dashboard.
- **`interrupt`** — a temporary overlay. Expires after its TTL, then the screen falls back
  to the newest base (or the device's idle fallback). Use for alerts and transient notices.

Resolution = **newest live interrupt → newest base → fallback**. There is no priority number.

> ⚠️ Default is `base` = **sticks forever**. If you want a message to auto-clear, you MUST
> send `"kind": "interrupt"`.

### Responses

| code | meaning |
|------|---------|
| `201` | stored |
| `200` | duplicate `id` (dedup no-op) |
| `403` | token not allowed on this channel |
| `404` | unknown device |
| `413` | body too large (>8 KiB) |
| `422` | bad envelope / `content` doesn't match the layout |
| `401` | missing/invalid token |

---

## Layouts + `content` shapes

The 8×8 font is **ASCII-only**; the listed caps are render limits — pre-trim or text clips.
`base` examples omit `ttl_seconds` (ignored); `interrupt` examples include it.

### `status_card` — heading + status word + body lines
```jsonc
{ "title": str≤12, "status": str≤8, "subtitle": str≤30, "lines": [str≤30, …up to 5], "footer": str≤30 }
```
```json
{ "schema":"pico-paper.v1","id":"srv-2026-06-28T14:02","device":"kitchen-01","channel":"home.status",
  "kind":"base","layout":"status_card",
  "content":{"title":"Home Server","status":"OK","subtitle":"All services nominal",
             "lines":["CPU 12%","RAM 41%","Disk 63%","Up 18d"],"footer":"updated 14:02"} }
```

### `alert` — severity notice (high = red banner + frame on tri-color)
```jsonc
{ "severity": "low"|"med"|"high", "title": str≤15, "message": str (wraps, ~4×30), "footer": str≤30 }
```
```json
{ "schema":"pico-paper.v1","id":"leak-0001","device":"kitchen-01","channel":"home.alerts",
  "kind":"interrupt","ttl_seconds":600,"layout":"alert",
  "content":{"severity":"high","title":"Water Leak","message":"Moisture under the sink. Shut the valve.","footer":"sensor-3"} }
```

### `list` — title + checklist (`[ ]` glyphs are decorative; not interactive)
```jsonc
{ "title": str≤15, "items": [str≤26, …up to 6], "footer": str≤30 }
```

### `metric` — one big number + unit + trend
```jsonc
{ "label": str≤30, "value": str≤7, "unit": str≤4, "trend": str≤30, "footer": str≤30 }
```
`value` is a **string** (preserves formatting). `trend` is ASCII (e.g. `"UP +0.4 vs 1h"`).

### `qr` — title + QR (rendered on-device from `qr_data`) + caption
```jsonc
{ "title": str≤15, "qr_data": str (1..512), "caption": str (wraps, ~7×17) }
```

---

## Build the skill

Implement a single function and expose it as your tool/skill:

```
send_screen(device_id, channel, layout, content,
            kind="base", ttl_seconds=None, id=None) -> bool
```

Behavior:
1. Build the envelope: `schema="pico-paper.v1"`, a unique `id` (timestamp/uuid if not given),
   `device=device_id`, plus the passed fields. Include `ttl_seconds` only for interrupts.
2. `POST {BASE_URL}/api/devices/{device_id}/events` with `Authorization: Bearer {INGEST_TOKEN}`.
3. Treat `201` and `200` as success; surface `4xx` bodies (they explain the validation error).
4. Read `BASE_URL` + `INGEST_TOKEN` from config/env — never hardcode the token.

Pick `kind`: status / dashboards / ambient → **base**; alerts / notifications → **interrupt** with a TTL.

### curl
```bash
curl -sS -X POST "$BASE_URL/api/devices/kitchen-01/events" \
  -H "Authorization: Bearer $INGEST_TOKEN" -H 'Content-Type: application/json' \
  -d '{"schema":"pico-paper.v1","id":"build-42","device":"kitchen-01","channel":"ci",
       "kind":"interrupt","ttl_seconds":1800,"layout":"status_card",
       "content":{"title":"CI","status":"PASS","subtitle":"main #42","lines":[],"footer":"just now"}}'
```

### Python
```python
import time, requests

def send_screen(base, token, device, channel, layout, content,
                kind="base", ttl_seconds=None, id=None):
    body = {"schema": "pico-paper.v1", "id": id or f"{layout}-{int(time.time())}",
            "device": device, "channel": channel, "kind": kind,
            "layout": layout, "content": content}
    if kind == "interrupt" and ttl_seconds is not None:
        body["ttl_seconds"] = ttl_seconds
    r = requests.post(f"{base}/api/devices/{device}/events",
                      headers={"Authorization": f"Bearer {token}"}, json=body, timeout=10)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"papertrail {r.status_code}: {r.text}")
    return True
```

Full field-by-field contract: [`../SCHEMA.md`](../SCHEMA.md). Live API docs: `GET /docs` on the bridge.
