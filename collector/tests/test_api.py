import json
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from api import AppState, create_app
from config import (
    AppConfig,
    ReadingsStore,
    TagConfig,
    load_config,
    save_config,
)


@pytest.fixture
def app_state(tmp_path):
    config = AppConfig(
        tags=[TagConfig(mac="AA:BB:CC:DD:EE:FF", name="Room A")],
    )
    config_path = str(tmp_path / "config.yaml")
    save_config(config, config_path)
    return AppState(
        config=config,
        readings=ReadingsStore(),
        watcher=None,
        config_path=config_path,
        start_time=time.time(),
        dashboard_path=str(tmp_path / "ruuvi.json"),
    )


@pytest.fixture
def client(app_state):
    app = create_app(app_state)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestHealthEndpoint:
    async def test_health_returns_200(self, client, app_state):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "uptime_seconds" in data
        assert data["tag_count"] == 1
        assert data["enabled_tag_count"] == 1


from conftest import make_reading


class TestGetTags:
    async def test_returns_configured_tags(self, client):
        resp = await client.get("/api/tags")
        assert resp.status_code == 200
        tags = resp.json()
        assert len(tags) == 1
        assert tags[0]["mac"] == "AA:BB:CC:DD:EE:FF"
        assert tags[0]["name"] == "Room A"

    async def test_includes_live_reading(self, client, app_state):
        await app_state.readings.update("AA:BB:CC:DD:EE:FF", make_reading(), rssi=-65)
        resp = await client.get("/api/tags")
        tag = resp.json()[0]
        assert tag["reading"]["temperature"] == 22.5
        assert tag["rssi"] == -65
        assert tag["last_seen"] is not None

    async def test_tag_without_reading_has_nulls(self, client):
        resp = await client.get("/api/tags")
        tag = resp.json()[0]
        assert tag["reading"] is None
        assert tag["rssi"] is None


class TestGetDiscovered:
    async def test_returns_unconfigured_tags(self, client, app_state):
        await app_state.readings.update(
            "11:22:33:44:55:66", make_reading(mac="11:22:33:44:55:66"), rssi=-70
        )
        resp = await client.get("/api/discovered")
        assert resp.status_code == 200
        tags = resp.json()
        assert len(tags) == 1
        assert tags[0]["mac"] == "11:22:33:44:55:66"

    async def test_excludes_configured_tags(self, client, app_state):
        await app_state.readings.update("AA:BB:CC:DD:EE:FF", make_reading(), rssi=-65)
        resp = await client.get("/api/discovered")
        assert len(resp.json()) == 0

    async def test_empty_when_no_discoveries(self, client):
        resp = await client.get("/api/discovered")
        assert resp.json() == []


class TestPostTag:
    async def test_add_tag(self, client, app_state):
        resp = await client.post(
            "/api/tags", json={"mac": "11:22:33:44:55:66", "name": "Kitchen"}
        )
        assert resp.status_code == 201
        assert any(t.mac == "11:22:33:44:55:66" for t in app_state.config.tags)

    async def test_add_tag_persists_to_yaml(self, client, app_state):
        await client.post(
            "/api/tags", json={"mac": "11:22:33:44:55:66", "name": "Kitchen"}
        )
        reloaded = load_config(app_state.config_path)
        assert any(t.mac == "11:22:33:44:55:66" for t in reloaded.tags)

    async def test_add_tag_mac_uppercased(self, client):
        resp = await client.post(
            "/api/tags", json={"mac": "aa:bb:cc:11:22:33", "name": "Test"}
        )
        assert resp.json()["mac"] == "AA:BB:CC:11:22:33"

    async def test_add_duplicate_returns_409(self, client):
        resp = await client.post(
            "/api/tags", json={"mac": "AA:BB:CC:DD:EE:FF", "name": "Dup"}
        )
        assert resp.status_code == 409

    async def test_add_tag_missing_name_returns_422(self, client):
        resp = await client.post("/api/tags", json={"mac": "11:22:33:44:55:66"})
        assert resp.status_code == 422


class TestPatchTag:
    async def test_rename(self, client, app_state):
        resp = await client.patch(
            "/api/tags/AA:BB:CC:DD:EE:FF", json={"name": "New Name"}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    async def test_disable(self, client, app_state):
        resp = await client.patch(
            "/api/tags/AA:BB:CC:DD:EE:FF", json={"enabled": False}
        )
        assert resp.json()["enabled"] is False

    async def test_persists_to_yaml(self, client, app_state):
        await client.patch("/api/tags/AA:BB:CC:DD:EE:FF", json={"name": "Updated"})
        reloaded = load_config(app_state.config_path)
        assert reloaded.tags[0].name == "Updated"

    async def test_not_found_returns_404(self, client):
        resp = await client.patch("/api/tags/FF:FF:FF:FF:FF:FF", json={"name": "X"})
        assert resp.status_code == 404

    async def test_case_insensitive_mac(self, client):
        resp = await client.patch("/api/tags/aa:bb:cc:dd:ee:ff", json={"name": "X"})
        assert resp.status_code == 200


class TestDeleteTag:
    async def test_delete(self, client, app_state):
        resp = await client.delete("/api/tags/AA:BB:CC:DD:EE:FF")
        assert resp.status_code == 204
        assert len(app_state.config.tags) == 0

    async def test_persists_to_yaml(self, client, app_state):
        await client.delete("/api/tags/AA:BB:CC:DD:EE:FF")
        reloaded = load_config(app_state.config_path)
        assert len(reloaded.tags) == 0

    async def test_not_found_returns_404(self, client):
        resp = await client.delete("/api/tags/FF:FF:FF:FF:FF:FF")
        assert resp.status_code == 404


class TestSettings:
    async def test_get_settings(self, client):
        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert "min_write_interval_seconds" in data
        assert "victoriametrics_url" in data
        assert data["columns_per_row"] == 6

    async def test_update_min_interval(self, client, app_state):
        resp = await client.patch(
            "/api/settings", json={"min_write_interval_seconds": 30}
        )
        assert resp.status_code == 200
        assert resp.json()["min_write_interval_seconds"] == 30

    async def test_clear_min_interval(self, client, app_state):
        resp = await client.patch(
            "/api/settings", json={"min_write_interval_seconds": None}
        )
        assert resp.json()["min_write_interval_seconds"] is None

    async def test_update_columns_per_row(self, client, app_state):
        resp = await client.patch("/api/settings", json={"columns_per_row": 4})
        assert resp.status_code == 200
        assert resp.json()["columns_per_row"] == 4
        assert app_state.config.dashboard.columns_per_row == 4

    async def test_update_persists_to_yaml(self, client, app_state):
        await client.patch("/api/settings", json={"min_write_interval_seconds": 60})
        reloaded = load_config(app_state.config_path)
        assert reloaded.collector.min_write_interval_seconds == 60


class TestReorderTags:
    async def test_reorder(self, client, app_state):
        await client.post(
            "/api/tags", json={"mac": "11:22:33:44:55:66", "name": "Room B"}
        )
        resp = await client.put(
            "/api/tags/order", json={"macs": ["11:22:33:44:55:66", "AA:BB:CC:DD:EE:FF"]}
        )
        assert resp.status_code == 200
        tags_resp = await client.get("/api/tags")
        tags = tags_resp.json()
        assert tags[0]["mac"] == "11:22:33:44:55:66"
        assert tags[1]["mac"] == "AA:BB:CC:DD:EE:FF"

    async def test_reorder_missing_mac_returns_400(self, client):
        resp = await client.put("/api/tags/order", json={"macs": ["FF:FF:FF:FF:FF:FF"]})
        assert resp.status_code == 400

    async def test_reorder_regenerates_dashboard(self, client, app_state):
        await client.post(
            "/api/tags", json={"mac": "11:22:33:44:55:66", "name": "Room B"}
        )
        await client.put(
            "/api/tags/order", json={"macs": ["11:22:33:44:55:66", "AA:BB:CC:DD:EE:FF"]}
        )
        d = json.loads(Path(app_state.dashboard_path).read_text())
        temp_stats = [
            p
            for p in d["panels"]
            if p["type"] == "stat" and "temperature" in p["targets"][0]["expr"]
        ]
        assert temp_stats[0]["title"] == "Room B"
        assert temp_stats[1]["title"] == "Room A"


class TestStaticFiles:
    async def test_root_serves_html(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
