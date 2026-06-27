# papertrail boot-time recovery guard (MicroPython, Pico W).
#
# MicroPython runs boot.py FIRST at every boot, then main.py. We hijack that so a
# bad OTA can NEVER permanently brick the device:
#
#   1. Increment a crash-loop counter in flash (boot_count.txt).
#   2. If the counter has exceeded BOOT_MAX_ATTEMPTS (i.e. that many boots in a row
#      without main reaching one fully-successful cycle), the freshly-applied
#      firmware is presumed bad: quarantine its version, RESTORE the known-good
#      files from /backup over the current ones, zero the counter, and reset.
#   3. Otherwise launch main(). main() zeroes the counter after ONE clean cycle, so
#      in steady state the counter just oscillates 0<->1 and never trips the guard.
#
# If main() throws instead of looping forever, we reset (rather than dropping a
# headless, battery-powered device to a dead REPL) so the counter advances and the
# guard eventually heals. A short delay before the reset leaves a Ctrl-C window for
# a developer at the REPL (KeyboardInterrupt is BaseException, so it escapes here).
#
# boot.py deliberately depends ONLY on `config` (device-local, NEVER OTA'd) + the
# stdlib, and inlines the crash-loop test (mirrored as ota.should_restore for host
# tests). It must not need any OTA-updatable module to perform a recovery.

try:
    import machine
    _HW = True
except ImportError:                         # host (py_compile / import) -- no auto-run
    machine = None
    _HW = False

try:
    import utime as time
except ImportError:
    import time

import os
import json
import config


def _read_count():
    try:
        with open(config.BOOT_COUNT_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def _write_count(n):
    try:
        with open(config.BOOT_COUNT_FILE, "w") as f:
            f.write(str(int(n)))
    except Exception:
        pass


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _ensure_parent(path):
    if "/" not in path:
        return
    parent = path.rsplit("/", 1)[0]
    cur = ""
    for part in parent.split("/"):
        if not part:
            continue
        cur = (cur + "/" + part) if cur else part
        try:
            os.mkdir(cur)
        except OSError:
            pass


def _read_pending_version():
    """The version of an apply that was IN FLIGHT (set by ota.apply before its rename
    loop, cleared after commit), or '' if none. Present only when an apply was
    interrupted before it committed -- in which case the on-disk manifest still names
    the OLD/good version, so the pending one is the true suspect."""
    try:
        with open(config.PENDING_VERSION_FILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def _record_bad_version():
    """Quarantine the suspected-bad version so ota.apply() won't immediately re-pull
    and re-crash on it: the PENDING version of an interrupted apply if present (so an
    interrupt before commit does NOT blame the still-current good version), otherwise
    the current manifest version. Best-effort; never raises."""
    try:
        v = _read_pending_version()
        if not v:
            with open(config.MANIFEST_FILE) as f:
                v = json.loads(f.read()).get("version")
        if v:
            with open(config.BAD_VERSION_FILE, "w") as f:
                f.write(v)
            print("boot: quarantined bad version", v)
    except Exception as e:
        print("boot: could not record bad version:", e)


def _restore_tree(backup_dir, rel=""):
    """Copy every file under /backup back over its live path. Best-effort."""
    src_dir = backup_dir + "/" + rel if rel else backup_dir
    try:
        entries = list(os.ilistdir(src_dir))
    except OSError:
        return
    for entry in entries:
        name = entry[0]
        typ = entry[1]
        child_rel = rel + "/" + name if rel else name
        if typ == 0x4000:                   # directory -> recurse
            _restore_tree(backup_dir, child_rel)
        else:
            try:
                with open(backup_dir + "/" + child_rel, "rb") as f:
                    data = f.read()
                _ensure_parent(child_rel)
                with open(child_rel, "wb") as f:
                    f.write(data)
                print("boot: restored", child_rel)
            except Exception as e:
                print("boot: restore failed for", child_rel, e)


def _has_restorable_backup(d):
    """True iff /backup holds at least one restorable file (recursively). An empty
    or absent backup tree means a restore would be a no-op -> resetting would just
    crash-loop again."""
    try:
        entries = list(os.ilistdir(d))
    except OSError:
        return False
    for entry in entries:
        name = entry[0]
        typ = entry[1]
        if typ == 0x4000:                   # directory -> recurse
            if _has_restorable_backup(d + "/" + name):
                return True
        else:
            return True                     # a real file -> something to restore
    return False


def run():
    count = _read_count() + 1
    _write_count(count)
    print("boot: attempt", count)

    # Crash-loop guard. Mirrors ota.should_restore(count, BOOT_MAX_ATTEMPTS).
    if count > config.BOOT_MAX_ATTEMPTS:
        _record_bad_version()

        # No usable /backup -> a restore + reset would just thrash a headless,
        # battery-powered board forever. Idle in a long deepsleep instead (preserve
        # battery; the counter is left high so each wake re-idles until a re-flash /
        # power-cycle heals it).
        if not _has_restorable_backup(config.BACKUP_DIR):
            print("boot: CRASH-LOOP (%d), no usable /backup -> long deepsleep idle"
                  % count)
            if _HW:
                machine.deepsleep(int(config.RECOVERY_IDLE_S) * 1000)
            return

        print("boot: CRASH-LOOP (%d) -> restoring /backup" % count)
        try:
            _restore_tree(config.BACKUP_DIR)
        except Exception as e:
            print("boot: restore error:", e)
        _write_count(0)
        if _HW:
            time.sleep(1)
            machine.reset()
        return

    # Launch the application. main() blocks forever (its own while True) on real
    # hardware; if it raises, reset so the counter climbs and the guard can heal.
    try:
        import main
        main.main()
    except Exception as e:
        print("boot: main crashed:", e)
        if _HW:
            time.sleep(2)                   # Ctrl-C window for a dev at the REPL
            machine.reset()


# Auto-run on device only. On a host this module just imports (for py_compile);
# run() is never invoked because main()/machine are hardware-only.
if _HW:
    run()
