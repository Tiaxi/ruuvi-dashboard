import stat
from pathlib import Path

import pytest
from conftest import failing

import fileutil
from fileutil import atomic_write_text


def _mode(path) -> int:
    return stat.S_IMODE(Path(path).stat().st_mode)


class TestAtomicWriteText:
    def test_creates_readable_file(self, tmp_path):
        path = tmp_path / "out.json"
        atomic_write_text(path, "hello ä")
        assert path.read_text(encoding="utf-8") == "hello ä"
        assert _mode(path) == 0o644

    def test_keeps_existing_mode(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("old")
        path.chmod(0o664)
        atomic_write_text(path, "new")
        assert path.read_text() == "new"
        assert _mode(path) == 0o664

    def test_failure_keeps_old_file_and_removes_temp(self, tmp_path, monkeypatch):
        path = tmp_path / "config.yaml"
        path.write_text("old")

        monkeypatch.setattr(fileutil.os, "replace", failing(OSError("rename failed")))
        with pytest.raises(OSError, match="rename failed"):
            atomic_write_text(path, "new")

        assert path.read_text() == "old"
        assert [p.name for p in tmp_path.iterdir()] == ["config.yaml"]

    def test_replaces_symlink_target_not_link(self, tmp_path):
        target = tmp_path / "real" / "config.yaml"
        target.parent.mkdir()
        target.write_text("old")
        link = tmp_path / "config.yaml"
        link.symlink_to(target)

        atomic_write_text(link, "new")

        assert link.is_symlink()
        assert target.read_text() == "new"
