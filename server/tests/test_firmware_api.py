"""Firmware OTA endpoints: /api/firmware/manifest, /api/firmware/file,
/api/admin/firmware, plus control.fw in /current (and NOT in the ETag)."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server.app import create_app

from .conftest import DEVICE_TOKEN, GHOST_TOKEN, INGEST_TOKEN, RATE_TOKEN, SEED, bearer

ADMIN_TOKEN = "admin-secret-fw"

MANIFEST = "/api/firmware/manifest"
FILE = "/api/firmware/file"
PROVISION = "/api/firmware/provision-file"
ADMIN_FW = "/api/admin/firmware"
CURRENT = "/api/devices/kitchen-01/current"


def _build_fw_dir(root):
    """A tiny but representative firmware tree: 3 code files + excluded ones."""
    (root / "lib").mkdir(parents=True)
    (root / "main.py").write_text("print('main')\n")
    (root / "render.py").write_text("# render\n")
    (root / "lib" / "uQR.py").write_text("# uqr\n")
    (root / "config.py").write_text("DEVICE_ID = 'kitchen-01'\n")        # excluded (device-local)
    (root / "boot.py").write_text("# recovery guard\n")                  # excluded (never OTA'd)
    (root / "secrets.py").write_text("WIFI_SSID = 's'\n")               # excluded (real secret)
    (root / "secrets.example.py").write_text("WIFI_SSID = ''\n")         # excluded; provisioning template
    (root / "test_logic.py").write_text("def test_x(): pass\n")          # excluded
    return root


@pytest.fixture
def fw_ctx(tmp_path):
    fwdir = _build_fw_dir(tmp_path / "firmware")
    app = create_app(
        db_path=str(tmp_path / "fw.db"),
        seed=SEED,
        max_body_bytes=8192,
        admin_token=ADMIN_TOKEN,
        firmware_dir=str(fwdir),
    )
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, fwdir=fwdir, manifest=app.state.firmware)


def admin_bearer(token: str = ADMIN_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- /api/firmware/manifest ----------------------------------------------------


def test_manifest_with_device_token(fw_ctx):
    r = fw_ctx.client.get(MANIFEST, headers=bearer(DEVICE_TOKEN))
    assert r.status_code == 200
    body = r.json()
    assert len(body["version"]) == 12
    assert set(body["files"]) == {"main.py", "render.py", "lib/uQR.py"}
    assert body["version"] == fw_ctx.manifest.version


def test_manifest_with_admin_token(fw_ctx):
    r = fw_ctx.client.get(MANIFEST, headers=admin_bearer())
    assert r.status_code == 200
    assert r.json()["version"] == fw_ctx.manifest.version


def test_manifest_any_device_token_ok(fw_ctx):
    # The contract: ANY valid device token (not scoped to a specific device).
    r = fw_ctx.client.get(MANIFEST, headers=bearer(GHOST_TOKEN))
    assert r.status_code == 200


def test_manifest_no_token_401(fw_ctx):
    assert fw_ctx.client.get(MANIFEST).status_code == 401


def test_manifest_bad_token_401(fw_ctx):
    assert fw_ctx.client.get(MANIFEST, headers=bearer("nope")).status_code == 401


def test_manifest_ingest_token_403(fw_ctx):
    # A valid token of the wrong kind is a scope failure, not unknown.
    assert fw_ctx.client.get(MANIFEST, headers=bearer(INGEST_TOKEN)).status_code == 403


# --- /api/firmware/file --------------------------------------------------------


def test_file_returns_raw_bytes_matching_manifest(fw_ctx):
    r = fw_ctx.client.get(FILE, headers=bearer(DEVICE_TOKEN), params={"path": "main.py"})
    assert r.status_code == 200
    assert r.content == b"print('main')\n"
    # bytes hash to exactly the sha advertised in the manifest (OTA verify path).
    assert hashlib.sha256(r.content).hexdigest() == fw_ctx.manifest.files["main.py"]


def test_file_nested_path(fw_ctx):
    r = fw_ctx.client.get(FILE, headers=bearer(DEVICE_TOKEN), params={"path": "lib/uQR.py"})
    assert r.status_code == 200
    assert r.content == b"# uqr\n"


def test_file_admin_token_ok(fw_ctx):
    r = fw_ctx.client.get(FILE, headers=admin_bearer(), params={"path": "render.py"})
    assert r.status_code == 200


def test_file_no_token_401(fw_ctx):
    assert fw_ctx.client.get(FILE, params={"path": "main.py"}).status_code == 401


def test_file_unknown_path_404(fw_ctx):
    r = fw_ctx.client.get(FILE, headers=bearer(DEVICE_TOKEN), params={"path": "nope.py"})
    assert r.status_code == 404


def test_file_excluded_path_404(fw_ctx):
    # config.py / boot.py / secrets.py exist on disk but are NOT manifest keys.
    # boot.py is the recovery guard: OTA must never serve/replace it -> 404.
    for p in ("config.py", "boot.py", "secrets.py", "secrets.example.py", "test_logic.py"):
        r = fw_ctx.client.get(FILE, headers=bearer(DEVICE_TOKEN), params={"path": p})
        assert r.status_code == 404, p


@pytest.mark.parametrize(
    "bad",
    ["../secrets.py", "../../etc/passwd", "/etc/passwd", "lib/../config.py", "..%2fsecrets.py"],
)
def test_file_traversal_blocked(fw_ctx, bad):
    r = fw_ctx.client.get(FILE, headers=bearer(DEVICE_TOKEN), params={"path": bad})
    assert r.status_code in (400, 404)


def test_file_missing_path_400(fw_ctx):
    r = fw_ctx.client.get(FILE, headers=bearer(DEVICE_TOKEN))
    assert r.status_code == 400


# --- per-token rate limit on the OTA GETs (device token; admin exempt) ---------


def test_manifest_rate_limited_per_device_token(fw_ctx):
    # RATE_TOKEN is a device token with rate_per_min=1 -> the second GET is 429.
    assert fw_ctx.client.get(MANIFEST, headers=bearer(RATE_TOKEN)).status_code == 200
    assert fw_ctx.client.get(MANIFEST, headers=bearer(RATE_TOKEN)).status_code == 429


def test_file_rate_limited_per_device_token(fw_ctx):
    assert fw_ctx.client.get(
        FILE, headers=bearer(RATE_TOKEN), params={"path": "main.py"}
    ).status_code == 200
    assert fw_ctx.client.get(
        FILE, headers=bearer(RATE_TOKEN), params={"path": "main.py"}
    ).status_code == 429


def test_admin_token_not_rate_limited_on_manifest(fw_ctx):
    # The admin token carries no token bucket -> never throttled on the OTA GETs.
    for _ in range(5):
        assert fw_ctx.client.get(MANIFEST, headers=admin_bearer()).status_code == 200


# --- /api/firmware/provision-file (config template, SEPARATE from OTA) ----------


def test_provision_config_with_device_token(fw_ctx):
    r = fw_ctx.client.get(PROVISION, headers=bearer(DEVICE_TOKEN), params={"path": "config.py"})
    assert r.status_code == 200
    assert r.content == b"DEVICE_ID = 'kitchen-01'\n"


def test_provision_secrets_example_ok(fw_ctx):
    r = fw_ctx.client.get(
        PROVISION, headers=bearer(DEVICE_TOKEN), params={"path": "secrets.example.py"}
    )
    assert r.status_code == 200
    assert r.content == b"WIFI_SSID = ''\n"


def test_provision_admin_token_ok(fw_ctx):
    r = fw_ctx.client.get(PROVISION, headers=admin_bearer(), params={"path": "config.py"})
    assert r.status_code == 200


def test_provision_no_token_401(fw_ctx):
    assert fw_ctx.client.get(PROVISION, params={"path": "config.py"}).status_code == 401


def test_provision_ingest_token_403(fw_ctx):
    r = fw_ctx.client.get(PROVISION, headers=bearer(INGEST_TOKEN), params={"path": "config.py"})
    assert r.status_code == 403


def test_provision_real_secrets_never_served_404(fw_ctx):
    # The REAL secrets.py is NOT on the allowlist -> 404 even though it's on disk.
    r = fw_ctx.client.get(PROVISION, headers=bearer(DEVICE_TOKEN), params={"path": "secrets.py"})
    assert r.status_code == 404


def test_provision_manifest_key_not_allowlisted_404(fw_ctx):
    # A real OTA file (manifest key) is NOT a provisioning template -> 404.
    r = fw_ctx.client.get(PROVISION, headers=bearer(DEVICE_TOKEN), params={"path": "main.py"})
    assert r.status_code == 404


def test_provision_missing_path_404(fw_ctx):
    assert fw_ctx.client.get(PROVISION, headers=bearer(DEVICE_TOKEN)).status_code == 404


@pytest.mark.parametrize(
    "bad",
    ["../secrets.py", "../../etc/passwd", "/etc/passwd", "lib/../config.py", "config.py "],
)
def test_provision_traversal_blocked(fw_ctx, bad):
    r = fw_ctx.client.get(PROVISION, headers=bearer(DEVICE_TOKEN), params={"path": bad})
    assert r.status_code == 404, bad


# --- /api/admin/firmware -------------------------------------------------------


def test_admin_firmware_version(fw_ctx):
    r = fw_ctx.client.get(ADMIN_FW, headers=admin_bearer())
    assert r.status_code == 200
    assert r.json() == {"version": fw_ctx.manifest.version}


def test_admin_firmware_no_token_401(fw_ctx):
    assert fw_ctx.client.get(ADMIN_FW).status_code == 401


def test_admin_firmware_device_token_401(fw_ctx):
    # admin endpoint: a device token is NOT the admin token.
    assert fw_ctx.client.get(ADMIN_FW, headers=bearer(DEVICE_TOKEN)).status_code == 401


def test_admin_firmware_disabled_503(tmp_path):
    fwdir = _build_fw_dir(tmp_path / "firmware")
    app = create_app(
        db_path=str(tmp_path / "fw.db"), seed=SEED, admin_token=None, firmware_dir=str(fwdir)
    )
    with TestClient(app) as client:
        assert client.get(ADMIN_FW, headers=admin_bearer()).status_code == 503


# --- control.fw in /current, NOT in the ETag -----------------------------------


def test_current_control_fw_matches_manifest(fw_ctx):
    body = fw_ctx.client.get(CURRENT, headers=bearer(DEVICE_TOKEN)).json()
    assert body["control"]["fw"] == fw_ctx.manifest.version
    # and matches what /api/firmware/manifest advertises
    mver = fw_ctx.client.get(MANIFEST, headers=bearer(DEVICE_TOKEN)).json()["version"]
    assert body["control"]["fw"] == mver


def test_fw_not_in_etag(tmp_path):
    # Two apps, SAME device/screen, DIFFERENT firmware bundles. The fw version
    # differs but is excluded from the ETag hash, so /current's ETag is identical.
    dir_a = _build_fw_dir(tmp_path / "fw_a")
    dir_b = _build_fw_dir(tmp_path / "fw_b")
    (dir_b / "main.py").write_text("print('DIFFERENT firmware')\n")  # bump version

    app_a = create_app(db_path=str(tmp_path / "a.db"), seed=SEED, firmware_dir=str(dir_a))
    app_b = create_app(db_path=str(tmp_path / "b.db"), seed=SEED, firmware_dir=str(dir_b))
    with TestClient(app_a) as ca, TestClient(app_b) as cb:
        ra = ca.get(CURRENT, headers=bearer(DEVICE_TOKEN))
        rb = cb.get(CURRENT, headers=bearer(DEVICE_TOKEN))

    fw_a = ra.json()["control"]["fw"]
    fw_b = rb.json()["control"]["fw"]
    assert fw_a != fw_b                                   # firmware really differs
    assert ra.headers["etag"] == rb.headers["etag"]       # but the ETag does NOT churn
