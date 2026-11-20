import textwrap
import time

import pytest
from conftest import failing, make_reading
from pydantic import ValidationError

import config as config_module
import fileutil
from config import (
    AppConfig,
    CollectorConfig,
    ConfigWatcher,
    DashboardConfig,
    ReadingsStore,
    TagConfig,
    TagReading,
    load_config,
    save_config,
)


def _one_tag_config(name: str) -> AppConfig:
    return AppConfig(tags=[TagConfig(mac="AA:BB:CC:DD:EE:FF", name=name)])


def _tag_names(changes: list[AppConfig]) -> list[str]:
    return [c.tags[0].name for c in changes if c.tags]


class TestTagConfig:
    def test_mac_uppercased(self):
        tag = TagConfig(mac="aa:bb:cc:dd:ee:ff", name="Test")
        assert tag.mac == "AA:BB:CC:DD:EE:FF"

    def test_enabled_defaults_true(self):
        tag = TagConfig(mac="AA:BB:CC:DD:EE:FF", name="Test")
        assert tag.enabled is True

    @pytest.mark.parametrize(
        "mac",
        [
            "invalid",
            "AA:BB:CC:DD:EE",
            "AA-BB-CC-DD-EE-FF",
            "GG:BB:CC:DD:EE:FF",
            "AA:BB:CC:DD:EE:\ufb00",
        ],
    )
    def test_invalid_mac_rejected(self, mac):
        with pytest.raises(ValidationError, match="Invalid MAC address"):
            TagConfig(mac=mac, name="Test")

    @pytest.mark.parametrize("name", ["Room\nB", "Room\rB", "Tab\there", "a\x00b"])
    def test_control_characters_in_name_rejected(self, name):
        with pytest.raises(ValidationError, match="control characters"):
            TagConfig(mac="AA:BB:CC:DD:EE:FF", name=name)

    @pytest.mark.parametrize("name", ["", "   "])
    def test_blank_name_rejected(self, name):
        with pytest.raises(ValidationError, match="must not be empty"):
            TagConfig(mac="AA:BB:CC:DD:EE:FF", name=name)

    def test_unicode_name_allowed(self):
        tag = TagConfig(mac="AA:BB:CC:DD:EE:FF", name="Kylpyhuone, Ä = 1 ☀")
        assert tag.name == "Kylpyhuone, Ä = 1 ☀"


class TestDashboardConfig:
    @pytest.mark.parametrize("columns", [1, 12])
    def test_columns_bounds_accepted(self, columns):
        assert DashboardConfig(columns_per_row=columns).columns_per_row == columns

    @pytest.mark.parametrize("columns", [0, -1, 13])
    def test_columns_out_of_range_rejected(self, columns):
        with pytest.raises(ValidationError):
            DashboardConfig(columns_per_row=columns)


class TestCollectorConfig:
    def test_defaults(self):
        c = CollectorConfig()
        assert c.victoriametrics_url == "http://victoriametrics:8428"
        assert c.min_write_interval_seconds is None

    @pytest.mark.parametrize("interval", [0, -1])
    def test_non_positive_interval_rejected(self, interval):
        with pytest.raises(ValidationError):
            CollectorConfig(min_write_interval_seconds=interval)


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

    def test_duplicate_macs_rejected_case_insensitively(self):
        with pytest.raises(ValidationError, match="Duplicate tag MAC"):
            AppConfig.model_validate(
                {
                    "tags": [
                        {"mac": "AA:BB:CC:DD:EE:FF", "name": "Room A"},
                        {"mac": "aa:bb:cc:dd:ee:ff", "name": "Room B"},
                    ]
                }
            )


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

    @pytest.mark.parametrize(
        "text",
        [
            "dashboard:\n  columns_per_row: 0\n",
            "dashboard:\n  columns_per_row: 13\n",
            "collector:\n  min_write_interval_seconds: -1\n",
            "collector:\n  min_write_interval_seconds: 0\n",
            'tags:\n  - mac: "invalid"\n    name: "Room"\n',
            'tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "Room\\nB"\n',
            'tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: ""\n',
            'tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: null\n',
            'tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "R"\n    enabled: null\n',
            'tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "A"\n'
            '  - mac: "aa:bb:cc:dd:ee:ff"\n    name: "B"\n',
        ],
    )
    def test_load_invalid_yaml_raises_validation_error(self, tmp_path, text):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(text)
        with pytest.raises(ValidationError):
            load_config(str(config_file))

    @pytest.mark.parametrize("text", ["[]\n", "false\n", "0\n", "just text\n"])
    def test_load_non_mapping_yaml_raises_validation_error(self, tmp_path, text):
        """A non-mapping root must not silently load an empty default config."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(text)
        with pytest.raises(ValidationError):
            load_config(str(config_file))

    def test_startup_with_invalid_yaml_fails_with_validation_error(self, tmp_path):
        """Startup stops at config loading, before dashboard generation."""
        from main import _ensure_config

        config_file = tmp_path / "config.yaml"
        config_file.write_text("dashboard:\n  columns_per_row: 0\n")
        with pytest.raises(ValidationError, match="columns_per_row"):
            _ensure_config(str(config_file))


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
            save_config(_one_tag_config("Saved"), str(config_file), watcher)
            time.sleep(1.5)
        finally:
            watcher.stop()

        assert changes == []

    def test_applies_external_edit_right_after_own_save(self, tmp_path):
        """An external edit just after an API save is reloaded (issue #8)."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tags: []\n")

        changes = []
        watcher = ConfigWatcher(str(config_file), on_change=changes.append)
        watcher.start()
        try:
            time.sleep(0.5)
            save_config(_one_tag_config("Saved"), str(config_file), watcher)
            config_file.write_text(
                'tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "Edited"\n'
            )
            for _ in range(40):
                if _tag_names(changes)[-1:] == ["Edited"]:
                    break
                time.sleep(0.1)
        finally:
            watcher.stop()

        # An in-place write can also be seen half-written (an empty file), so
        # check only that the edit was applied and our own save was not.
        assert _tag_names(changes)[-1:] == ["Edited"]
        assert "Saved" not in _tag_names(changes)

    def test_own_save_then_external_edit_without_observer(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tags: []\n")
        changes = []
        watcher = ConfigWatcher(str(config_file), on_change=changes.append)
        reload = watcher._handle_change  # noqa: SLF001 - what the observer calls

        save_config(_one_tag_config("Saved"), str(config_file), watcher)
        reload()
        assert changes == []

        config_file.write_text(
            'tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "Edited"\n'
        )
        reload()
        reload()  # a second event for the same content is not reapplied
        assert [c.tags[0].name for c in changes] == ["Edited"]

        # Reverting by hand to the text we saved earlier is still an edit.
        save_config(_one_tag_config("Saved"), str(tmp_path / "other.yaml"))
        config_file.write_text((tmp_path / "other.yaml").read_text())
        reload()
        assert [c.tags[0].name for c in changes] == ["Edited", "Saved"]

    def test_undecodable_file_does_not_stop_reloads(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tags: []\n")
        changes = []
        watcher = ConfigWatcher(str(config_file), on_change=changes.append)
        reload = watcher._handle_change  # noqa: SLF001 - what the observer calls

        config_file.write_bytes(
            'tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "Sauna \xe4"\n'.encode(
                "latin-1"
            )
        )
        reload()  # logged, not raised into the observer thread
        assert changes == []

        config_file.write_text(
            'tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "Fixed"\n'
        )
        reload()
        assert _tag_names(changes) == ["Fixed"]

    def test_failed_own_save_does_not_hide_that_content(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tags: []\n")
        changes = []
        watcher = ConfigWatcher(str(config_file), on_change=changes.append)

        monkeypatch.setattr(
            config_module, "atomic_write_text", failing(OSError("disk full"))
        )
        with pytest.raises(OSError, match="disk full"):
            save_config(_one_tag_config("Saved"), str(config_file), watcher)
        monkeypatch.undo()

        save_config(_one_tag_config("Saved"), str(tmp_path / "other.yaml"))
        config_file.write_text((tmp_path / "other.yaml").read_text())
        watcher._handle_change()  # noqa: SLF001 - what the observer calls
        assert [c.tags[0].name for c in changes] == ["Saved"]

    def test_rejected_reload_keeps_last_valid_config(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text('tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "A"\n')
        running = [load_config(str(config_file))]
        watcher = ConfigWatcher(str(config_file), on_change=running.append)
        reload = watcher._handle_change  # noqa: SLF001 - what the observer calls

        config_file.write_text("dashboard:\n  columns_per_row: 0\n")
        reload()
        config_file.write_text(
            'tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "A"\n'
            '  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "B"\n'
        )
        reload()
        assert len(running) == 1
        assert running[-1].tags[0].name == "A"

        config_file.write_text('tags:\n  - mac: "AA:BB:CC:DD:EE:FF"\n    name: "C"\n')
        reload()
        assert running[-1].tags[0].name == "C"


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

    def test_failed_save_keeps_previous_file(self, tmp_path, monkeypatch):
        path = tmp_path / "config.yaml"
        save_config(_one_tag_config("Old"), str(path))
        before = path.read_text()

        monkeypatch.setattr(fileutil.os, "fsync", failing(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            save_config(_one_tag_config("New"), str(path))

        assert path.read_text() == before
        assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]

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
