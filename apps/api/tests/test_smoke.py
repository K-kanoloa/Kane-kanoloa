from __future__ import annotations

import tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.main import app
from app.security.url_safety import is_safe_http_url
from app.store.file_store import FileStore


def test_api_imports_and_creates_app():
    assert app.title == "Kane Agent Platform API"
    assert app.version == "2.0.0"


def test_health_endpoint_responds_honestly():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "kane-agent-platform-api"
    assert body["version"] == "2.0.0"
    assert "startup" in body

    # Confirm no legacy task/run/watchdog/diagnostics fields
    assert "tasks_total" not in body
    assert "runs_total" not in body
    assert "local_bridge_reachable" not in body
    assert "waiting_handoffs" not in body
    assert "diagnostics_url" not in body


def test_auth_middleware_exempts_health(monkeypatch):
    monkeypatch.setenv("OCTOPUS_API_TOKEN", "secret-test-token")
    client = TestClient(app)

    # Health remains exempt
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_url_safety_utility():
    assert is_safe_http_url("https://example.com/api") is True
    assert is_safe_http_url("http://127.0.0.1:8000") is False
    assert is_safe_http_url("http://localhost:3000") is False
    assert is_safe_http_url("http://metadata.google.internal") is False
    assert is_safe_http_url("ftp://example.com") is False


class DummyItem(BaseModel):
    item_id: str
    name: str


def test_file_store_generic_infrastructure():
    with tempfile.TemporaryDirectory(prefix="kane-store-test-") as tmpdir:
        store_path = Path(tmpdir) / "test_items.json"
        store = FileStore(path=store_path, model=DummyItem, id_field="item_id")

        items = store.list()
        assert items == []

        item = DummyItem(item_id="item_1", name="first")
        store.upsert(item)

        loaded = store.get("item_1")
        assert loaded is not None
        assert loaded.name == "first"
        assert len(store.list()) == 1
