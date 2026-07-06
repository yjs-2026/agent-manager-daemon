"""OS user authentication + password change.

Linux only. This module is the security boundary for the web UI:

  * ``authenticate(user, password)`` — verifies a username/password pair
    against ``/etc/shadow`` using :func:`crypt.crypt`. Constant-time
    comparison via :func:`hmac.compare_digest`. No subprocess, no PAM
    stack calls, no shell injection surface.

  * ``change_password(user, new_password)`` — replaces the password for
    an existing local account via ``chpasswd``. chpasswd reads
    ``user:password`` lines on stdin and writes the new hash through the
    standard libc ``shadow`` APIs (handles /etc/shadow locking and
    aging metadata correctly). We never write to ``/etc/shadow``
    directly.

  * ``user_exists(user)`` — does the account appear in ``/etc/passwd``.
    Used both for the web login allow-list and to refuse the password
    change endpoint before we shell out.

Validation rules for new passwords are intentionally conservative. They
are *not* a substitute for PAM quality checks (cracklib, passwdqc);
daemon users behind ``pam_pwquality`` get those checks automatically.
"""

from __future__ import annotations

import crypt
import hmac
import os
import pwd
import re
import spwd
import subprocess
from dataclasses import dataclass
from typing import Optional

_MIN_PW_LEN = 8
_MAX_PW_LEN = 128
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Base class for authentication-related failures."""


class InvalidCredentials(AuthError):
    """Username/password did not match."""


class UserNotFound(AuthError):
    """Account does not exist on this host."""


class WeakPassword(AuthError):
    """Password failed local validation rules."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PasswordChangeFailed(AuthError):
    """``chpasswd`` returned non-zero."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthResult:
    """Return type for ``authenticate``."""

    user: str
    uid: int
    gid: int
    home: str
    shell: str


def authenticate(username: str, password: str) -> AuthResult:
    """Verify ``username``/``password`` against ``/etc/shadow``.

    Raises:
        UserNotFound: account does not exist.
        InvalidCredentials: account exists but the password is wrong,
            or the password field is locked (``!``/``*``).
    """
    username = _normalize_user(username)
    if not username:
        raise UserNotFound("empty username")

    try:
        shadow = spwd.getspnam(username)
    except KeyError as exc:
        raise UserNotFound(f"user {username!r} not found") from exc
    except PermissionError as exc:
        # /etc/shadow is mode 0640 root:shadow on most distros. The
        # daemon must run as root (it owns chpasswd/systemctl anyway).
        raise AuthError("daemon cannot read /etc/shadow — run as root") from exc

    stored = shadow.sp_pwd
    if not stored or stored in ("!", "*", "!!", "!"):
        raise InvalidCredentials("account is locked")

    # crypt(3) accepts the stored hash as the salt.
    candidate = crypt.crypt(password, stored)
    if not candidate or not hmac.compare_digest(candidate, stored):
        raise InvalidCredentials("invalid credentials")

    pw_entry = pwd.getpwnam(username)
    return AuthResult(
        user=username,
        uid=pw_entry.pw_uid,
        gid=pw_entry.pw_gid,
        home=pw_entry.pw_dir,
        shell=pw_entry.pw_shell,
    )


def change_password(username: str, new_password: str) -> None:
    """Set ``username``'s password to ``new_password``.

    Uses ``chpasswd`` so all the usual shadow-locking and aging rules
    apply. We deliberately do *not* pass the password on the argv, so
    it never appears in ``ps`` output.
    """
    username = _normalize_user(username)
    if not username:
        raise UserNotFound("empty username")
    if not user_exists(username):
        raise UserNotFound(f"user {username!r} not found")
    _validate_new_password(new_password)

    payload = f"{username}:{new_password}\n".encode("utf-8")
    proc = subprocess.run(
        ["chpasswd"],
        input=payload,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        raise PasswordChangeFailed(stderr or "chpasswd failed")


def user_exists(username: str) -> bool:
    username = _normalize_user(username)
    if not username:
        return False
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def validate_username(username: str) -> bool:
    """Cheap syntactic check; does *not* check /etc/passwd."""
    return bool(username) and bool(_USER_RE.match(username))


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _normalize_user(username: str) -> str:
    if username is None:
        return ""
    return username.strip()


def _validate_new_password(password: str) -> None:
    if password is None:
        raise WeakPassword("password is required")
    if len(password) < _MIN_PW_LEN:
        raise WeakPassword(f"password must be at least {_MIN_PW_LEN} characters")
    if len(password) > _MAX_PW_LEN:
        # chpasswd on glibc rejects long inputs; cap defensively.
        raise WeakPassword(f"password must be at most {_MAX_PW_LEN} characters")
    # Reject control chars / NUL — chpasswd would write garbage otherwise.
    if any(ord(c) < 0x20 for c in password):
        raise WeakPassword("password contains control characters")


def ensure_root() -> Optional[str]:
    """Return an error message if the current process is not root.

    Helper for the WSGI entry point and tests. Empty return = OK.
    """
    if os.geteuid() == 0:
        return None
    return (
        "agent-manager must run as root (needs read access to /etc/shadow "
        "and write access to chpasswd / systemctl)."
    )


__all__ = [
    "AuthError",
    "AuthResult",
    "InvalidCredentials",
    "PasswordChangeFailed",
    "UserNotFound",
    "WeakPassword",
    "authenticate",
    "change_password",
    "ensure_root",
    "user_exists",
    "validate_username",
]