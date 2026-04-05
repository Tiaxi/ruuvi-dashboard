import textwrap
import time
from unittest.mock import MagicMock

import pytest
from conftest import make_reading

from config import (
    AppConfig,
    CollectorConfig,
    ConfigWatcher,
    ReadingsStore,
    TagConfig,
    TagReading,
    load_config,
    save_config,
)


class TestTagConfig:
    def test_mac_uppercased(self):
        tag = TagConfig(mac="aa:bb:cc:dd:ee:ff", name="Test")
        assert tag.mac == "AA:BB:CC:DD:EE:FF"

    def test_enabled_defaults_true(self):
        tag = TagConfig(mac="AA:BB:CC:DD:EE:FF", name="Test")
        assert tag.enabled is True


class TestCollectorConfig:
    def test_defaults(self):
        c = CollectorConfig()
        assert c.victoriametrics_url == "http://victoriametrics:8428"
        assert c.min_write_interval_seconds is None


class TestAppConfig:
    def test_enabled_tags_map(self):
        config = AppConfig(
            tags=[
                TagConfig(mac="AA:BB:CC:DD:EE:FF", name="Room A", enabled=True),
                TagConfig(mac="11:22:33:44:55:66", name="Room B", enabled=False),
            ]
        )
        enabled = config.enabled_tags_map
        assert "AA:BB:CC:DD:EE:FF" in enabled
        assert "11:22:33:44:55:66" not in enabled
        assert enabled["AA:BB:CC:DD:EE:FF"].name == "Room A"

    def test_empty_tags(self):
        config = AppConfig(tags=[])
        assert config.enabled_tags_map == {}


class TestLoadConfig:
    def test_load_valid_yaml(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            tags:
              - mac: "AA:BB:CC:DD:EE:FF"
                name: "Sauna"
                enabled: true
              - mac: "11:22:33:44:55:66"
                name: "Balcony"
                enabled: false
            collector:
              min_write_interval_seconds: 30
              victoriametrics_url: "http://localhost:8428"
        """)
        )
        config = load_config(str(config_file))
        assert len(config.tags) == 2
        assert config.tags[0].name == "Sauna"
        assert config.collector.min_write_interval_seconds == 30

    def test_load_minimal_yaml(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tags: []\n")
        config = load_config(str(config_file))
        assert config.tags == []
        assert config.collector.victoriametrics_url == "http://victoriametrics:8428"

    def test_load_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")


class TestReadingsStore:
    async def test_update_and_get_all(self):
        store = ReadingsStore()
        await store.update("AA:BB:CC:DD:EE:FF", make_reading(), rssi=-65)
        result = await store.get_all()
        assert "AA:BB:CC:DD:EE:FF" in result
        tr = result["AA:BB:CC:DD:EE:FF"]
        assert isinstance(tr, TagReading)
        assert tr.reading.temperature == 22.5
        assert tr.rssi == -65
        assert tr.last_seen > 0

    async def test_update_replaces_previous(self):
        store = ReadingsStore()
        await store.update("AA:BB:CC:DD:EE:FF", make_reading(), rssi=-60)
        await store.update(
            "AA:BB:CC:DD:EE:FF", make_reading(temperature=25.0), rssi=-55
        )
        result = await store.get_all()
        assert result["AA:BB:CC:DD:EE:FF"].reading.temperature == 25.0
        assert result["AA:BB:CC:DD:EE:FF"].rssi == -55

    async def test_get_all_returns_copy(self):
        store = ReadingsStore()
        await store.update("AA:BB:CC:DD:EE:FF", make_reading(), rssi=-65)
        result1 = await store.get_all()
        result1.pop("AA:BB:CC:DD:EE:FF")
        result2 = await store.get_all()
        assert "AA:BB:CC:DD:EE:FF" in result2

    async def test_get_by_mac(self):
        store = ReadingsStore()
        await store.update("AA:BB:CC:DD:EE:FF", make_reading(), rssi=-65)
        tr = await store.get_by_mac("AA:BB:CC:DD:EE:FF")
        assert tr is not None
        assert tr.reading.temperature == 22.5

    async def test_get_by_mac_missing_returns_none(self):
        store = ReadingsStore()
        assert await store.get_by_mac("FF:FF:FF:FF:FF:FF") is None

    async def test_remove(self):
        store = ReadingsStore()
        await store.update("AA:BB:CC:DD:EE:FF", make_reading(), rssi=-65)
        await store.remove("AA:BB:CC:DD:EE:FF")
        assert await store.get_by_mac("AA:BB:CC:DD:EE:FF") is None

    async def test_remove_missing_is_noop(self):
        store = ReadingsStore()
        await store.remove("FF:FF:FF:FF:FF:FF")


class TestConfigWatcher:
    def test_detects_external_change(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tags: []\n")

        changes = []
        watcher = ConfigWatcher(str(config_file), on_change=changes.append)
        watcher.start()
        try:
            time.sleep(0.5)
            config_file.write_text(
                'tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "Test"\n'
            )
            for _ in range(40):
                if changes:
                    break
                time.sleep(0.1)
        finally:
            watcher.stop()

        assert len(changes) >= 1
        assert len(changes[0].tags) == 1

    def test_ignores_own_writes(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tags: []\n")

        changes = []
        watcher = ConfigWatcher(str(config_file), on_change=changes.append)
        watcher.start()
        try:
            time.sleep(0.5)
            watcher.mark_own_write()
            config_file.write_text(
                'tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "Test"\n'
            )
            time.sleep(3)
        finally:
            watcher.stop()

        assert len(changes) == 0


class TestSaveConfig:
    def test_save_and_reload_roundtrip(self, tmp_path):
        config = AppConfig(
            tags=[
                TagConfig(mac="AA:BB:CC:DD:EE:FF", name="Sauna", enabled=True),
                TagConfig(mac="11:22:33:44:55:66", name="Kitchen", enabled=False),
            ],
            collector=CollectorConfig(min_write_interval_seconds=30),
        )
        path = str(tmp_path / "config.yaml")
        save_config(config, path)
        reloaded = load_config(path)
        assert len(reloaded.tags) == 2
        assert reloaded.tags[0].mac == "AA:BB:CC:DD:EE:FF"
        assert reloaded.tags[0].name == "Sauna"
        assert reloaded.tags[1].enabled is False
        assert reloaded.collector.min_write_interval_seconds == 30

    def test_save_calls_mark_own_write(self, tmp_path):
        config = AppConfig(tags=[])
        path = str(tmp_path / "config.yaml")
        watcher = MagicMock()
        save_config(config, path, watcher=watcher)
        watcher.mark_own_write.assert_called_once()

    def test_save_without_watcher(self, tmp_path):
        config = AppConfig(tags=[])
        path = str(tmp_path / "config.yaml")
        save_config(config, path)

    def test_save_with_null_interval(self, tmp_path):
        config = AppConfig(
            tags=[TagConfig(mac="AA:BB:CC:DD:EE:FF", name="Test")],
            collector=CollectorConfig(min_write_interval_seconds=None),
        )
        path = str(tmp_path / "config.yaml")
        save_config(config, path)
        reloaded = load_config(path)
        assert reloaded.collector.min_write_interval_seconds is None

    def test_save_default_config_creates_loadable_file(self, tmp_path):
        """Saving a default AppConfig produces a file that loads back with defaults."""
        path = str(tmp_path / "subdir" / "config.yaml")
        from pathlib import Path

        Path(path).parent.mkdir(parents=True)
        save_config(AppConfig(), path)
        reloaded = load_config(path)
        assert reloaded.tags == []
        assert reloaded.collector.victoriametrics_url == "http://victoriametrics:8428"
        assert reloaded.dashboard.columns_per_row == 6
