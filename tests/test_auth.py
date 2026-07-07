"""Tests for ``agent_manager.auth``.

Exercises:
  * authenticate() happy path + InvalidCredentials + UserNotFound
  * change_password() happy path + WeakPassword + PasswordChangeFailed
  * The new /etc/shadow direct-write path (replaces chpasswd)
"""

from __future__ import annotations

import builtins
from io import StringIO
from tempfile import NamedTemporaryFile

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
# authenticate (unchanged)
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

    class LockedShadow:
        sp_pwd = "!"
        sp_lstchg = sp_min = sp_max = sp_warn = sp_inact = sp_expire = sp_flag = 0

    monkeypatch.setattr(auth_mod.spwd, "getspnam", lambda _n: LockedShadow())
    with pytest.raises(InvalidCredentials):
        authenticate("alice", "hunter2")


# ---------------------------------------------------------------------------
# change_password — direct /etc/shadow rewrite
# ---------------------------------------------------------------------------


def _install_shadow_open(monkeypatch, lines):
    """Patch builtins.open so change_password reads from a fake
    /etc/shadow and writes to a tmp file. The contents that
    change_password tried to write can be inspected by reading the
    tmp file path we capture."""
    import os as _os

    real_open = builtins.open
    captured: dict = {}

    def fake_open(path, mode="r", *args, **kwargs):
        p = str(path)
        # Match /etc/shadow and the in-progress tmp file we create.
        if p == "/etc/shadow" or p == "/etc/.pwd.lock" or p.endswith("/etc/shadow.am-new"):
            if "r" in mode and not _os.path.exists(p) and p.endswith("/etc/shadow.am-new"):
                # Reading a tmp that didn't get created yet = empty.
                return StringIO("")
            if "r" in mode:
                return StringIO("".join(lines) if isinstance(lines, list) else lines)
            tmp = NamedTemporaryFile(
                mode="w" if "b" not in mode else "wb",
                delete=False,
                prefix="am-shadow-",
            )
            captured["tmp_path"] = tmp.name
            captured["tmp_mode"] = mode
            captured.setdefault("writes", []).append(tmp.name)
            return tmp
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    return captured


def test_change_password_happy(monkeypatch, tmp_path):
    """Round-trip: existing alice hash, change her password, verify
    the new plaintext authenticates.

    We do this end-to-end against real tmp_path files (no mocking of
    open/os.replace). change_password reads /etc/shadow, so we
    monkeypatch auth.SHADOW_PATH to a tmp file.
    """
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})

    import crypt as _crypt

    shadow_file = tmp_path / "shadow"
    alice_old = _crypt.crypt("hunter2", "$6$testsalt$")
    shadow_file.write_text(
        f"root:$6$othersalt$xyz:19000:0:99999:7:::\n"
        f"alice:{alice_old}:19000:0:99999:7:::\n"
    )
    lock_file = tmp_path / ".pwd.lock"
    lock_file.write_text("")  # content irrelevant; fcntl.flock handles it

    monkeypatch.setattr(auth_mod, "SHADOW_PATH", str(shadow_file))
    monkeypatch.setattr(auth_mod, "LOCK_PATH", str(lock_file))

    change_password("alice", "new-password-1234")

    # Verify the new shadow has alice's hash updated and root unchanged.
    new = shadow_file.read_text().splitlines()
    root_line = next(line for line in new if line.startswith("root:"))
    alice_line = next(line for line in new if line.startswith("alice:"))
    assert "othersalt" in root_line, "root was touched"
    parts = alice_line.split(":")
    assert parts[0] == "alice"
    assert parts[1] != alice_old, "hash didn't change"
    assert parts[1].startswith("$6$"), f"expected sha512 salt prefix, got {parts[1][:6]!r}"

    # And the new hash should authenticate alice with the new pwd.
    monkeypatch.setattr(auth_mod.spwd, "getspnam", lambda _n: type(
        "S",
        (),
        {"sp_pwd": parts[1], "sp_lstchg": 0, "sp_min": 0, "sp_max": 0,
         "sp_warn": 0, "sp_inact": 0, "sp_expire": 0, "sp_flag": 0},
    )())
    res = authenticate("alice", "new-password-1234")
    assert res.user == "alice"


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
    _install_shadow_open(monkeypatch, [])
    with pytest.raises(UserNotFound):
        change_password("bob", "longenoughpw")


def test_change_password_locked_account(monkeypatch):
    """If the existing shadow hash is '!' / '*' / '!!', refuse to
    overwrite — that would silently unlock a locked account."""
    install = __import__("tests.conftest", fromlist=["install_fake_shadow"]).install_fake_shadow
    install(monkeypatch, {"alice": "hunter2"})
    _install_shadow_open(monkeypatch, ["alice:!:19000:0:99999:7:::\n"])
    with pytest.raises(PasswordChangeFailed, match="locked"):
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
    assert validate_username("123root") is False
    assert validate_username("") is False
    assert validate_username("a" * 64) is False