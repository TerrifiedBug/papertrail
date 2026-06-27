"""Firmware manifest: hash the bundled Pico firmware at startup for OTA.

The bridge serves a PULL / delta / hash-verified OTA. At startup it walks the
bundled firmware directory, hashes each *code* file, and derives a stable
``version`` (first 12 hex of sha256 over the canonical ``{path: sha}`` map).
Devices poll the manifest, diff it against their last-applied copy, and pull
only the files whose sha changed (delta).

Pure functions where possible so the include/exclude rule + version derivation
are host-testable without touching the filesystem.

Include / exclude (OTA contract):
  INCLUDE  the code: main.py, ota.py, render.py, poller.py, wifi.py,
           ina219.py, epaper2in13.py, epaper2in13b.py, qr.py, lib/uQR.py — i.e.
           every ``*.py`` in the tree, including sub-packages like ``lib/``.
  EXCLUDE  device-local + non-code + the recovery guard: config.py (DEVICE-LOCAL
           — holds DEVICE_ID / pins; OTA'ing it would clobber a device), boot.py
           (the boot-time crash-loop RECOVERY GUARD — must never be served or
           replaced by OTA, or a bad update could disable its own safety net),
           secrets.py / secrets.example.py, test_*.py, and anything non-``.py``
           (runtime ``*.txt``, README, .gitignore, .cmake) plus ``__pycache__``.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Optional

from .resolve import canonical_json

# Names that must NEVER be OTA'd even though they are ``.py``: config.py is
# device-local; boot.py is the recovery guard (OTA must never serve/replace it —
# the device also never-pulls it); secrets* carry wifi/token material. test_*.py
# is matched by the ``test_`` prefix rule below.
_EXCLUDE_NAMES = frozenset(
    {"config.py", "boot.py", "secrets.py", "secrets.example.py"}
)

_READ_CHUNK = 65536


def is_firmware_file(relpath: str) -> bool:
    """True if a firmware-dir-relative path belongs in the OTA bundle.

    ``relpath`` uses forward slashes (POSIX-style) regardless of host OS.
    """
    parts = relpath.split("/")
    name = parts[-1]
    if "__pycache__" in parts:
        return False
    if not name.endswith(".py"):
        return False
    if name in _EXCLUDE_NAMES:
        return False
    if name.startswith("test_"):
        return False
    return True


def compute_version(files: dict[str, str]) -> str:
    """First 12 hex of sha256(canonical_json(files)).

    Deterministic (canonical_json sorts keys), so identical firmware bytes always
    yield the same version, and any added / removed / changed file flips it.
    """
    return hashlib.sha256(canonical_json(files)).hexdigest()[:12]


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_READ_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class FirmwareManifest:
    """The hashed firmware bundle plus the absolute dir it was hashed from."""

    version: str
    files: dict[str, str]   # firmware-dir-relative POSIX path -> sha256 hex
    root: str               # absolute firmware dir the files were hashed from

    def to_dict(self) -> dict[str, object]:
        """The wire shape for GET /api/firmware/manifest."""
        return {"version": self.version, "files": dict(self.files)}

    def has(self, path: str) -> bool:
        return path in self.files

    def abspath(self, path: str) -> Optional[str]:
        """Resolve a manifest path to an absolute file path.

        Returns None if ``path`` is not a manifest key OR (defense in depth) the
        resolved path escapes ``root`` (``..`` / absolute / traversal / symlink).
        Because the caller validates membership first this double-checks the
        filesystem. ``realpath`` resolves symlinks so a symlinked firmware file
        can never point a fetch outside the bundle root.
        """
        if path not in self.files:
            return None
        root = os.path.realpath(self.root)
        full = os.path.realpath(os.path.join(root, path))
        if full != root and not full.startswith(root + os.sep):
            return None
        return full


def default_firmware_dir() -> str:
    """Resolve the firmware dir: ``PAPERTRAIL_FIRMWARE_DIR`` if set, else the
    repo's ``firmware/`` resolved relative to this package (``../firmware``).

    The same ``../firmware`` default lands on ``/app/firmware`` inside the image
    (server/ is COPY'd to /app/server, firmware/ to /app/firmware)."""
    env = os.environ.get("PAPERTRAIL_FIRMWARE_DIR")
    if env:
        return env
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "firmware"))


def build_manifest(firmware_dir: Optional[str] = None) -> FirmwareManifest:
    """Walk ``firmware_dir`` (or the default), hash every OTA file, derive the
    version. A missing dir yields an empty-but-valid manifest (version of ``{}``).

    ``firmware_dir`` takes priority over the env var when explicitly passed; pass
    None to fall back to ``default_firmware_dir()``. ``realpath`` resolves
    symlinks so the stored ``root`` and the per-file containment check (abspath)
    agree on a canonical, symlink-free bundle root."""
    root = os.path.realpath(firmware_dir if firmware_dir is not None else default_firmware_dir())
    files: dict[str, str] = {}
    if os.path.isdir(root):
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune pycache dirs up front (the filter also guards each path).
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if is_firmware_file(rel):
                    files[rel] = _sha256_file(full)
    files = dict(sorted(files.items()))
    return FirmwareManifest(version=compute_version(files), files=files, root=root)
