"""Tests for ``agent_manager.auth``.

Exercises:
  * authenticate() happy path + InvalidCredentials + UserNotFound
  * change_password() happy path + WeakPassword + PasswordChangeFailed
  * ``chpasswd`` is invoked via stdin (no argv password leak)
"""

from __future__ import annotations

import subprocess

import pytest

from agent_manager import auth as auth_mod
from agent_manager.auth import (
    InvalidCredentials,
    PasswordChangeFailed,
    UserNotFound,
    WeakPassword,
    authenticate,
    change_password,
    user_exists,
    validate_username,
)


# ---------------------------------------------------------------------------
# authenticate
# ---------------------------------------------------------------------------


def test_authenticate_happy(monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    res = authenticate("alice", "hunter2")
    assert res.user == "alice"
    assert res.uid == 1000
    assert res.home == "/home/alice"


def test_authenticate_unknown_user(monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    with pytest.raises(UserNotFound):
        authenticate("bob", "whatever")


def test_authenticate_wrong_password(monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    with pytest.raises(InvalidCredentials):
        authenticate("alice", "WRONG")


def test_authenticate_locked_account(monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})

    # Re-patch spwd to return a locked shadow entry.
    class LockedShadow:
        sp_pwd = "!"
        sp_lstchg = sp_min = sp_max = sp_warn = sp_inact = sp_expire = sp_flag = 0

    monkeypatch.setattr(auth_mod.spwd, "getspnam", lambda _n: LockedShadow())
    with pytest.raises(InvalidCredentials):
        authenticate("alice", "hunter2")


# ---------------------------------------------------------------------------
# change_password
# ---------------------------------------------------------------------------


def test_change_password_happy(monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    seen = {}

    def fake_run(cmd, input=None, capture_output=False, check=False):
        seen["cmd"] = cmd
        seen["input"] = input
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(auth_mod.subprocess, "run", fake_run)
    change_password("alice", "new-password-1234")

    assert seen["cmd"] == ["chpasswd"]
    # Password must come via stdin, never argv.
    assert all("new-password-1234" not in str(a) for a in seen["cmd"])
    assert b"new-password-1234" in seen["input"]
    assert b"alice:" in seen["input"]


def test_change_password_weak(monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    with pytest.raises(WeakPassword):
        change_password("alice", "short")
    with pytest.raises(WeakPassword):
        change_password("alice", "x" * 200)
    with pytest.raises(WeakPassword):
        change_password("alice", "abc\x00defghij")


def test_change_password_unknown_user(monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    with pytest.raises(UserNotFound):
        change_password("bob", "longenoughpw")


def test_change_password_chpasswd_failure(monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})

    def fake_run(cmd, input=None, capture_output=False, check=False):
        return subprocess.CompletedProcess(cmd, 1, b"", b"authentication failure\n")

    monkeypatch.setattr(auth_mod.subprocess, "run", fake_run)
    with pytest.raises(PasswordChangeFailed):
        change_password("alice", "longenoughpw")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def test_user_exists(monkeypatch):
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    assert user_exists("alice") is True
    assert user_exists("bob") is False
    assert user_exists("") is False


def test_validate_username():
    assert validate_username("alice") is True
    assert validate_username("svc-deploy") is True
    assert validate_username("123root") is False  # must start with letter/underscore
    assert validate_username("") is False
    assert validate_username("a" * 64) is False