"""Fraud dashboard endpoints and user moderation."""

import uuid

from tests.conftest import ADMIN_HEADERS, auth_headers, make_user


def _trip_denials(client, n=2):
    headers, user_id = auth_headers(client)
    for i in range(n):
        client.post(
            "/v1/game/claim",
            json={"game": "nope", "event": "nope", "idem_key": f"fraud-{uuid.uuid4()}"},
            headers=headers,
        )
    return headers, user_id


def test_fraud_events_list_and_filter(client, db):
    _, user_id = _trip_denials(client)
    events = client.get("/admin/fraud/events", headers=ADMIN_HEADERS).json()
    assert len(events) == 2
    assert events[0]["kind"] == "game_claim_denied:unknown_game_event"
    assert events[0]["user_id"] == str(user_id)

    filtered = client.get(
        f"/admin/fraud/events?user_id={user_id}&kind=game_claim_denied:unknown_game_event",
        headers=ADMIN_HEADERS,
    ).json()
    assert len(filtered) == 2
    none = client.get(
        "/admin/fraud/events?kind=does_not_exist", headers=ADMIN_HEADERS
    ).json()
    assert none == []


def test_fraud_summary_counts(client, db):
    _trip_denials(client, n=3)
    user = make_user(db, risk_score=5)
    summary = client.get("/admin/fraud/summary?hours=24", headers=ADMIN_HEADERS).json()
    assert summary["events_by_kind"]["game_claim_denied:unknown_game_event"] == 3
    assert summary["top_risk_users"][0]["user_id"] == str(user.id)
    assert summary["top_risk_users"][0]["risk_score"] == 5
    assert summary["top_denied_ips"][0]["denials"] == 3


def test_user_detail(client, db):
    headers, user_id = auth_headers(client)
    client.post("/v1/checkin", headers=headers)
    detail = client.get(f"/admin/users/{user_id}", headers=ADMIN_HEADERS).json()
    assert detail["balance"] == 100
    assert detail["total_credited"] == 100
    assert detail["checkin_streak"] == 1
    assert detail["redemptions_by_status"] == {}
    assert (
        client.get(f"/admin/users/{uuid.uuid4()}", headers=ADMIN_HEADERS).status_code
        == 404
    )


def test_ban_and_unban(client, db):
    headers, user_id = auth_headers(client)
    resp = client.post(
        f"/admin/users/{user_id}/status",
        json={"status": "banned"},
        headers=ADMIN_HEADERS,
    )
    assert resp.json()["status"] == "banned"
    assert client.get("/v1/me", headers=headers).status_code == 403

    client.post(
        f"/admin/users/{user_id}/status",
        json={"status": "active"},
        headers=ADMIN_HEADERS,
    )
    assert client.get("/v1/me", headers=headers).status_code == 200

    events = client.get(
        "/admin/fraud/events?kind=admin_status_change", headers=ADMIN_HEADERS
    ).json()
    assert len(events) == 2
    assert (
        client.post(
            f"/admin/users/{user_id}/status",
            json={"status": "godmode"},
            headers=ADMIN_HEADERS,
        ).status_code
        == 422
    )


def test_dashboard_requires_admin_key(client, db):
    assert client.get("/admin/fraud/summary").status_code == 403
    assert client.get("/admin/fraud/events").status_code == 403
