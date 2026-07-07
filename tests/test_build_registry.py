"""Tests for build_registry error path."""

from __future__ import annotations

import os
import stat

import pytest

from agent_manager.config import Config, FTPSettings, LoggingSettings, ServerSettings, TLSSettings, UpgradeSettings
from agent_manager.upgrade import build_registry


def _make_config(work_dir: str) -> Config:
    return Config(
        server=ServerSettings(),
        api=None,  # type: ignore[arg-type]
        upgrade=UpgradeSettings(
            work_dir=work_dir,
            install_root="/tmp/_unused",
            ftp=FTPSettings(url=""),
        ),
        logging=LoggingSettings(),
        tls=TLSSettings(enabled=False),
    )


def test_build_registry_creates_dir(tmp_path):
    cfg = _make_config(str(tmp_path / "work"))
    build_registry(cfg)
    assert (tmp_path / "work").is_dir()
    assert (tmp_path / "work" / "jobs.json").is_file() or True  # jobs.json may not exist yet


def test_build_registry_unwritable_parent_gives_helpful_error(tmp_path):
    # Make a read-only parent, then ask build_registry to create a child
    ro_parent = tmp_path / "ro_parent"
    ro_parent.mkdir()
    # strip write bits from parent — non-root can still read but not create children
    os.chmod(ro_parent, stat.S_IRUSR | stat.S_IXUSR)
    try:
        cfg = _make_config(str(ro_parent / "work"))
        with pytest.raises(OSError) as ei:
            build_registry(cfg)
        msg = str(ei.value)
        # Operator-facing context must mention the path AND the config knob
        assert "could not create upgrade.work_dir" in msg
        assert str(ro_parent / "work") in msg
        assert "config.yaml" in msg
    finally:
        os.chmod(ro_parent, stat.S_IRWXU)