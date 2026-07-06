"""Shared pytest fixtures.

The whole upgrade/auth machinery is rooted in OS primitives
(/etc/shadow, FTP, systemctl) that don't exist on most test hosts.
We patch those at the module boundary so each test exercises only the
daemon's logic.
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from agent_manager import auth as auth_mod
from agent_manager.api_auth import hash_token
from agent_manager.app import create_app
from agent_manager.config import (
    APISettings,
    Config,
    FTPSettings,
    LoggingSettings,
    ServerSettings,
    TLSSettings,
    UpgradeSettings,
)


# ---------------------------------------------------------------------------
# shadow / pwd fixtures
# ---------------------------------------------------------------------------


@dataclass
class FakeShadow:
    sp_pwd: str
    sp_lstchg: int = 0
    sp_min: int = 0
    sp_max: int = 99999
    sp_warn: int = 7
    sp_inact: int = -1
    sp_expire: int = -1
    sp_flag: int = 0


class FakePwdEntry:
    def __init__(self, name: str, uid: int, gid: int, home: str, shell: str) -> None:
        self.pw_name = name
        self.pw_uid = uid
        self.pw_gid = gid
        self.pw_dir = home
        self.pw_shell = shell
        self.pw_passwd = "x"
        self.pw_gecos = ""
        self._tuple = (name, "x", uid, gid, "", home, shell)

    def __getitem__(self, idx):
        return self._tuple[idx]


def install_fake_shadow(monkeypatch: pytest.MonkeyPatch, users: dict[str, str]) -> None:
    """Install a fake /etc/passwd + /etc/shadow.

    ``users`` maps username -> plaintext password. The fake uses crypt
    with the standard $6$ (SHA-512) prefix and a fixed salt so we can
    re-derive hashes deterministically.
    """
    pw_table: dict[str, FakePwdEntry] = {}
    sh_table: dict[str, FakeShadow] = {}
    for i, (uname, plaintext) in enumerate(users.items()):
        pw_table[uname] = FakePwdEntry(uname, 1000 + i, 1000 + i, f"/home/{uname}", "/bin/bash")
        import crypt as _crypt
        salt = "$6$testsalt$"
        sh_table[uname] = FakeShadow(_crypt.crypt(plaintext, salt))

    def getpwnam(name: str):
        if name not in pw_table:
            raise KeyError(name)
        return pw_table[name]

    def getspnam(name: str):
        if name not in sh_table:
            raise KeyError(name)
        return sh_table[name]

    monkeypatch.setattr(auth_mod.pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(auth_mod.spwd, "getspnam", getspnam)


# ---------------------------------------------------------------------------
# config + app fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_workdir(tmp_path: Path) -> Path:
    work = tmp_path / "agent-mgr"
    work.mkdir()
    return work


@pytest.fixture
def config(tmp_workdir: Path) -> Config:
    return Config(
        server=ServerSettings(
            bind_host="127.0.0.1",
            bind_port=0,
            secret_key="test-secret-key-do-not-use-in-prod",
            session_cookie_secure=False,
            session_cookie_httponly=True,
            session_cookie_samesite="Lax",
            web_allowed_users=(),
        ),
        api=APISettings(
            tokens=(hash_token("test-token"),),
            require_token=True,
        ),
        upgrade=UpgradeSettings(
            work_dir=str(tmp_workdir / "work"),
            install_root=str(tmp_workdir / "opt" / "myagent"),
            keep_releases=2,
            systemd_unit="",
            ftp=FTPSettings(
                url="http://127.0.0.1:0/agents",
                username_env="AGENT_MANAGER_FTP_USER",
                password_env="AGENT_MANAGER_FTP_PASS",
                timeout=10,
                verify_tls=False,
            ),
            archive_formats=(".tar.gz", ".tgz", ".zip"),
            post_install_hook="",
        ),
        logging=LoggingSettings(level="WARNING", file="", format="%(message)s"),
        tls=TLSSettings(enabled=False, mode="adhoc"),
        source_path="<test>",
    )


@pytest.fixture
def app(config: Config):
    flask_app = create_app(config)
    flask_app.config.update(TESTING=True)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# local HTTP file server (used as a stand-in for an FTP server)
# ---------------------------------------------------------------------------


@pytest.fixture
def http_file_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Serve ``tmp_path`` over HTTP. Yields (base_url, served_dir)."""
    served_dir = tmp_path / "ftp_root"
    served_dir.mkdir()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            rel = self.path.split("?", 1)[0].lstrip("/")
            target = (served_dir / rel).resolve()
            try:
                target.relative_to(served_dir.resolve())
            except ValueError:
                self.send_error(403, "forbidden")
                return
            if not target.is_file():
                self.send_error(404, "not found")
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_a, **_kw):  # silence test logs
            return

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, served_dir
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# misc helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def make_tar_gz(tmp_path: Path):
    """Factory: write a small .tar.gz under ``tmp_path / name``."""

    def _make(name: str, members: dict[str, bytes]) -> Path:
        import io
        import tarfile

        archive = tmp_path / name
        with tarfile.open(archive, "w:gz") as tf:
            for arcname, data in members.items():
                info = tarfile.TarInfo(name=arcname)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        return archive

    return _make


@pytest.fixture
def fake_clock():
    """Deterministic clock for job timestamps."""
    base = [0.0]

    def clock():
        import datetime as _dt
        base[0] += 1
        return _dt.datetime(2025, 1, 1, 0, 0, int(base[0]), tzinfo=_dt.timezone.utc)

    return clock