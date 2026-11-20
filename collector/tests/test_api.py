import json
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import api
import config as config_module
from api import AppState, create_app
from config import (
    AppConfig,
    CollectorConfig,
    ReadingsStore,
    TagConfig,
    load_config,
    save_config,
)
from dashboard import write_dashboard
from writer import MetricsWriter


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
def snapshot(app_state):
    """Return a check that the config on disk and in memory did not change."""
    before_config = app_state.config.model_copy(deep=True)
    before_text = Path(app_state.config_path).read_text()

    def assert_unchanged():
        assert app_state.config == before_config
        assert Path(app_state.config_path).read_text() == before_text
        load_config(app_state.config_path)

    return assert_unchanged


@pytest.fixture
def two_tag_state(app_state):
    config = AppConfig(
        tags=[
            TagConfig(mac="AA:BB:CC:DD:EE:FF", name="Room A"),
            TagConfig(mac="11:22:33:44:55:66", name="Room B"),
        ],
    )
    save_config(config, app_state.config_path)
    app_state.config = config
    return app_state


ADMIN_PASSWORD = "test-admin-password"


@pytest.fixture
def client(app_state):
    app = create_app(app_state, admin_password=ADMIN_PASSWORD)
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport, base_url="http://test", auth=("admin", ADMIN_PASSWORD)
    )


class TestHealthEndpoint:
    async def test_health_returns_200(self, client, app_state):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "uptime_seconds" in data
        assert data["tag_count"] == 1
        assert data["enabled_tag_count"] == 1
        assert data["scanner"] == "unknown"

    async def test_health_reports_scanner_status(self, client, app_state):
        from scanner import RuuviScanner, ScannerStatus

        app_state.scanner = RuuviScanner(
            app_state.config, writer=None, readings=app_state.readings
        )
        resp = await client.get("/api/health")
        assert resp.json()["scanner"] == "starting"

        app_state.scanner.status = ScannerStatus.FAILED
        resp = await client.get("/api/health")
        assert resp.json()["scanner"] == "failed"


from conftest import failing, make_reading


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

    async def test_add_tag_invalid_mac_returns_422(self, client, snapshot):
        resp = await client.post("/api/tags", json={"mac": "invalid", "name": "X"})
        assert resp.status_code == 422
        snapshot()

    @pytest.mark.parametrize("name", ["Room\nB", "Room\x00B", "", "   "])
    async def test_add_tag_invalid_name_returns_422(self, client, snapshot, name):
        resp = await client.post(
            "/api/tags", json={"mac": "11:22:33:44:55:66", "name": name}
        )
        assert resp.status_code == 422
        snapshot()


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

    @pytest.mark.parametrize("body", [{"name": None}, {"enabled": None}])
    async def test_explicit_null_returns_422(self, client, snapshot, body):
        resp = await client.patch("/api/tags/AA:BB:CC:DD:EE:FF", json=body)
        assert resp.status_code == 422
        snapshot()

    @pytest.mark.parametrize("name", ["Room\nB", "Tab\there", "", "   "])
    async def test_invalid_name_returns_422(self, client, snapshot, name):
        resp = await client.patch("/api/tags/AA:BB:CC:DD:EE:FF", json={"name": name})
        assert resp.status_code == 422
        assert "name" in resp.json()["detail"]
        snapshot()


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
        assert data["dashboard_title"] == "Ruuvi Dashboard"

    async def test_get_settings_includes_dashboard_title(self, client, app_state):
        app_state.dashboard_title = "Home & <Sensors>"
        resp = await client.get("/api/settings")
        assert resp.json()["dashboard_title"] == "Home & <Sensors>"

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

    @pytest.mark.parametrize(
        "macs",
        [
            ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66", "11:22:33:44:55:66"],
            ["aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"],
            ["AA:BB:CC:DD:EE:FF"],
            ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66", "FF:FF:FF:FF:FF:FF"],
            [],
        ],
        ids=[
            "duplicate",
            "duplicate-case",
            "missing",
            "extra",
            "empty",
        ],
    )
    async def test_reorder_invalid_list_returns_400(self, client, two_tag_state, macs):
        before_config = two_tag_state.config.model_copy(deep=True)
        before_text = Path(two_tag_state.config_path).read_text()
        resp = await client.put("/api/tags/order", json={"macs": macs})
        assert resp.status_code == 400
        assert two_tag_state.config == before_config
        assert Path(two_tag_state.config_path).read_text() == before_text

    @pytest.mark.parametrize(
        "macs",
        [
            ["AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF"],
            ["AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff"],
        ],
    )
    async def test_reorder_duplicate_of_only_tag_returns_400(
        self, client, snapshot, macs
    ):
        resp = await client.put("/api/tags/order", json={"macs": macs})
        assert resp.status_code == 400
        snapshot()

    async def test_reorder_normalizes_case(self, client, two_tag_state):
        resp = await client.put(
            "/api/tags/order", json={"macs": ["11:22:33:44:55:66", "aa:bb:cc:dd:ee:ff"]}
        )
        assert resp.status_code == 200
        reloaded = load_config(two_tag_state.config_path)
        assert [t.mac for t in reloaded.tags] == [
            "11:22:33:44:55:66",
            "AA:BB:CC:DD:EE:FF",
        ]

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


import asyncio

from aiohttp import web

METRICS_TEXT = """\
# HELP vm_rows rows
flag{name="retentionPeriod", value="1y"} 1
vm_rows{type="storage/big"} 10
vm_rows{type="storage/small"} 5
vm_data_size_bytes{type="storage/big"} 2048
vm_data_size_bytes{type="storage/small"} 1024
vm_cache_entries{type="storage/metricName"} 3
"""


@pytest.fixture
async def fake_vm(app_state):
    """Serve a fake VictoriaMetrics on 127.0.0.1 whose behaviour tests choose."""
    handlers = {}
    release = asyncio.Event()

    async def hang(_):
        await release.wait()
        return web.Response(text="")

    handlers["hang"] = hang

    async def dispatch(request):
        return await handlers[request.path](request)

    vm_app = web.Application()
    vm_app.router.add_get("/{tail:.*}", dispatch)
    runner = web.AppRunner(vm_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    app_state.config.collector.victoriametrics_url = f"http://127.0.0.1:{port}"
    yield handlers
    release.set()
    await runner.cleanup()


class TestDbStats:
    async def test_returns_stats(self, client, fake_vm):
        async def metrics(_):
            return web.Response(text=METRICS_TEXT)

        async def tsdb(_):
            return web.json_response(
                {
                    "data": {
                        "totalSeries": 7,
                        "seriesCountByMetricName": [{"name": "ruuvi", "value": 7}],
                    }
                }
            )

        fake_vm["/metrics"] = metrics
        fake_vm["/api/v1/status/tsdb"] = tsdb
        resp = await client.get("/api/db-stats")
        assert resp.status_code == 200
        assert resp.json() == {
            "total_datapoints": 15,
            "storage_bytes": 3072,
            "active_time_series": 3,
            "total_series": 7,
            "series_by_metric": [{"name": "ruuvi", "count": 7}],
        }

    async def test_hanging_database_times_out_with_503(
        self, client, fake_vm, monkeypatch
    ):
        monkeypatch.setattr(api, "DB_STATS_TIMEOUT_SECONDS", 0.2)
        fake_vm["/metrics"] = fake_vm["hang"]
        fake_vm["/api/v1/status/tsdb"] = fake_vm["hang"]
        async with asyncio.timeout(5):
            resp = await client.get("/api/db-stats")
        assert resp.status_code == 503
        assert resp.json() == {"detail": "VictoriaMetrics statistics are unavailable"}

    async def test_invalid_responses_return_503(self, client, fake_vm):
        async def bad_metrics(_):
            return web.Response(text='vm_rows{type="storage/big"} not-a-number')

        async def bad_tsdb(_):
            return web.Response(text="<html>oops</html>", content_type="text/html")

        fake_vm["/metrics"] = bad_metrics
        fake_vm["/api/v1/status/tsdb"] = bad_tsdb
        resp = await client.get("/api/db-stats")
        assert resp.status_code == 503

    async def test_other_service_on_url_returns_503(self, client, fake_vm):
        async def html(_):
            return web.Response(text="<html>hi</html>", content_type="text/html")

        async def empty_json(_):
            return web.json_response({})

        fake_vm["/metrics"] = html
        fake_vm["/api/v1/status/tsdb"] = empty_json
        resp = await client.get("/api/db-stats")
        assert resp.status_code == 503

    async def test_unexpected_json_shape_returns_503(self, client, fake_vm):
        async def error(_):
            return web.Response(status=500)

        async def odd_tsdb(_):
            return web.json_response(["not", "an", "object"])

        fake_vm["/metrics"] = error
        fake_vm["/api/v1/status/tsdb"] = odd_tsdb
        resp = await client.get("/api/db-stats")
        assert resp.status_code == 503

    async def test_partial_stats_when_one_request_fails(self, client, fake_vm):
        async def metrics(_):
            return web.Response(text=METRICS_TEXT)

        async def error(_):
            return web.Response(status=500)

        fake_vm["/metrics"] = metrics
        fake_vm["/api/v1/status/tsdb"] = error
        resp = await client.get("/api/db-stats")
        assert resp.status_code == 200
        assert resp.json()["storage_bytes"] == 3072
        assert "total_series" not in resp.json()


class TestStaticFiles:
    async def test_root_serves_html(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


class TestApplyConfigFailures:
    """A failed save leaves files and runtime state on the old config (issue #7)."""

    @pytest.fixture
    def dashboard_before(self, app_state):
        write_dashboard(app_state.config.tags, app_state.dashboard_path)
        return Path(app_state.dashboard_path).read_text()

    async def test_dashboard_render_failure_changes_nothing(
        self, client, app_state, snapshot, dashboard_before, monkeypatch
    ):
        monkeypatch.setattr(
            api, "render_dashboard", failing(RuntimeError("render failed"))
        )
        with pytest.raises(RuntimeError, match="render failed"):
            await client.patch("/api/tags/AA:BB:CC:DD:EE:FF", json={"name": "New"})

        snapshot()
        assert Path(app_state.dashboard_path).read_text() == dashboard_before

    async def test_dashboard_write_failure_changes_nothing(
        self, client, app_state, snapshot, dashboard_before, monkeypatch
    ):
        monkeypatch.setattr(
            api, "write_dashboard_text", failing(OSError("dashboards dir read-only"))
        )
        resp = await client.patch("/api/tags/AA:BB:CC:DD:EE:FF", json={"name": "New"})

        assert resp.status_code == 500
        assert "dashboard" in resp.json()["detail"]
        snapshot()
        assert Path(app_state.dashboard_path).read_text() == dashboard_before

    async def test_config_write_failure_restores_dashboard(
        self, client, app_state, snapshot, dashboard_before, monkeypatch
    ):
        monkeypatch.setattr(
            config_module, "atomic_write_text", failing(OSError("config dir read-only"))
        )
        resp = await client.patch("/api/tags/AA:BB:CC:DD:EE:FF", json={"name": "New"})

        assert resp.status_code == 500
        assert "configuration" in resp.json()["detail"]
        snapshot()
        assert Path(app_state.dashboard_path).read_text() == dashboard_before


class TestApplyConfigWriter:
    def test_new_victoriametrics_url_reaches_writer(self, app_state):
        """Applying a config with a new URL moves writes there (issue #6)."""
        writer = MetricsWriter("http://db-a:8428", min_write_interval=5)
        app_state.writer = writer
        new_config = app_state.config.model_copy(
            update={
                "collector": CollectorConfig(
                    victoriametrics_url="http://db-b:8428",
                    min_write_interval_seconds=10,
                )
            }
        )

        api._apply_config(app_state, new_config)  # noqa: SLF001 - both apply paths

        assert writer._url == "http://db-b:8428/write?precision=ns"  # noqa: SLF001
        assert writer._min_interval == 10  # noqa: SLF001
