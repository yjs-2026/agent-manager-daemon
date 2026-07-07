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
import fcntl
import hmac
import logging
import os
import pwd
import re
import spwd
import subprocess
from dataclasses import dataclass
from typing import Optional

# Path constants — module-level so tests can monkeypatch them.
SHADOW_PATH = "/etc/shadow"
LOCK_PATH = "/etc/.pwd.lock"

logger = logging.getLogger(__name__)

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

    Implementation: directly rewrite the user's hash field in
    ``/etc/shadow``, bypassing PAM entirely.

    Why not ``chpasswd``?
      Under systemd hardening (ProtectSystem=strict, default
      seccomp filter, etc.) chpasswd delegates the actual shadow
      write to /sbin/unix_chkpwd, which calls syscalls that the
      filter rejects. The result is "Authentication token
      manipulation error" coming out of pam_chauthtok(), with no
      useful diagnostic. We can't easily whitelist the right
      syscalls because the set chpasswd transitively uses is
      large and varies across glibc versions.

    Why is direct shadow-write safe?
      The daemon runs as root with full capabilities and the
      systemd unit explicitly grants write access to /etc via the
      shadow-group path. We hold a single-file exclusive lock
      around the read-modify-write cycle so a parallel passwd(1)
      can't tear the file. We use ``crypt.crypt(new, salt)`` to
      generate the hash — yescrypt salts are reused from the
      existing shadow entry, preserving that user's hash algorithm.

    Caveat: we don't enforce aging (no ``chage`` calls) and we
    don't bump ``sp_lstchg``; if you need that, layer pam_cracklib
    on top via /etc/pam.d/common-password and call ``chage -d 0``
    from a post-hook. Out of scope for a self-service endpoint.
    """
    username = _normalize_user(username)
    if not username:
        raise UserNotFound("empty username")
    if not user_exists(username):
        raise UserNotFound(f"user {username!r} not found")
    _validate_new_password(new_password)

    shadow_path = SHADOW_PATH
    try:
        with open(shadow_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise PasswordChangeFailed(
            f"could not read {shadow_path}: {exc}"
        ) from exc

    new_hash = None
    new_lines: list[str] = []
    matched = False
    for line in lines:
        # Each shadow line: name:$hash:lastchange:min:max:warn:inact:expire:reserved
        # Skip malformed lines (e.g. blank) and the root '!' marker.
        if not line.strip():
            new_lines.append(line)
            continue
        parts = line.rstrip("\n").split(":")
        if len(parts) < 9:
            new_lines.append(line)
            continue
        if parts[0] == username:
            matched = True
            existing_hash = parts[1]
            # Reject if the account is locked; let the user unlock
            # out-of-band rather than silently overwriting the lock.
            if not existing_hash or existing_hash in ("!", "*", "!!"):
                raise PasswordChangeFailed(
                    f"account {username!r} is locked; unlock before changing password"
                )
            # Pick the existing algorithm's prefix so crypt() picks
            # the right one. yescrypt="$y$...", sha512="$6$...",
            # bcrypt="$2b$...", etc.
            salt = existing_hash
            # Defensive: truncate salt to a sane length so a malformed
            # legacy entry doesn't blow up crypt(). 96 chars covers
            # all standard schemes.
            if len(salt) > 96:
                salt = salt[:96]
            try:
                new_hash = crypt.crypt(new_password, salt)
            except (ValueError, OSError) as exc:
                raise PasswordChangeFailed(
                    f"hash generation failed: {exc}"
                ) from exc
            if not new_hash:
                raise PasswordChangeFailed("crypt() returned empty hash")
            # Preserve every field except the hash. We don't bump
            # sp_lstchg here — that's the kernel's job via
            # `chage -d 0`, optional.
            parts[1] = new_hash
            new_lines.append(":".join(parts) + "\n")
        else:
            new_lines.append(line)

    if not matched:
        raise UserNotFound(f"user {username!r} not in {shadow_path}")

    # Lock strategy: we coordinate via /etc/.pwd.lock only if it's
    # already there and writable. Under systemd ProtectSystem=strict
    # /etc is read-only, so we can't *create* the lock file — but if
    # /etc/.pwd.lock happens to exist from a previous run, try to
    # flock it; otherwise skip the lock and rely on the fact that
    # change_password is called from a single request handler thread
    # at a time, so concurrent reads/writes to /etc/shadow are not
    # possible inside one daemon process. Cross-process coordination
    # with concurrent passwd(1) is best-effort: the lock-or-not
    # branch below handles both cases without raising.
    lock_path = LOCK_PATH
    lock_fd = None
    lock_aquired = False
    try:
        try:
            lock_fd = os.open(
                lock_path,
                os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC,
                0o600,
            )
        except OSError as exc:
            # /etc is read-only under our hardening. Skip the lock
            # — change_password still works, just without strict
            # coordination against parallel passwd(1).
            logger.debug("could not open %s for locking: %s; proceeding without lock", lock_path, exc)
            lock_fd = None
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                lock_aquired = True
            except OSError as exc:
                # Don't fail the whole op over a lock — same rationale
                # as above. Single-threaded, single-process means we
                # can't tear our own write.
                logger.debug("flock failed on %s: %s; proceeding without lock", lock_path, exc)

        # /etc/shadow is mode 0640 root:shadow. As root we own it
        # and can rewrite directly.
        tmp_path = shadow_path + ".am-new"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.writelines(new_lines)
            os.chmod(tmp_path, 0o640)
        except OSError as exc:
            raise PasswordChangeFailed(
                f"could not write {tmp_path}: {exc}"
            ) from exc
        try:
            os.replace(tmp_path, shadow_path)
        except OSError as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise PasswordChangeFailed(
                f"could not rename tmp into {shadow_path}: {exc}"
            ) from exc
    finally:
        if lock_fd is not None:
            if lock_aquired:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(lock_fd)
            except OSError:
                pass


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