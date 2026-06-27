"""The /flash web-serial provisioner page is served without auth."""


def test_flash_page_served(ctx):
    r = ctx.client.get("/flash")
    assert r.status_code == 200
    assert "web serial" in r.text.lower()
