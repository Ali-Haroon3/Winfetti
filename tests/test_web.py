"""The web frontends: page serving, static assets, and the public config
the player app builds its wheel from."""

from app.payouts import CHECKIN_REWARDS, GAME_PAYOUTS


def test_player_page_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Winfetti" in resp.text
    assert "/static/app.js" in resp.text


def test_console_page_served(client):
    resp = client.get("/console")
    assert resp.status_code == 200
    assert "Console · Winfetti" in resp.text
    assert "/static/console.js" in resp.text


def test_static_assets_served(client):
    for path in (
        "/static/theme.css",
        "/static/app.js",
        "/static/console.js",
        "/static/fonts/inter-var.woff2",
        "/favicon.svg",
    ):
        assert client.get(path).status_code == 200, path


def test_client_config_matches_payout_tables(client):
    body = client.get("/v1/config").json()
    assert body["games"] == GAME_PAYOUTS
    assert body["checkin_rewards"] == CHECKIN_REWARDS
    assert body["coins_per_usd"] > 0


def test_api_routes_not_shadowed_by_pages(client):
    # the pages are explicit routes, so the API namespaces must be intact
    assert client.get("/admin/redemptions").status_code == 403  # needs key
    assert client.get("/healthz").json() == {"ok": True}
