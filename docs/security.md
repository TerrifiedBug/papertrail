# Papertrail — Security Model

What papertrail defends against, the controls actually implemented in the bridge,
and what is deliberately **out of scope** for the MVP. The authoritative rules live
in [`SCHEMA.md` §5](../SCHEMA.md); this file explains the reasoning.

Papertrail is a small, single-tenant notification bridge: untrusted **sources**
write events, a low-power **Pico** reads the resolved screen. The asset worth
protecting is modest — the ability to write to someone's desk display, and the
device/ingest tokens — but the bridge is internet-facing, so the basics matter.

---

## Trust boundaries

```
  untrusted internet          |  trusted host (Docker)        | trusted LAN
  --------------------------- | ----------------------------- | ----------------
  webhook sources ---------\  |                               |
  VPS dashboard cron -------+-> Caddy (TLS) -> FastAPI bridge -+-> Pico (device
  attackers / scanners ----/  |              -> SQLite        |     token, read)
```

- Everything left of Caddy is **untrusted**. Authentication, validation, and size
  limits all happen in the bridge, never in the client.
- A valid **ingest** token can write events to its one device (optionally one
  channel). A valid **device** token can read that one device's current screen
  and status, and set that one device's `poll_interval` (server-clamped to
  `[30, 3600]`). Neither can do anything else — there is no admin surface over
  HTTP.
- The Pico is treated as semi-trusted: it holds a device token scoped to itself.
  A compromised Pico can read its own screen/telemetry and nudge its own poll
  cadence within the clamp — nothing more (it cannot write events, reach another
  device, or escape the `[30, 3600]` bound). The telemetry it self-reports
  (`batt`/`rssi`/`fw`/`up`) is **untrusted** and only ever displayed, never
  trusted for an authz decision.

---

## Threat model (what we worry about)

| threat | vector | mitigation |
|--------|--------|------------|
| **Unauthorized writes** to someone's display | guessed/forged ingest token | bearer token required; stored as `sha256`; constant-time compare; `401` on miss |
| **Privilege creep across devices/channels** | a valid token used on the wrong device or channel | scope check -> `403`; ingest tokens optionally channel-scoped |
| **Token theft from the DB** | DB read / backup leak | only `sha256` hex digests persisted; plaintext never stored |
| **Resource exhaustion** | huge bodies, flooding | 8 KiB body cap -> `413`; per-token rate-limit bucket -> `429` |
| **SSRF / outbound abuse** | malicious URL in a payload | no field is ever fetched; the bridge makes **no** outbound requests on ingest |
| **Code/markup injection on the device** | HTML/JS/binary in content | allowlisted layouts + typed `content` fields; rendered as plain 8x8 glyphs, never evaluated |
| **Oversized QR / memory abuse on RP2040** | gigantic `qr_data` | `qr_data` capped at 512 chars -> `422`; QR encoded on-device, no image on the wire |
| **Replay / duplicate spam** | re-POSTing the same event | `id` dedup: first write wins, duplicates are idempotent no-ops |
| **Eavesdropping / tampering in transit** | plaintext HTTP | TLS terminated at Caddy; there is no plaintext path |

---

## Controls implemented (MVP)

### Two scoped token classes, hashed at rest

- **device** token: `GET /api/devices/:id/current`, scoped to exactly one device.
- **ingest** token: `POST /api/devices/:id/events`, scoped to one device and
  optionally to a set of channels (`channels` JSON array; `NULL` = all).
- Both presented as `Authorization: Bearer <token>`.
- Stored **only** as `token_sha256` (hex `sha256`) in SQLite; the plaintext is
  never written to disk. Lookup compares with **`hmac.compare_digest`**
  (constant-time) to avoid timing oracles on the digest.

### Strict reject matrix

The bridge fails closed, with distinct codes so misconfig is debuggable:

| condition | status |
|-----------|--------|
| missing / malformed / unknown bearer token | `401` |
| valid token, wrong device or disallowed channel | `403` |
| unknown `:id` device | `404` |
| raw body > 8 KiB (8192 bytes) | `413` |
| `layout` not in the allowlist | `422` |
| `schema` mismatch or `content` fails layout validation | `422` |
| `qr_data` length > 512 | `422` |
| `PATCH .../config` `poll_interval` non-int or missing | `422` |
| rate limit exceeded | `429` |

Out-of-range `poll_interval` is **clamped** (`[30, 3600]`), not rejected.
Telemetry query params on `GET .../current` (`batt`/`rssi`/`fw`/`up`) are
**never** a reject reason — malformed values are silently ignored so a poll never
`4xx`s on telemetry.

### Body size cap (two layers, streamed)

Raw request body is capped at **8 KiB (8192 bytes)** and rejected with `413`
*before* parsing. There are **two independent caps**, defence-in-depth:

- **At the edge:** Caddy's `request_body { max_size 8KB }` rejects oversized
  uploads before they ever reach the bridge.
- **In the bridge:** the body is read **as a stream** and the read is **aborted
  the moment the running byte count crosses 8 KiB** — the bridge never buffers an
  unbounded body into memory and never trusts a client-supplied `Content-Length`.

The streamed cap is the key defence against a **buffering DoS**: a hostile client
that lies about `Content-Length`, sends a chunked/streamed body, or trickles bytes
slowly cannot force the bridge to accumulate a large in-memory buffer — the read
stops at the cap regardless. This bounds parser/memory cost from hostile input at
both layers.

### Layout allowlist + typed content (no injection, no SSRF)

- `layout` must be one of `status_card | alert | list | metric | qr`; anything
  else is `422`. The set is **frozen** for `pico-paper.v1`.
- `content` is validated field-by-field against the layout's typed shape; unknown
  shapes are rejected.
- **No external image URLs, no embedded code, no HTML** are accepted, and the
  bridge never dereferences any field — so there is **no SSRF surface** at ingest.
  The Pico renders content as plain monospaced 8x8 glyphs (and QR codes encoded
  locally), so payload text cannot become executable markup on the device.

### Rate limiting (best-effort)

Per-token in-memory token bucket (default `rate_per_min`), returning `429` when a
token exceeds its ceiling. **Known limitation (documented in code):** the bucket
is in-memory, so it **resets on process restart and is not shared across
workers**. It is a courtesy/abuse-dampening ceiling, **not a security boundary**.
For hard guarantees, back it with Redis or a SQLite-backed counter.

### Dedup / idempotency

Event `id` is the dedup key: if it already exists, the new event is an idempotent
no-op (`200 {"status":"duplicate"}`) and the original is **never overwritten**
(first write wins). This neutralizes naive replay and makes retries safe.

### TLS in transit

Caddy terminates TLS (auto-managed Let's Encrypt certs). Both ingest and the
Pico's reads use HTTPS; there is no plaintext listener exposed.

### Secrets hygiene

Real tokens and WiFi credentials are **never committed**. The repo ships only
`.env.example` and `secrets.example.py`; `.gitignore` excludes the real
`secrets.py`, `.env`, and `*.db`. Device tokens live in the Pico's local
`secrets.py`; ingest tokens live with each source.

---

## Out of scope (MVP — accepted risk)

These are intentional non-goals. They are documented so nobody assumes a guarantee
that isn't there:

- **Hard, distributed rate limiting.** The in-memory bucket is best-effort only
  (see above). Multi-worker / multi-process enforcement needs a shared store.
- **Multi-tenant isolation / RBAC.** Single owner, flat token table. No org/user
  model, no per-endpoint roles beyond the device/ingest split.
- **Token rotation & expiry automation.** Rotation is manual (mint new, delete
  old). Tokens do not auto-expire; there is no built-in audit log of token use.
- **Replay protection beyond `id` dedup.** No nonce/timestamp signing on the wire;
  an attacker who captures a valid `(token, body)` can resend a *new* `id` until
  the token is revoked. TLS is the transport defense.
- **Request signing / mTLS.** Auth is a bearer token over TLS, not HMAC-signed
  bodies or client certificates.
- **At-rest encryption of the SQLite file.** Token digests are hashed, but event
  `content` is stored in plaintext. Protect the host/volume with OS-level controls.
- **Pico-side hardening.** The device token sits in `secrets.py` on the flash;
  physical access to the Pico exposes a token scoped to one device — read
  (`current`/`status`) plus a clamped `poll_interval` write, nothing else. No
  secure element, no attestation.
- **DoS resilience at scale.** Body cap + best-effort rate limit blunt casual
  abuse; a determined flood needs an upstream WAF/CDN in front of Caddy.
- **Abuse of `qr_data` content semantics.** The bridge bounds length (512) and the
  Pico just renders modules; it does **not** inspect what the QR encodes. A
  scanned QR could point anywhere — treat QR sources as you would any link.

---

## Deferred / future

Sketched here so the design is on record, but **not built** in the MVP. Both ride
the existing poll/control channel — no new transport, no inbound connection to the
Pico (it stays poll-only, asleep between wakes).

### One-shot device actions (`reboot` / `clear` / `force_full_refresh`)

The current `control` block carries only **idempotent settings** (`poll_interval`):
re-reading it on every poll is harmless. A one-shot **action** is different —
naively putting `{"action":"reboot"}` in `control` would re-fire on **every** poll
and wedge the device in a loop (reboot -> poll -> see reboot -> reboot ...).

The fix is an **ack handshake** keyed on a monotonic control id:

- Server assigns each pending action a `control_id` and returns it in `control`,
  e.g. `"control": {"poll_interval": 120, "action": "force_full_refresh", "control_id": 42}`.
- The Pico performs the action, then **echoes the last-applied id** on its next
  poll — piggybacked like telemetry, e.g. `GET .../current?...&ack=42`.
- The server only surfaces an action whose `control_id` is **newer** than the
  device's last `ack`. Once `ack == control_id`, the action is considered
  delivered and is **cleared** from subsequent responses — so it fires **exactly
  once** and cannot loop.
- `reboot` / `clear` are inherently lossy (the Pico may die mid-action); the ack
  is best-effort and the worst case is one missed or one repeated action, never a
  loop. `force_full_refresh` would clear ghosting on the **mono** panel (which has a
  partial-refresh mode); the shipped **tri-color B V4 is full-refresh only**, so it
  is a no-op there.

This keeps the device strictly poll-driven and requires no always-on listener,
preserving the deep-sleep battery model and the "no inbound to the Pico" boundary.

### Per-event render hints (`invert` / `full_refresh`)

Today, inversion is **implicit** and layout-bound (only `alert` `severity:"high"`
inverts + frames). A future additive `content`-adjacent hint could let any event
opt into `invert` (white-on-ink) or `full_refresh` (force a full redraw instead of
a partial one — only meaningful on the **mono** panel; the tri-color B V4 is always
full-refresh) per event. These are **render hints only** —
they change pixels/refresh mode on the device, never authz, validation, or
resolution — and would ship additively (old firmware ignores unknown hints).

---

## Quick checklist for operators

- [ ] DNS points at the host; Caddy has issued a valid cert (HTTPS only).
- [ ] Bridge port `8000` is **not** published to the internet (Caddy-only).
- [ ] Each device has its own device token; each source its own ingest token.
- [ ] Ingest tokens are channel-scoped where a source only needs one channel.
- [ ] Real `secrets.py` / `.env` / `*.db` are git-ignored and never pushed.
- [ ] Tokens rotated on suspicion of leak (mint new, delete old row).
- [ ] Host clock is NTP-synced (TTLs depend on it).
- [ ] SQLite volume is backed up and access-controlled.
