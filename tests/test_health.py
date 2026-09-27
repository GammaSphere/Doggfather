from doggfather import __version__, clock


def test_healthz_reports_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__


def test_unknown_api_route_is_json_404(client):
    response = client.get("/api/definitely-not-here")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_clock_round_trips_iso_and_freezes():
    assert clock.normalize("2026-03-01T18:00:00Z") == "2026-03-01T18:00:00Z"
    assert clock.normalize("2026-03-01T19:00:00+01:00") == "2026-03-01T18:00:00Z"
    with clock.frozen("2026-03-01T17:59:59Z"):
        assert clock.now_iso() == "2026-03-01T17:59:59Z"
    assert clock.now_iso() != "2026-03-01T17:59:59Z"
