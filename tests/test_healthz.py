import pytest


@pytest.mark.django_db
def test_healthz_returns_ok(client):
    resp = client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert resp["Content-Type"] == "application/json"
