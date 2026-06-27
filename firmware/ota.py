# papertrail OTA updater (MicroPython, Pico W).
#
# Firmware updates are PULL, delta, sha256-verified, atomic, with a recovery guard
# (boot.py) behind it. The Pico W has ~1MB of filesystem and the firmware is ~150KB,
# so we use per-file atomic writes + delta pulls + ONE known-good /backup snapshot.
#
# SAFETY IS PARAMOUNT. The apply() sequence is ordered so a failure at any step
# leaves the device on its current, working firmware:
#   1. fetch the server manifest
#   2. plan the delta (manifest_diff): which paths to pull, which to delete
#   3. download EVERY changed file to '<path>.new' and verify its sha256 FIRST.
#      A network/sha failure here means NO live file has been touched -> abort,
#      delete the .new staging files, the device stays 100% on the old firmware.
#   4. back up the to-be-changed CURRENT files (+ the old manifest) into /backup
#   5. atomically os.rename each '<path>.new' over the old file, delete removed ones
#   6. write the new manifest.json LAST -- this is the commit point / new version
#   7. machine.reset() into the new firmware
# If the new firmware then crash-loops, boot.py restores /backup (see boot.py).
#
# The PURE decision helpers (manifest_diff / should_update / verify / should_restore)
# take NO hardware and are exercised by test_logic.py on host CPython. All network /
# filesystem / reset IO is guarded behind the urequests import so this module still
# imports + py_compiles on a host.

try:
    import urequests
    _HW = True
except ImportError:                         # running on a host (tests/py_compile)
    urequests = None
    _HW = False

try:
    import machine
except ImportError:
    machine = None

try:
    import os
except ImportError:                         # never on CPython, belt-and-braces
    os = None

try:
    import hashlib                           # CPython + recent MicroPython
except ImportError:
    import uhashlib as hashlib               # older MicroPython

import json
import config


class OTAError(Exception):
    pass


# Files OTA must NEVER touch -- neither pull/overwrite NOR delete:
#   * config.py / secrets.py / secrets.example.py -- device-local identity/creds/pins
#     (clobbering them would change DEVICE_ID/token/pin map out from under the board).
#   * manifest.json -- the local commit point itself (written by apply(), not pulled).
#   * boot.py -- the recovery guard. It is laid down at flash time ONLY; an OTA that
#     could replace boot.py would be a self-defeating recovery guard, so it is
#     immutable to OTA (the bridge also drops it from the manifest; we align here).
#   * any runtime *.txt backstop (last_etag / poll_interval / boot_count / bad_fw /
#     pending_fw) -- device-local state, never server-managed.
_PROTECTED = ("config.py", "secrets.py", "secrets.example.py", "manifest.json",
              "boot.py")


def _basename(path):
    return path.rsplit("/", 1)[-1]


def _is_protected(path):
    """A path OTA must never pull/overwrite OR delete (defense-in-depth even though
    the server already excludes these). PURE."""
    base = _basename(path)
    if base in _PROTECTED:
        return True
    if base.endswith(".txt"):
        return True
    return False


# ==========================================================================
# PURE, host-testable decision helpers (no hardware, no IO).
# ==========================================================================
def manifest_diff(local_files, server_files):
    """Plan a delta update. PURE.

    Returns (to_pull, to_delete), both sorted for determinism:
      to_pull   = every server path whose sha differs from local (or is missing
                  locally), EXCEPT protected files -- we NEVER pull/overwrite
                  config.py / secrets.py / boot.py / manifest.json / *.txt, even if
                  the server advertises a different sha (defense-in-depth: the
                  server already excludes them, but the device must not rely on it).
      to_delete = every local path that left the server manifest, EXCEPT those same
                  protected files.
    """
    local_files = local_files or {}
    server_files = server_files or {}

    to_pull = [p for p in server_files
               if local_files.get(p) != server_files[p] and not _is_protected(p)]
    to_delete = [p for p in local_files
                 if p not in server_files and not _is_protected(p)]
    return sorted(to_pull), sorted(to_delete)


def should_update(control_fw, local_version):
    """Decide whether a poll's advertised fw warrants an OTA check. PURE.

      control_fw None / "" (no advert, or a 304 with no body) -> False (skip)
      control_fw == local_version                              -> False (skip)
      control_fw != local_version                              -> True  (update)
    """
    if not control_fw:
        return False
    return control_fw != local_version


def verify(content_bytes, expected_sha):
    """True iff sha256(content_bytes) == expected_sha (case-insensitive hex). PURE.

    A None/empty expected_sha, or any hashing error, returns False -- we never
    write a file we could not positively verify.
    """
    if not expected_sha:
        return False
    try:
        actual = _sha256_hex(content_bytes)
    except Exception:
        return False
    return actual == str(expected_sha).lower()


def should_restore(boot_count, max_attempts):
    """Crash-loop decision (mirrors boot.py's inline guard). PURE.

    True when the boots-without-a-clean-cycle counter has exceeded the cap
    (counter <= max_attempts -> ok; counter > max_attempts -> restore /backup).
    An unreadable/garbage counter -> False (don't restore on a misread).
    """
    try:
        return int(boot_count) > int(max_attempts)
    except (TypeError, ValueError):
        return False


def _hexlify(b):
    """bytes -> lowercase hex str. Works on CPython and MicroPython (no binascii)."""
    return "".join("%02x" % x for x in b)


def _sha256_hex(data):
    h = hashlib.sha256()
    h.update(data)
    return _hexlify(h.digest())


# ==========================================================================
# Local manifest state (the last-applied { version, files:{path:sha} }).
# Reads are host-safe (plain file IO); they just return {} / None off-device.
# ==========================================================================
def _read_local_manifest():
    try:
        with open(config.MANIFEST_FILE) as f:
            m = json.loads(f.read())
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def local_version():
    """The running firmware version = local manifest.json's version, or None.

    Used both as telemetry (`fw` reported to the server) and as the left-hand side
    of should_update(). None when the device has never applied/seeded a manifest.
    """
    v = _read_local_manifest().get("version")
    return v if v else None


# ==========================================================================
# Guarded IO -- network, filesystem, reset. Only ever runs on MicroPython.
# ==========================================================================
def _auth(token):
    return {"Authorization": "Bearer " + token}


def _get_manifest(base_url, token):
    url = base_url + config.FIRMWARE_MANIFEST_PATH
    resp = urequests.get(url, headers=_auth(token))
    try:
        if resp.status_code != 200:
            raise OTAError("manifest http " + str(resp.status_code))
        return resp.json()
    finally:
        _safe_close(resp)


def _get_file(base_url, token, path):
    # `path` is a manifest key the bridge re-validates; '/' in the query value is
    # legal and the stock bridge accepts it (no traversal: server checks membership).
    url = base_url + config.FIRMWARE_FILE_PATH + "?path=" + path
    resp = urequests.get(url, headers=_auth(token))
    try:
        if resp.status_code != 200:
            raise OTAError("file http " + str(resp.status_code) + " for " + path)
        return resp.content                 # raw bytes
    finally:
        _safe_close(resp)


def _safe_close(resp):
    try:
        resp.close()
    except Exception:
        pass


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _write_bytes(path, data):
    with open(path, "wb") as f:
        f.write(data)


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _ensure_dir(d):
    """mkdir -p for a slash-separated dir (e.g. 'backup/lib')."""
    if not d or d == "/":
        return
    cur = ""
    for part in d.split("/"):
        if not part:
            continue
        cur = (cur + "/" + part) if cur else part
        try:
            os.mkdir(cur)
        except OSError:
            pass                            # already exists (or a leading segment does)


def _ensure_parent(path):
    """Create the parent directory chain for a file path (e.g. lib/ for lib/uQR.py)."""
    if "/" in path:
        _ensure_dir(path.rsplit("/", 1)[0])


def _rmtree(d):
    """Recursively delete a directory tree. Best-effort, never raises."""
    try:
        entries = list(os.ilistdir(d))
    except OSError:
        return
    for entry in entries:
        name = entry[0]
        typ = entry[1]
        full = d + "/" + name
        if typ == 0x4000:                   # directory
            _rmtree(full)
        else:
            _safe_remove(full)
    try:
        os.rmdir(d)
    except OSError:
        pass


def _reset_backup_dir():
    """Start the known-good snapshot from a clean slate (only ONE backup is kept)."""
    _rmtree(config.BACKUP_DIR)
    _ensure_dir(config.BACKUP_DIR)


def _backup(path):
    """Copy a current file into /backup preserving its relative path."""
    if not _exists(path):
        return
    dst = config.BACKUP_DIR + "/" + path
    _ensure_parent(dst)
    with open(path, "rb") as f:
        data = f.read()
    _write_bytes(dst, data)


def _write_local_manifest(manifest):
    # Atomic: write a temp then rename so a power cut mid-write can't corrupt the
    # commit point. This is the LAST thing apply() does -> defines the new version.
    tmp = config.MANIFEST_FILE + ".new"
    with open(tmp, "w") as f:
        f.write(json.dumps(manifest))
    os.rename(tmp, config.MANIFEST_FILE)


def _read_bad_version():
    """A firmware version boot.py quarantined after it crash-looped, or '' if none."""
    try:
        with open(config.BAD_VERSION_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def _clear_bad_version():
    _safe_remove(config.BAD_VERSION_FILE)


def _write_pending_version(version):
    """Record the version of an in-flight apply BEFORE we start mutating live files.
    If power is cut mid-apply (after some renames, before the manifest commit) the
    on-disk manifest still names the OLD/good version -- so boot.py must quarantine
    THIS pending version, not the good one, when it recovers. Best-effort."""
    try:
        with open(config.PENDING_VERSION_FILE, "w") as f:
            f.write(str(version) if version is not None else "")
    except Exception:
        pass


def _clear_pending_version():
    _safe_remove(config.PENDING_VERSION_FILE)


def _cleanup_staged(staged):
    for tmp, _path in staged:
        _safe_remove(tmp)


def _sweep_stale_new(d="."):
    """Recursively delete leftover '<path>.new' staging files from a previously
    interrupted apply so a stale half-download can't shadow a fresh attempt. The
    known-good /backup snapshot is skipped. Best-effort, never raises."""
    try:
        entries = list(os.ilistdir(d))
    except OSError:
        return
    for entry in entries:
        name = entry[0]
        typ = entry[1]
        full = name if d == "." else d + "/" + name
        if typ == 0x4000:                   # directory
            if full == config.BACKUP_DIR:   # never disturb the known-good snapshot
                continue
            _sweep_stale_new(full)
        elif name.endswith(".new"):
            _safe_remove(full)


def apply(base_url, token):
    """Pull + apply the server firmware atomically, then machine.reset().

    Raises OTAError / propagates on any failure WITHOUT having touched live files
    past the point where /backup + the recovery guard can heal it. On success this
    does not return (the board resets into the new firmware).
    """
    if not _HW:
        raise RuntimeError("ota.apply requires MicroPython (urequests/os)")

    # 0. sweep any stale '*.new' staging files a prior interrupted apply left behind
    #    (before we stage fresh ones) so they can't shadow this attempt.
    _sweep_stale_new()

    # 1. server manifest + version.
    server = _get_manifest(base_url, token)
    if not isinstance(server, dict):
        raise OTAError("bad server manifest")
    server_files = server.get("files") or {}
    server_version = server.get("version")

    # Don't reapply a version the recovery guard already quarantined -- that would
    # be an OTA -> crash -> restore -> OTA battery-draining loop. We only retry once
    # the server advertises a DIFFERENT (hopefully fixed) version.
    bad = _read_bad_version()
    if bad and server_version == bad:
        print("ota: version", server_version, "is quarantined (bad) -> skip")
        return

    # 2. plan the delta.
    local = _read_local_manifest()
    local_files = local.get("files") or {}
    to_pull, to_delete = manifest_diff(local_files, server_files)
    print("ota: pull", to_pull, "delete", to_delete)

    if not to_pull and not to_delete:
        # Files already match; only the version label differs -> adopt it and stop.
        # (This also completes an apply whose renames finished but whose manifest
        # commit was interrupted, hence the pending-marker clear.)
        _write_local_manifest(server)
        _clear_bad_version()
        _clear_pending_version()
        return

    # 3. download EVERY changed file to '<path>.new' + verify sha BEFORE anything
    #    live changes. Any failure -> wipe staging, abort, device stays on old fw.
    staged = []
    try:
        for path in to_pull:
            data = _get_file(base_url, token, path)
            if not verify(data, server_files.get(path)):
                raise OTAError("sha mismatch: " + path)
            tmp = path + ".new"
            _ensure_parent(tmp)
            _write_bytes(tmp, data)
            staged.append((tmp, path))
    except Exception:
        _cleanup_staged(staged)
        raise

    # 4. snapshot the to-be-changed CURRENT files (+ the old manifest) as known-good.
    _reset_backup_dir()
    for path in to_pull:
        _backup(path)
    for path in to_delete:
        _backup(path)
    _backup(config.MANIFEST_FILE)           # so a restore reverts the VERSION too

    # 5. apply: atomic rename each .new over the old, then delete removed files.
    #    Mark the apply IN-FLIGHT first: from here until the manifest commit the
    #    on-disk manifest still names the OLD version, so if power is lost mid-rename
    #    boot.py must quarantine THIS pending version, not the good one.
    _write_pending_version(server_version)
    for tmp, path in staged:
        os.rename(tmp, path)                # atomic replace on the same filesystem
    for path in to_delete:
        _safe_remove(path)

    # 6. commit: write the new manifest LAST, clear the quarantine + pending markers.
    _write_local_manifest(server)
    _clear_bad_version()
    _clear_pending_version()
    print("ota: applied version", server_version, "-> reset")

    # 7. reboot into the new firmware.
    if machine is not None:
        machine.reset()
