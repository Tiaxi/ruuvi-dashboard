import base64
import logging
import stat
import time

import pytest
from httpx import ASGITransport, AsyncClient

from api import AppState, create_app
from auth import resolve_admin_password
from config import AppConfig, ConfigWatcher, ReadingsStore, TagConfig, save_config

PASSWORD = "correct horse battery staple"
MAC = "AA:BB:CC:DD:EE:FF"


@pytest.fixture
def app_state(tmp_path):
    config = AppConfig(tags=[TagConfig(mac=MAC, name="Room A")])
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


def make_client(app_state, auth=None, headers=None):
    app = create_app(app_state, admin_password=PASSWORD)
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        auth=auth,
        headers=headers,
    )


PROTECTED = [
    ("GET", "/", None),
    ("GET", "/index.html", None),
    ("GET", "/api/tags", None),
    ("GET", "/api/discovered", None),
    ("GET", "/api/settings", None),
    ("GET", "/api/db-stats", None),
    ("GET", "/api/does-not-exist", None),
    ("POST", "/api/tags", {"mac": "11:22:33:44:55:66", "name": "X"}),
    ("PATCH", f"/api/tags/{MAC}", {"name": "Hacked"}),
    ("DELETE", f"/api/tags/{MAC}", None),
    ("PUT", "/api/tags/order", {"macs": [MAC]}),
    ("PATCH", "/api/settings", {"columns_per_row": 3}),
    ("POST", "/api/health", None),
]


def assert_unauthorized(resp):
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith('Basic realm="')


class TestUnauthenticated:
    @pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
    async def test_no_credentials_rejected(self, app_state, method, path, body):
        async with make_client(app_state) as client:
            resp = await client.request(method, path, json=body)
        assert_unauthorized(resp)

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "/api/tags", None),
            ("PATCH", "/api/settings", {"columns_per_row": 3}),
        ],
    )
    async def test_wrong_password_rejected(self, app_state, method, path, body):
        async with make_client(app_state, auth=("admin", "wrong")) as client:
            resp = await client.request(method, path, json=body)
        assert_unauthorized(resp)

    async def test_mutations_without_credentials_leave_config_unchanged(
        self, app_state
    ):
        before = app_state.config.model_copy(deep=True)
        async with make_client(app_state) as client:
            for method, path, body in PROTECTED:
                await client.request(method, path, json=body)
        assert app_state.config == before

    async def test_wrong_username_rejected(self, app_state):
        async with make_client(app_state, auth=("root", PASSWORD)) as client:
            resp = await client.get("/api/tags")
        assert_unauthorized(resp)

    @pytest.mark.parametrize(
        "header",
        [
            "Bearer abc",
            "Basic",
            "Basic not-base64!",
            "Basic " + base64.b64encode(b"admin").decode(),
            "Basic " + base64.b64encode(f"admin:{PASSWORD}x".encode()).decode(),
            "Basic " + base64.b64encode(b"\xff\xfe:\xff").decode(),
        ],
    )
    async def test_malformed_authorization_rejected(self, app_state, header):
        async with make_client(app_state, headers={"Authorization": header}) as c:
            resp = await c.get("/api/tags")
        assert_unauthorized(resp)


class TestAuthenticated:
    async def test_reads_work(self, app_state):
        async with make_client(app_state, auth=("admin", PASSWORD)) as client:
            ui = await client.get("/")
            tags = await client.get("/api/tags")
            settings = await client.get("/api/settings")
        assert ui.status_code == 200
        assert "<html" in ui.text.lower()
        assert tags.status_code == 200
        assert tags.json()[0]["mac"] == MAC
        assert settings.status_code == 200

    async def test_mutations_work(self, app_state):
        async with make_client(app_state, auth=("admin", PASSWORD)) as client:
            patched = await client.patch(f"/api/tags/{MAC}", json={"name": "Den"})
            settings = await client.patch("/api/settings", json={"columns_per_row": 4})
            deleted = await client.delete(f"/api/tags/{MAC}")
        assert patched.status_code == 200
        assert settings.status_code == 200
        assert deleted.status_code == 204
        assert app_state.config.tags == []
        assert app_state.config.dashboard.columns_per_row == 4

    async def test_scheme_is_case_insensitive(self, app_state):
        token = base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
        headers = {"Authorization": f"basic {token}"}
        async with make_client(app_state, headers=headers) as client:
            resp = await client.get("/api/tags")
        assert resp.status_code == 200

    async def test_non_ascii_password(self, app_state):
        app = create_app(app_state, admin_password="pässwörd-€")
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            auth=("admin", "pässwörd-€"),
        ) as client:
            resp = await client.get("/api/tags")
        assert resp.status_code == 200


class TestHealthIsPublic:
    async def test_health_without_credentials(self, app_state):
        async with make_client(app_state) as client:
            resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["tag_count"] == 1


class TestCreateApp:
    def test_empty_password_refused(self, app_state):
        with pytest.raises(ValueError, match="must not be empty"):
            create_app(app_state, admin_password="")


class TestResolveAdminPassword:
    def test_env_password_wins_and_no_file_written(self, tmp_path):
        config_path = str(tmp_path / "config.yaml")
        assert resolve_admin_password(config_path, "from-env") == "from-env"
        assert not (tmp_path / "admin_password").exists()

    def test_env_password_overrides_existing_file(self, tmp_path):
        (tmp_path / "admin_password").write_text("from-file\n")
        config_path = str(tmp_path / "config.yaml")
        assert resolve_admin_password(config_path, "from-env") == "from-env"

    @pytest.mark.parametrize("env", [None, ""])
    def test_generates_private_file_when_env_unset_or_empty(self, tmp_path, env):
        config_path = str(tmp_path / "config.yaml")
        password = resolve_admin_password(config_path, env)
        path = tmp_path / "admin_password"
        assert len(password) >= 32
        assert path.read_text() == f"{password}\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert sorted(p.name for p in tmp_path.iterdir()) == ["admin_password"]

    def test_reuses_stored_password(self, tmp_path):
        config_path = str(tmp_path / "config.yaml")
        first = resolve_admin_password(config_path, None)
        assert resolve_admin_password(config_path, None) == first

    def test_uses_hand_edited_file(self, tmp_path):
        (tmp_path / "admin_password").write_text("my own password\r\n")
        config_path = str(tmp_path / "config.yaml")
        assert resolve_admin_password(config_path, None) == "my own password"

    def test_regenerates_blank_file(self, tmp_path):
        path = tmp_path / "admin_password"
        path.write_text("  \n")
        password = resolve_admin_password(str(tmp_path / "config.yaml"), None)
        assert password.strip()
        assert path.read_text() == f"{password}\n"

    def test_creates_missing_config_dir(self, tmp_path):
        config_path = str(tmp_path / "nested" / "config.yaml")
        password = resolve_admin_password(config_path, None)
        assert (tmp_path / "nested" / "admin_password").read_text().strip() == (
            password
        )

    def test_password_is_never_logged(self, tmp_path, caplog):
        caplog.set_level(logging.DEBUG)
        config_path = str(tmp_path / "config.yaml")
        generated = resolve_admin_password(config_path, None)
        resolve_admin_password(config_path, None)
        resolve_admin_password(config_path, "env-secret")
        assert str(tmp_path / "admin_password") in caplog.text
        assert generated not in caplog.text
        assert "env-secret" not in caplog.text

    def test_generated_passwords_differ(self, tmp_path):
        a = resolve_admin_password(str(tmp_path / "a" / "config.yaml"), None)
        b = resolve_admin_password(str(tmp_path / "b" / "config.yaml"), None)
        assert a != b


class TestConfigWatcherIgnoresPasswordFile:
    def test_password_file_writes_do_not_reload_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        save_config(AppConfig(), str(config_file))
        changes = []
        watcher = ConfigWatcher(str(config_file), on_change=changes.append)
        watcher.start()
        try:
            time.sleep(0.2)
            resolve_admin_password(str(config_file), None)
            (tmp_path / "admin_password").write_text("edited\n")
            time.sleep(1.0)
        finally:
            watcher.stop()
        assert changes == []
