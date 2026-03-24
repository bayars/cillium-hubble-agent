"""
End-to-end tests against a live network-monitor deployment.

Requires: API running at E2E_API_URL (default http://localhost:8000)
"""

import os
import pytest
import httpx

API_URL = os.getenv("E2E_API_URL", "http://localhost:8000")


@pytest.fixture(scope="module")
def api():
    client = httpx.Client(base_url=API_URL, timeout=10)
    # Seed topology for tests
    client.post("/api/topology/nodes", json={"id": "e2e-r1", "label": "R1", "type": "router"})
    client.post("/api/topology/nodes", json={"id": "e2e-r2", "label": "R2", "type": "router"})
    client.post("/api/topology/links", json={
        "id": "e2e-link1",
        "source": "e2e-r1",
        "target": "e2e-r2",
        "source_interface": "eth0",
        "target_interface": "eth0",
        "state": "idle",
    })
    yield client
    # Cleanup
    client.delete("/api/topology/links/e2e-link1")
    client.delete("/api/topology/nodes/e2e-r1")
    client.delete("/api/topology/nodes/e2e-r2")
    client.close()


def test_health(api):
    r = api.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "healthy"
    assert "uptime_seconds" in data


def test_topology(api):
    r = api.get("/api/topology")
    assert r.status_code == 200
    data = r.json()
    assert "nodes" in data
    assert "edges" in data
    assert len(data["nodes"]) > 0
    assert len(data["edges"]) > 0


def test_links_list(api):
    r = api.get("/api/links")
    assert r.status_code == 200
    data = r.json()
    assert "links" in data
    assert "count" in data
    assert data["count"] > 0
    for link in data["links"]:
        assert "id" in link
        assert "state" in link
        assert "metrics" in link
        assert "data_source" in link["metrics"]


def test_get_single_link(api):
    r = api.get("/api/links/e2e-link1")
    assert r.status_code == 200
    assert r.json()["id"] == "e2e-link1"


def test_get_link_metrics(api):
    r = api.get("/api/links/e2e-link1/metrics")
    assert r.status_code == 200
    data = r.json()
    assert "data_source" in data


def test_update_link_metrics(api):
    metrics = {
        "rx_bps": 1000000,
        "tx_bps": 500000,
        "data_source": "external",
    }
    r = api.put("/api/links/e2e-link1/metrics", json=metrics)
    assert r.status_code == 200
    data = r.json()
    assert data["metrics"]["rx_bps"] == 1000000
    assert data["metrics"]["data_source"] == "external"

    # Verify persistence
    r2 = api.get("/api/links/e2e-link1/metrics")
    assert r2.json()["data_source"] == "external"


def test_update_link_state(api):
    r = api.put("/api/links/e2e-link1/state", params={"state": "active"})
    assert r.status_code == 200


def test_links_filter_by_state(api):
    r = api.get("/api/links", params={"state": "active"})
    assert r.status_code == 200
    for link in r.json()["links"]:
        assert link["state"] == "active"


def test_nonexistent_link_404(api):
    r = api.get("/api/links/does-not-exist-xyz")
    assert r.status_code == 404


def test_event_submission(api):
    event = {
        "interface": "eth0",
        "old_state": "idle",
        "new_state": "active",
        "source": "e2e-test",
    }
    r = api.post("/api/events", json=event)
    assert r.status_code in (200, 201, 204)


def test_event_history(api):
    r = api.get("/api/events/history")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list) or "events" in data


def test_openapi_docs(api):
    r = api.get("/docs")
    assert r.status_code == 200

    r2 = api.get("/openapi.json")
    assert r2.status_code == 200
    assert "paths" in r2.json()
