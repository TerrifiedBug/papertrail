"""Unit tests for the firmware manifest hasher (pure / host-testable).

Covers the include/exclude rule, a stable + content-sensitive version, and the
manifest path resolver's traversal guard.
"""

from __future__ import annotations

from server.firmware_manifest import (
    build_manifest,
    compute_version,
    is_firmware_file,
)


# --- include / exclude rule ----------------------------------------------------


def test_include_code_files():
    for p in (
        "main.py",
        "ota.py",
        "render.py",
        "poller.py",
        "wifi.py",
        "ina219.py",
        "epaper2in13.py",
        "epaper2in13b.py",
        "qr.py",
        "lib/uQR.py",
    ):
        assert is_firmware_file(p), p


def test_exclude_device_local_and_non_code():
    for p in (
        "config.py",            # DEVICE-LOCAL: never OTA'd
        "boot.py",              # recovery guard: never served/replaced by OTA
        "secrets.py",
        "secrets.example.py",
        "test_logic.py",        # host tests
        "test_offset.py",
        "lib/test_x.py",        # test_ prefix anywhere
        "README.md",
        ".gitignore",
        "pico_sdk_import.cmake",
        "notes.txt",
        "__pycache__/main.cpython-311.pyc",
        "lib/__pycache__/uQR.cpython-311.pyc",
    ):
        assert not is_firmware_file(p), p


# --- build_manifest over a temp tree -------------------------------------------


def _write_tree(root):
    (root / "lib").mkdir(parents=True)
    (root / "main.py").write_text("print('main')\n")
    (root / "render.py").write_text("# render\n")
    (root / "lib" / "uQR.py").write_text("# uqr\n")
    # excluded:
    (root / "config.py").write_text("DEVICE_ID = 'kitchen-01'\n")
    (root / "secrets.py").write_text("WIFI_SSID = 's'\n")
    (root / "secrets.example.py").write_text("WIFI_SSID = ''\n")
    (root / "test_logic.py").write_text("def test_x(): pass\n")
    (root / "boot_count.txt").write_text("0\n")
    (root / "README.md").write_text("# fw\n")


def test_manifest_includes_only_code(tmp_path):
    _write_tree(tmp_path)
    m = build_manifest(str(tmp_path))
    assert set(m.files) == {"main.py", "render.py", "lib/uQR.py"}
    # every value is a 64-hex sha256
    assert all(len(h) == 64 and all(c in "0123456789abcdef" for c in h) for h in m.files.values())


def test_version_is_12_hex_and_stable(tmp_path):
    _write_tree(tmp_path)
    a = build_manifest(str(tmp_path))
    b = build_manifest(str(tmp_path))
    assert len(a.version) == 12
    assert all(c in "0123456789abcdef" for c in a.version)
    assert a.version == b.version          # same bytes -> same version


def test_version_changes_when_a_file_changes(tmp_path):
    _write_tree(tmp_path)
    before = build_manifest(str(tmp_path)).version
    (tmp_path / "main.py").write_text("print('changed')\n")
    after = build_manifest(str(tmp_path)).version
    assert before != after


def test_version_unchanged_when_only_excluded_file_changes(tmp_path):
    _write_tree(tmp_path)
    before = build_manifest(str(tmp_path)).version
    # config.py is device-local + excluded: touching it must NOT move the version.
    (tmp_path / "config.py").write_text("DEVICE_ID = 'living-room-09'\n")
    after = build_manifest(str(tmp_path)).version
    assert before == after


def test_compute_version_deterministic_regardless_of_insertion_order():
    a = compute_version({"a.py": "11", "b.py": "22"})
    b = compute_version({"b.py": "22", "a.py": "11"})
    assert a == b


def test_missing_dir_yields_empty_but_valid_manifest(tmp_path):
    m = build_manifest(str(tmp_path / "does-not-exist"))
    assert m.files == {}
    assert m.version == compute_version({})
    assert len(m.version) == 12


# --- abspath traversal guard ---------------------------------------------------


def test_abspath_resolves_manifest_key(tmp_path):
    _write_tree(tmp_path)
    m = build_manifest(str(tmp_path))
    full = m.abspath("main.py")
    assert full is not None and full.endswith("/main.py")
    assert m.abspath("lib/uQR.py").endswith("/lib/uQR.py")


def test_abspath_rejects_non_keys_and_traversal(tmp_path):
    _write_tree(tmp_path)
    m = build_manifest(str(tmp_path))
    for bad in ("config.py", "secrets.py", "../secrets.py", "/etc/passwd", "lib/../config.py", ""):
        assert m.abspath(bad) is None, bad
