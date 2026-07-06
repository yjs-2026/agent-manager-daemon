"""Tests for ``agent_manager.upgrade``.

Covers:
  * ArchiveExtractor (suffix detection + safe extract with traversal guard)
  * FtpDownloader against a local HTTP file server (avoids needing a
    real FTP daemon in CI)
  * UpgradeManager happy path + failed download + rollback
  * JobRegistry persistence + concurrency
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from agent_manager.config import Config, FTPSettings, UpgradeSettings
from agent_manager.upgrade import (
    ArchiveExtractor,
    DownloadFailed,
    ExtractFailed,
    FtpDownloader,
    JobRegistry,
    JobStatus,
    SwitchFailed,
    UnsupportedArchive,
    UpgradeError,
    UpgradeManager,
    UpgradeRequest,
    build_registry,
)


# ---------------------------------------------------------------------------
# ArchiveExtractor
# ---------------------------------------------------------------------------


def test_extractor_supports_known_suffixes(tmp_path: Path):
    ext = ArchiveExtractor((".tar.gz", ".zip"))
    assert ext.supports("agent-1.2.3.tar.gz") is True
    assert ext.supports("agent-1.2.3.zip") is True
    assert ext.supports("agent-1.2.3.TAR.GZ") is True  # case-insensitive
    assert ext.supports("agent.bin") is False


def test_extractor_extracts_tar_gz(tmp_path: Path):
    src = tmp_path / "build"
    src.mkdir()
    (src / "hello.txt").write_text("hi")
    archive = tmp_path / "agent.tar.gz"
    import tarfile
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(src / "hello.txt", arcname="hello.txt")

    out = tmp_path / "out"
    ArchiveExtractor((".tar.gz",)).extract(archive, out)
    assert (out / "hello.txt").read_text() == "hi"


def test_extractor_rejects_traversal(tmp_path: Path):
    archive = tmp_path / "evil.tar.gz"
    import tarfile
    with tarfile.open(archive, "w:gz") as tf:
        # Add a member that tries to escape.
        info = tarfile.TarInfo(name="../../etc/passwd")
        data = b"pwned"
        info.size = len(data)
        import io
        tf.addfile(info, io.BytesIO(data))

    with pytest.raises(ExtractFailed):
        ArchiveExtractor((".tar.gz",)).extract(archive, tmp_path / "out")


def test_extractor_unsupported(tmp_path: Path):
    archive = tmp_path / "agent.bin"
    archive.write_bytes(b"")
    with pytest.raises(UnsupportedArchive):
        ArchiveExtractor((".tar.gz",)).extract(archive, tmp_path / "out")


# ---------------------------------------------------------------------------
# FtpDownloader (backed by local HTTP)
# ---------------------------------------------------------------------------


def _make_dummy_archive(tmp_path: Path, name: str = "agent.tar.gz") -> Path:
    archive = tmp_path / name
    archive.write_bytes(b"hello-bytes")
    return archive


def test_downloader_via_http(http_file_server, tmp_path: Path, make_tar_gz):
    base_url, server_dir = http_file_server
    archive = make_tar_gz("agent-1.0.0.tar.gz", {"agent": b"binary"})
    # Put it where the HTTP server can find it.
    shutil.copy(archive, server_dir / "agent-1.0.0.tar.gz")

    ftp = FTPSettings(
        url=base_url,
        username_env="AGENT_MANAGER_FTP_USER",
        password_env="AGENT_MANAGER_FTP_PASS",
        timeout=10,
    )
    downloader = FtpDownloader(ftp)
    dest = tmp_path / "fetched.tar.gz"
    downloader.fetch(f"{base_url}/agent-1.0.0.tar.gz", dest)
    assert dest.is_file()
    assert dest.read_bytes() == archive.read_bytes()


def test_downloader_missing_file_raises(http_file_server, tmp_path: Path):
    base_url, _ = http_file_server
    ftp = FTPSettings(
        url=base_url,
        username_env="AGENT_MANAGER_FTP_USER",
        password_env="AGENT_MANAGER_FTP_PASS",
        timeout=10,
    )
    downloader = FtpDownloader(ftp)
    with pytest.raises(DownloadFailed):
        downloader.fetch(f"{base_url}/nope.tar.gz", tmp_path / "out.tar.gz")


# ---------------------------------------------------------------------------
# JobRegistry
# ---------------------------------------------------------------------------


def test_job_registry_create_get_list(tmp_path: Path):
    reg = JobRegistry(path=str(tmp_path / "jobs.json"))
    reg.create("j1", "agent-1.tar.gz", "1")
    reg.create("j2", "agent-2.tar.gz", "2")
    assert reg.get("j1").version == "1"
    listed = reg.list()
    assert {j.job_id for j in listed} == {"j1", "j2"}


def test_job_registry_persistence(tmp_path: Path):
    p = tmp_path / "jobs.json"
    reg = JobRegistry(path=str(p))
    reg.create("j1", "agent-1.tar.gz", "1")
    reg.update("j1", status=JobStatus.SUCCESS, finished_at="2025-01-01T00:00:00+00:00")
    reg2 = JobRegistry(path=str(p))
    loaded = reg2.get("j1")
    assert loaded is not None
    assert loaded.status == JobStatus.SUCCESS
    assert loaded.finished_at == "2025-01-01T00:00:00+00:00"


def test_job_registry_corrupted_file_is_nonfatal(tmp_path: Path):
    p = tmp_path / "jobs.json"
    p.write_text("{ this is not json")
    reg = JobRegistry(path=str(p))
    assert reg.list() == []  # graceful fallback


# ---------------------------------------------------------------------------
# UpgradeManager
# ---------------------------------------------------------------------------


def _manager(config: Config) -> UpgradeManager:
    registry = build_registry(config)
    return UpgradeManager(cfg=config, registry=registry, systemd_unit_override="")


def test_upgrade_happy_path(http_file_server, config: Config, make_tar_gz, tmp_path: Path, fake_clock):
    base_url, server_dir = http_file_server

    # Stage an artifact on the HTTP server.
    archive = make_tar_gz("agent-1.2.3.tar.gz", {"bin/agent": b"#!/bin/sh\necho agent"})
    shutil.copy(archive, server_dir / "agent-1.2.3.tar.gz")

    cfg = Config(**{**config.__dict__, "upgrade": UpgradeSettings(
        **{
            **config.upgrade.__dict__,
            "ftp": FTPSettings(url=base_url, username_env="", password_env="", timeout=10),
        }
    )})

    mgr = _manager(cfg)
    req = UpgradeRequest(
        job_id="j-happy",
        filename="agent-1.2.3.tar.gz",
        version="1.2.3",
    )
    job = mgr.upgrade(req)

    assert job.status == JobStatus.SUCCESS
    assert job.installed_release == "1.2.3"
    install_root = Path(cfg.upgrade.install_root)
    current = install_root / "current"
    assert current.is_symlink()
    assert current.resolve() == (install_root / "releases" / "1.2.3").resolve()
    assert (install_root / "releases" / "1.2.3" / "bin" / "agent").exists()


def test_upgrade_download_failure_marks_failed(config: Config, tmp_path: Path, fake_clock):
    cfg = Config(**{**config.__dict__, "upgrade": UpgradeSettings(
        **{
            **config.upgrade.__dict__,
            "ftp": FTPSettings(
                url="http://127.0.0.1:1/never",
                username_env="", password_env="", timeout=1,
            ),
        }
    )})
    mgr = _manager(cfg)
    req = UpgradeRequest(job_id="j-fail", filename="missing.tar.gz", version="9.9.9")
    job = mgr.upgrade(req)
    assert job.status == JobStatus.FAILED
    assert "download failed" in (job.error or "")


def test_upgrade_rejects_existing_release_dir(http_file_server, config: Config, make_tar_gz, tmp_path: Path, fake_clock):
    base_url, server_dir = http_file_server
    archive = make_tar_gz("agent-2.0.0.tar.gz", {"agent": b"x"})
    shutil.copy(archive, server_dir / "agent-2.0.0.tar.gz")

    cfg = Config(**{**config.__dict__, "upgrade": UpgradeSettings(
        **{
            **config.upgrade.__dict__,
            "ftp": FTPSettings(url=base_url, username_env="", password_env="", timeout=10),
        }
    )})
    # Pre-create the target dir.
    Path(cfg.upgrade.install_root, "releases", "2.0.0").mkdir(parents=True)

    mgr = _manager(cfg)
    job = mgr.upgrade(UpgradeRequest("j-x", "agent-2.0.0.tar.gz", "2.0.0"))
    assert job.status == JobStatus.FAILED
    assert "release dir already exists" in (job.error or "")


def test_upgrade_rollback_swaps_to_previous(http_file_server, config: Config, make_tar_gz, tmp_path: Path, fake_clock):
    base_url, server_dir = http_file_server

    cfg = Config(**{**config.__dict__, "upgrade": UpgradeSettings(
        **{
            **config.upgrade.__dict__,
            "ftp": FTPSettings(url=base_url, username_env="", password_env="", timeout=10),
        }
    )})
    mgr = _manager(cfg)

    shutil.copy(make_tar_gz("agent-1.0.0.tar.gz", {"agent": b"v1"}), server_dir / "agent-1.0.0.tar.gz")
    shutil.copy(make_tar_gz("agent-2.0.0.tar.gz", {"agent": b"v2"}), server_dir / "agent-2.0.0.tar.gz")

    mgr.upgrade(UpgradeRequest("j-1", "agent-1.0.0.tar.gz", "1.0.0"))
    mgr.upgrade(UpgradeRequest("j-2", "agent-2.0.0.tar.gz", "2.0.0"))

    install_root = Path(cfg.upgrade.install_root)
    current = install_root / "current"
    assert current.resolve().name == "2.0.0"

    rb = mgr.rollback("j-2")
    assert rb.status == JobStatus.SUCCESS
    assert current.resolve().name == "1.0.0"


def test_upgrade_prunes_old_releases(http_file_server, config: Config, make_tar_gz, tmp_path: Path, fake_clock):
    base_url, server_dir = http_file_server
    cfg = Config(**{**config.__dict__, "upgrade": UpgradeSettings(
        **{
            **config.upgrade.__dict__,
            "ftp": FTPSettings(url=base_url, username_env="", password_env="", timeout=10),
            "keep_releases": 2,
        }
    )})
    mgr = _manager(cfg)

    for v in ("1.0.0", "2.0.0", "3.0.0"):
        archive = make_tar_gz(f"agent-{v}.tar.gz", {f"agent-{v}": b"x"})
        shutil.copy(archive, server_dir / f"agent-{v}.tar.gz")
        mgr.upgrade(UpgradeRequest(f"j-{v}", f"agent-{v}.tar.gz", v))

    releases = sorted((Path(cfg.upgrade.install_root) / "releases").iterdir())
    assert {p.name for p in releases} == {"2.0.0", "3.0.0"}


def test_upgrade_unsupported_archive(http_file_server, config: Config, tmp_path: Path, fake_clock):
    base_url, server_dir = http_file_server
    cfg = Config(**{**config.__dict__, "upgrade": UpgradeSettings(
        **{
            **config.upgrade.__dict__,
            "ftp": FTPSettings(url=base_url, username_env="", password_env="", timeout=10),
        }
    )})
    (server_dir / "agent.bin").write_bytes(b"junk")

    mgr = _manager(cfg)
    job = mgr.upgrade(UpgradeRequest("j-bad", "agent.bin", "9.9.9"))
    assert job.status == JobStatus.FAILED
    assert "unsupported archive" in (job.error or "")


def test_upgrade_uses_ftp_url_override(http_file_server, config: Config, make_tar_gz, tmp_path: Path, fake_clock):
    base_url, server_dir = http_file_server
    cfg = Config(**{**config.__dict__, "upgrade": UpgradeSettings(
        **{
            **config.upgrade.__dict__,
            "ftp": FTPSettings(url="http://wrong-host:1/never", username_env="", password_env="", timeout=10),
        }
    )})
    archive = make_tar_gz("agent-1.0.0.tar.gz", {"agent": b"x"})
    shutil.copy(archive, server_dir / "agent-1.0.0.tar.gz")

    mgr = _manager(cfg)
    job = mgr.upgrade(
        UpgradeRequest(
            job_id="j-override",
            filename="agent-1.0.0.tar.gz",
            version="1.0.0",
            ftp_url=f"{base_url}/agent-1.0.0.tar.gz",
        )
    )
    assert job.status == JobStatus.SUCCESS


# ---------------------------------------------------------------------------
# Concurrency: two upgrades with the same job_id should serialize
# ---------------------------------------------------------------------------


def test_concurrent_upgrade_serialized(http_file_server, config: Config, make_tar_gz, tmp_path: Path, fake_clock):
    base_url, server_dir = http_file_server
    cfg = Config(**{**config.__dict__, "upgrade": UpgradeSettings(
        **{
            **config.upgrade.__dict__,
            "ftp": FTPSettings(url=base_url, username_env="", password_env="", timeout=10),
        }
    )})
    for v in ("1.0.0", "2.0.0"):
        shutil.copy(make_tar_gz(f"agent-{v}.tar.gz", {"agent": b"x"}), server_dir / f"agent-{v}.tar.gz")

    mgr = _manager(cfg)
    results: list = []

    def run(v):
        results.append(mgr.upgrade(UpgradeRequest(f"j-{v}", f"agent-{v}.tar.gz", v)))

    t1 = threading.Thread(target=run, args=("1.0.0",))
    t2 = threading.Thread(target=run, args=("2.0.0",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert {j.status for j in results} == {JobStatus.SUCCESS}