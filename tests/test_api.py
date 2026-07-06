"""End-to-end tests for the HTTP API + Web UI blueprints.

Auth is exercised against a faked /etc/shadow. Upgrade endpoints use
the same HTTP file-server fixture as test_upgrade.py.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_manager import auth as auth_mod


# ---------------------------------------------------------------------------
# Web: login + change password
# ---------------------------------------------------------------------------


def _login(client, username, password):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def _logout(client):
    return client.post("/logout", follow_redirects=False)


def test_web_login_get_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert b"Sign in" in r.data


def test_web_login_happy_redirects_to_password_page(client, monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    r = _login(client, "alice", "hunter2")
    assert r.status_code == 302
    assert "/account/password" in r.headers["Location"]


def test_web_login_rejects_bad_password(client, monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    r = _login(client, "alice", "wrong")
    assert r.status_code == 200
    assert b"Invalid credentials" in r.data


def test_web_change_password_requires_login(client):
    r = client.get("/account/password", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_web_change_password_happy(client, monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    _login(client, "alice", "hunter2")

    # Patch chpasswd to record the call.
    seen = {}

    def fake_run(cmd, input=None, capture_output=False, check=False):
        seen["input"] = input
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(auth_mod.subprocess, "run", fake_run)

    r = client.post(
        "/account/password",
        data={
            "current_password": "hunter2",
            "new_password": "new-secret-1234",
            "confirm_password": "new-secret-1234",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert b"Password updated successfully" in r.data
    assert b"new-secret-1234" in seen["input"]


def test_web_change_password_wrong_current(client, monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    _login(client, "alice", "hunter2")
    r = client.post(
        "/account/password",
        data={
            "current_password": "WRONG",
            "new_password": "new-secret-1234",
            "confirm_password": "new-secret-1234",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert b"Current password is incorrect" in r.data


def test_web_change_password_mismatch(client, monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    _login(client, "alice", "hunter2")
    r = client.post(
        "/account/password",
        data={
            "current_password": "hunter2",
            "new_password": "new-secret-1234",
            "confirm_password": "DIFFERENT",
        },
        follow_redirects=True,
    )
    assert b"do not match" in r.data


def test_web_change_password_weak(client, monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    _login(client, "alice", "hunter2")
    r = client.post(
        "/account/password",
        data={
            "current_password": "hunter2",
            "new_password": "short",
            "confirm_password": "short",
        },
        follow_redirects=True,
    )
    assert b"Weak password" in r.data


# ---------------------------------------------------------------------------
# API: token enforcement
# ---------------------------------------------------------------------------


def _auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def test_api_health_requires_token(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_api_health_happy(client):
    r = client.get("/api/v1/health", headers=_auth_header("test-token"))
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok"


def test_api_health_invalid_token(client):
    r = client.get("/api/v1/health", headers=_auth_header("not-a-real-token"))
    assert r.status_code == 401


def test_api_health_malformed_authorization(client):
    r = client.get("/api/v1/health", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# API: upgrade lifecycle
# ---------------------------------------------------------------------------


def _wait_for_terminal(client, job_id: str, timeout: float = 5.0):
    """Poll GET /upgrades/<id> until status is terminal or timeout."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/v1/upgrades/{job_id}", headers=_auth_header("test-token"))
        if r.status_code != 200:
            time.sleep(0.05)
            continue
        body = r.get_json()
        if body["status"] in ("success", "failed", "rolled_back"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_api_create_upgrade_happy(client, http_file_server, make_tar_gz, tmp_path: Path):
    base_url, server_dir = http_file_server
    archive = make_tar_gz("agent-1.0.0.tar.gz", {"agent": b"x"})
    shutil.copy(archive, server_dir / "agent-1.0.0.tar.gz")

    from agent_manager.config import Config, FTPSettings, UpgradeSettings

    app = client.application
    cfg: Config = app.config["AGENT_MANAGER_CONFIG"]
    new_cfg = Config(**{**cfg.__dict__, "upgrade": UpgradeSettings(
        **{
            **cfg.upgrade.__dict__,
            "ftp": FTPSettings(url=base_url, username_env="", password_env="", timeout=10),
        }
    )})
    # Mutate the app's stored config + manager so requests see it.
    app.config["AGENT_MANAGER_CONFIG"] = new_cfg
    from agent_manager.upgrade import UpgradeManager
    app.extensions["agent_manager.upgrade_manager"] = UpgradeManager(
        cfg=new_cfg, registry=app.extensions["agent_manager.job_registry"]
    )

    r = client.post(
        "/api/v1/upgrades",
        json={
            "job_id": "api-j-1",
            "filename": "agent-1.0.0.tar.gz",
            "version": "1.0.0",
        },
        headers=_auth_header("test-token"),
    )
    assert r.status_code == 202
    body = r.get_json()
    assert body["job_id"] == "api-j-1"
    assert body["status"] == "pending"

    final = _wait_for_terminal(client, "api-j-1")
    assert final["status"] == "success"


def test_api_create_upgrade_validation_errors(client):
    r = client.post(
        "/api/v1/upgrades",
        json={"job_id": "x"},  # missing fields
        headers=_auth_header("test-token"),
    )
    assert r.status_code == 400


def test_api_create_upgrade_rejects_duplicate_job(client):
    r1 = client.post(
        "/api/v1/upgrades",
        json={"job_id": "dup", "filename": "a.tar.gz", "version": "1"},
        headers=_auth_header("test-token"),
    )
    # First one starts running; subsequent should 409.
    assert r1.status_code == 202
    r2 = client.post(
        "/api/v1/upgrades",
        json={"job_id": "dup", "filename": "a.tar.gz", "version": "2"},
        headers=_auth_header("test-token"),
    )
    assert r2.status_code == 409


def test_api_get_unknown_job(client):
    r = client.get("/api/v1/upgrades/nope", headers=_auth_header("test-token"))
    assert r.status_code == 404


def test_api_list_returns_jobs(client):
    client.post(
        "/api/v1/upgrades",
        json={"job_id": "listjob", "filename": "a.tar.gz", "version": "1"},
        headers=_auth_header("test-token"),
    )
    r = client.get("/api/v1/upgrades", headers=_auth_header("test-token"))
    assert r.status_code == 200
    body = r.get_json()
    assert any(j["job_id"] == "listjob" for j in body["upgrades"])