# Papertrail — OTA firmware updates

Keep deployed Picos current without USB. The web flasher ([`flashing.md`](flashing.md)) only
lands code at provision time, so a device runs whatever firmware it was last given and then
drifts from the repo. **OTA closes that gap from the bridge:** the bridge serves the firmware it
bundles, and each device pulls only what changed on its next poll.

> **The OTA contract — five guarantees.** Updates are **PULL** (the bridge never connects to a
> device), **delta** (only changed files move), **hash-verified** (every file checked against its
> manifest `sha256` before it goes live), **atomic** (per-file `tmp → rename`, never a half-written
> file), and **recoverable** (a boot-attempt guard rolls back to one known-good backup on a
> crash-loop, then **quarantines** the bad version so it is not re-pulled). The recovery guard
> itself — `boot.py` — is **immutable to OTA**: it is laid down only at flash time and never appears
> in the manifest, so an update can never replace the code that rescues a bad update.
>
> The Pico W has a ~1MB filesystem and the firmware is ≈150KB. A typical delta touches 1–2 files, so
> steady-state OTA needs only a file's worth of spare flash. The honest worst case — a full re-pull
> that **stages** every new file while a **backup** of every old file is held alongside the **live**
> set — peaks at roughly **3× the firmware** (~450KB), still comfortably inside the ~1MB FS.

- [How it works end-to-end](#how-it-works-end-to-end)
- [Bridge — manifest + files](#bridge--manifest--files)
- [Poll integration — `control.fw`](#poll-integration--controlfw)
- [Device — the OTA algorithm](#device--the-ota-algorithm)
- [Recovery — the crash-loop guard](#recovery--the-crash-loop-guard)
- [Pico storage discipline](#pico-storage-discipline)
- [The lifecycle of a firmware change](#the-lifecycle-of-a-firmware-change)
- [The rollback guarantee](#the-rollback-guarantee)
- [Hardening — firmware signing (deferred)](#hardening--firmware-signing-deferred)
- [Dashboard — version spread](#dashboard--version-spread)
- [What is host-testable](#what-is-host-testable)

---

## How it works end-to-end

```
 edit firmware/*.py          CI rebuilds the image          device polls /current
        │                            │                              │
        ▼                            ▼                              ▼
 sha256 of the file       bridge re-hashes firmware/      control.fw != local version?
   changes  ─────────────►  at startup → new `version`  ──► yes → ota.apply():
                            (control.fw advertises it)        GET manifest, diff sha,
                                                              pull only changed files,
                                                              verify, atomic-rename,
                                                              backup, machine.reset()
```

Nothing is pushed. The bridge advertises the latest version inside the **existing** poll
response; the device decides whether to act. Until a real update is waiting, OTA costs **zero**
extra requests — the version comparison is one string compare against a field the device already
receives.

---

## Bridge — manifest + files

Two LAN endpoints, served by the FastAPI bridge. **Auth = a valid *device* token (any device's
token works) OR the admin token.** A device only ever holds its own token; that is sufficient to
read firmware, because the firmware is identical for every device (config is device-local and
never travels — see below).

### `GET /api/firmware/manifest`

```json
{
  "version": "a1b2c3d4e5f6",
  "files": {
    "main.py":   "9f86d0818884…",
    "ota.py":    "fcde2b2edba5…",
    "render.py": "…",
    "poller.py": "…",
    "wifi.py":   "…",
    "ina219.py": "…",
    "epaper2in13.py":  "…",
    "epaper2in13b.py": "…",
    "qr.py":     "…",
    "lib/uQR.py": "…"
  }
}
```

- The bridge **hashes the bundled firmware files at startup** — there is no build step. `version`
  is the first **12 hex** of `sha256(canonical_json(files))`, so it changes whenever *any included
  file's* hash changes and is stable for a given file set. (`boot.py` is **not** in `files` — see
  below — so it has no effect on `version`.)
- The firmware directory comes from `PAPERTRAIL_FIRMWARE_DIR` (default: the repo `firmware/` for
  dev, `/app/firmware` in the image).
- **Included (code only):** `main.py`, `ota.py`, `render.py`, `poller.py`, `wifi.py`,
  `ina219.py`, `epaper2in13.py`, `epaper2in13b.py`, `qr.py`, `lib/uQR.py`.
- **Excluded (device-local + non-code):** `config.py`, `secrets.py`, `secrets.example.py`,
  `test_*.py`, runtime `*.txt` files, the README, `.gitignore`.
- **Excluded (immutable recovery guard):** `boot.py` — never hashed into the manifest, never OTA'd
  (see the callout below). Because `version` is `sha256` over the `files` map, a `boot.py` change
  does **not** move `version` and never triggers an update.

> **`config.py` is never OTA'd.** It holds each device's `DEVICE_ID`, pin map, panel model and
> Y-offset. Shipping it would clobber a device's identity and hardware wiring. The same is true of
> `secrets.py` (WiFi + token). OTA only carries the shared code.

> **`boot.py` is immutable to OTA.** `boot.py` *is* the recovery guard — the code that detects a
> crash-loop and rolls a bad update back. If OTA could replace it, a broken `boot.py` would also
> break the only mechanism that could rescue the device, with no way back short of USB. So `boot.py`
> is laid down **only at flash time** (the USB provision writes it as a fixed file, outside the
> manifest-driven set — see [`flashing.md`](flashing.md)), is never in the manifest, never pulled,
> never pruned, and never backed over. Changing `boot.py` is a deliberate re-flash, not an OTA.

### `GET /api/firmware/file?path=<path>`

Returns the raw bytes of one file. The bridge **validates that `<path>` is a key in the
manifest** and rejects anything else (`400`/`404`) — no `..`, no absolute paths, no traversal
outside the firmware set. You can only fetch a file the manifest already names.

---

## Poll integration — `control.fw`

OTA rides the poll the device already makes. The `GET /api/devices/{id}/current` response carries
an additive `control` block (today: `poll_interval`); it gains one field:

```json
"control": { "poll_interval": 120, "fw": "a1b2c3d4e5f6" }
```

- `control.fw` is the bridge's **latest manifest version**.
- On each poll the device compares `control.fw` to its **own running version**. Equal → do
  nothing. Different → run `ota.apply()`. So the OTA check fires only when an update is actually
  waiting; steady state is a single string compare.
- **Telemetry the other way:** the device reports its **current** firmware version as the `fw`
  query param on the poll — the local `manifest.json` version if present, else `config.FW_VERSION`
  (e.g. `pt-1.0.0`). The bridge stores it as the device's `last_fw`. The 12-hex manifest version
  fits the existing `fw` charset/length limit (`<=16`, `[A-Za-z0-9._-]`), so nothing about the
  telemetry contract changes.

---

## Device — the OTA algorithm

Lives in `firmware/ota.py`, triggered from `firmware/main.py`, guarded by `firmware/boot.py`.

**Local state:**
- `firmware/manifest.json` — the last-applied `{ version, files: { path: sha } }`. This is the
  single source of "what am I running" on the device; there is no separate `version.txt`.
- `pending_version.txt` — the version `ota.apply()` is **trying to move to**, written before the
  reset and cleared once the new code completes one clean cycle. Lets recovery name the version
  that failed.
- `bad_version.txt` — a **quarantined** version: one that crash-looped and was rolled back. The OTA
  trigger refuses to re-pull it.

**Trigger** (in the poll loop): run `ota.apply()` when `control.fw != local manifest version`
**and** `control.fw != bad_version.txt`. The second clause is the **quarantine**: a device that
just rolled a version back will not immediately re-pull the same broken firmware. It waits on
known-good code until the bridge advertises a *different* version (a fix), which clears the
quarantine automatically.

**`ota.apply()`** does, in order:

1. `GET /api/firmware/manifest` (the server manifest).
2. **Diff:** for each path where `server_sha != local_sha` (or the file is missing locally),
   mark it for pull. `boot.py` is not in the manifest, so it is never a diff candidate.
3. **Backup first:** copy the **to-be-changed current files** into `/backup/` — this is the
   known-good set used by recovery. (Only the files about to change are backed up, keeping the
   flash cost tiny.)
4. **Record the target:** write `pending_version.txt = <server version>` — so if the next boot
   crash-loops, recovery knows exactly which version to quarantine.
5. **Per-file atomic pull (protected):** for each changed path —
   `GET /api/firmware/file?path=<path>` → **verify `sha256`** against the manifest → write
   `<path>.new` → `os.rename()` over the live file (atomic on the filesystem). Create `lib/` first
   if it does not exist. The fetch only ever names a **manifest key**, so a pull can never reach
   `config.py`, `secrets.py`, `boot.py`, or any path outside the firmware set.
6. **Prune:** delete any local file that **left** the manifest — but **never** `config.py`,
   `secrets.py`, `boot.py`, `manifest.json`, or runtime `*.txt` files.
7. **Commit:** write the new `manifest.json` **last** (so a crash mid-update leaves the old
   manifest, and the device re-attempts the same update on the next boot rather than believing it
   finished).
8. `machine.reset()` — reboot into the new code.

If a hash check fails or a download is incomplete, that file's `.new` is simply never renamed over
the live file; the live file stays as-is. Because the manifest is written last, an interrupted
apply is a no-op that retries.

---

## Recovery — the crash-loop guard

`firmware/boot.py` runs **first** at every boot, before `main.py`, and owns a boot-attempt counter
persisted in flash as `boot_count.txt`. `boot.py` is **immutable to OTA** (it is never in the
manifest, never pulled — see [Bridge — manifest + files](#bridge--manifest--files)), so the guard
that rescues a bad update can never itself be replaced by one.

1. `boot.py` **increments** `boot_count.txt`, then imports/launches `main`.
2. `main.py`, after **one fully-successful cycle** (booted, polled, rendered), **resets the counter
   to 0** and **clears `pending_version.txt`** — the version it just ran is confirmed good and
   becomes the plain current version.
3. If `boot.py` sees the counter **> 3** (a crash-loop — the device keeps rebooting before it ever
   completes a clean cycle), it runs the recovery sequence:
   - **Roll back** — restore the files in `/backup/` over the current ones, returning to the last
     known-good firmware.
   - **Quarantine** — if `pending_version.txt` is set, copy it to `bad_version.txt`. The OTA trigger
     now refuses to re-pull that version, so the device cannot fall into an
     update → crash → rollback → update loop on the same broken firmware.
   - **Reset** the boot counter, clear `pending_version.txt`, and **reboot** — landing back on the
     known-good code.

A healthy update boots once, completes a cycle, zeroes the counter, clears the pending marker, and
never trips the guard. A bad update never completes a cycle, so the counter climbs past the
threshold; recovery rolls back, quarantines the bad version, and reboots — no USB, no human. The
device then stays on known-good code until the bridge ships a **different** version (a fix), which
clears the quarantine and is picked up on the next poll.

---

## Pico storage discipline

The Pico W has a ~1MB filesystem and the firmware is ≈150KB. The design is shaped by that budget:

- **Delta pulls.** Most updates touch 1–2 files; only those move. A full re-pull only happens on a
  first provision (where `manifest.json` is seeded so even that is a no-op — see
  [`flashing.md`](flashing.md)).
- **Per-file atomic writes.** Writing `<path>.new` then renaming needs only **one changed file's
  worth** of staging at a time (~tens of KB) — for a typical 1–2-file delta the flash overhead is
  negligible.
- **Worst-case peak ≈ 3× the firmware.** A full re-pull holds three copies of a file at once: the
  **live** set, the `/backup/` of every old file, and the staged `.new` of every new file. With a
  ≈150KB firmware that peaks near **~450KB** — well under the ~1MB FS, so even an all-files update
  fits, but it is the real ceiling, not "a single extra copy."
- **Exactly one backup.** `/backup/` holds only the files a given update is about to change — the
  known-good versions for rollback. There is one backup generation, not a history.
- **Device-local + guard files are sacrosanct.** `config.py`, `secrets.py`, `boot.py`,
  `manifest.json`, and runtime `*.txt` (poll-interval backstop, `boot_count.txt`,
  `pending_version.txt`, `bad_version.txt`) are never fetched, never pruned, never backed over by a
  code update.

---

## The lifecycle of a firmware change

How a one-line edit reaches every deployed tag, with no device-side action:

1. **Edit `firmware/`.** Change `render.py` (say). Its `sha256` changes.
2. **CI rebuilds the image.** `firmware/` is `COPY`'d into the bridge image; CI already builds from
   the repo. No separate firmware build/version bump is needed.
3. **The bridge re-hashes at startup.** On boot it hashes the bundled files; the changed
   `render.py` gives a new `files` map and therefore a new 12-hex `version`. `control.fw` now
   advertises it.
4. **Devices auto-update on their next poll.** Each device sees `control.fw != local version`,
   runs `ota.apply()`, pulls **only `render.py`** (the one changed file), verifies its hash,
   atomically renames it in, backs up the old one, writes the new `manifest.json`, and resets.
5. **Steady state.** After the reset the device's `local version == control.fw`; subsequent polls
   are plain string-compare no-ops. Its `last_fw` telemetry now matches the bridge, so the
   dashboard shows it as current.

No "deploy to devices" button, no scheduled job. Shipping the bridge ships the firmware.

---

## The rollback guarantee

> **A failed update cannot brick a device.** Every change is staged (`.new`), hash-checked, and
> renamed atomically, so the device never runs a half-written or corrupt file. The old versions of
> every file an update touches are copied to `/backup/` *before* the swap. If the new firmware
> fails to complete a single clean cycle, the boot counter climbs past the crash-loop threshold and
> `boot.py` restores `/backup/` over the live files — returning the device to the last known-good
> firmware with no human intervention. The worst case for a bad OTA is a few reboots and an
> automatic return to the previous version.

The one rule that makes this hold: **the new `manifest.json` is written last.** Until it is
written, the device still believes it is running the old version, so an interruption at any earlier
step is a clean retry, not a corrupt state.

---

## Hardening — firmware signing (deferred)

OTA today proves **integrity, not authenticity**. Every file is checked against its manifest
`sha256`, and the manifest's own `version` is a hash of the file set — so the device can prove the
bytes it pulled match the manifest it was handed. What it **cannot** yet prove is that the manifest
came from a bridge the operator trusts. The manifest is fetched over **LAN HTTP** and trusted on
sight; anyone who can answer `GET /api/firmware/manifest` (and the matching `/file` requests) with a
self-consistent set could serve code the device would install.

**v1 threat model — why that is acceptable today.** OTA lives entirely on the **home LAN**, and both
endpoints are **device-token-gated** (a valid device or admin token — see
[`security.md`](security.md)). An attacker would already need to be on the LAN *and* hold a device
token, at which point the bridge itself is the trust root. There is no internet-facing OTA surface.

**Deferred hardening — sign the manifest.** A future revision **signs the manifest** and the device
**verifies the signature before trusting any `sha`**:

- **HMAC** with a key **baked into flash at provision time** (the simplest fit — symmetric, tiny,
  no asymmetric crypto on the Pico), or
- **Ed25519** — the bridge signs with a private key, the device ships only the public key; even a
  compromised bridge image cannot mint a valid update without the signing key.

Either way the check moves the guarantee from *"these bytes match this manifest"* to *"this manifest
was issued by the holder of the signing key"* — closing the LAN-MITM gap. Tracked in
[`roadmap.md`](roadmap.md); complements the deferred request-signing / mTLS note in
[`security.md`](security.md).

---

## Dashboard — version spread

The admin dashboard ([`dashboard.md`](dashboard.md)) surfaces the fleet's firmware state:

- **Latest version:** `GET /api/admin/firmware → { "version": "a1b2c3d4e5f6" }` (admin-token; a
  small read of the bridge's startup-computed manifest version).
- **Per-device:** each device's stored `last_fw` (reported via poll telemetry). The dashboard
  **highlights out-of-date devices** — any whose `last_fw` differs from the latest version.

OTA is **pull-only**: there is no "push update" action. A device updates itself on its next poll;
the dashboard's job is visibility (who is current, who is lagging), not triggering.

---

## What is host-testable

The network and filesystem IO is isolated so the **pure decision logic** runs on a host (CPython)
without a Pico:

- **Manifest diff** — given a server manifest and a local manifest, which paths need pulling
  (changed sha or missing locally) and which need pruning (left the manifest, minus the protected
  set).
- **Version compare / should-update** — does `control.fw` differ from the local version **and** is
  it not the quarantined `bad_version` (so a rolled-back device skips a known-bad update).
- **Hash-verify decision** — does a fetched file's `sha256` match the manifest entry (accept vs
  reject the rename).
- **Crash-loop decision** — given `boot_count.txt`, should recovery restore `/backup/`
  (counter > 3), and which version (`pending_version.txt`) to write to `bad_version.txt`.

The thin wrappers that actually do HTTP `GET`, write files, and call `machine.reset()` are kept
out of those functions so the branching logic is unit-tested the same way as the rest of the
firmware (`firmware/test_*.py`).

---

See also: [`flashing.md`](flashing.md) (Layer A.2 — USB provision that seeds the first manifest),
[`roadmap.md`](roadmap.md), [`security.md`](security.md).
