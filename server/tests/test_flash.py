"""The /flash web-serial provisioner page is served without auth."""


def test_flash_page_served(ctx):
    r = ctx.client.get("/flash")
    assert r.status_code == 200
    assert "web serial" in r.text.lower()


def test_root_redirects_to_admin(ctx):
    # LAN-internal convenience: the bare root 307s to the dashboard.
    r = ctx.client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/admin"
