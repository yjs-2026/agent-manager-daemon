"""Configuration loading.

Reads `config.yaml` from one of (in priority order):

  1. Path passed to ``Config.load(path=...)``
  2. $AGENT_MANAGER_CONFIG env var
  3. ./config.yaml (cwd)
  4. /etc/agent-manager/config.yaml

Each top-level YAML key may be overridden by an env var of the form
``AGENT_MANAGER_<UPPER_SNAKE_PATH>``. Examples:

    AGENT_MANAGER_SERVER__BIND_PORT=9090
    AGENT_MANAGER_UPGRADE__FTP__URL=ftp://new-host/path

Env vars use ``__`` as the path separator so nested dicts can be
addressed without ambiguity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_CANDIDATES = (
    "./config.yaml",
    "/etc/agent-manager/config.yaml",
)
_ENV_PATH = "AGENT_MANAGER_CONFIG"


@dataclass(frozen=True)
class FTPSettings:
    url: str
    username_env: str = "AGENT_MANAGER_FTP_USER"
    password_env: str = "AGENT_MANAGER_FTP_PASS"
    timeout: int = 60
    verify_tls: bool = True

    def username(self) -> str:
        return os.environ.get(self.username_env, "anonymous")

    def password(self) -> str:
        return os.environ.get(self.password_env, "")


@dataclass(frozen=True)
class UpgradeSettings:
    work_dir: str
    install_root: str
    keep_releases: int = 3
    systemd_unit: str = ""
    ftp: FTPSettings = field(default_factory=lambda: FTPSettings(url=""))
    archive_formats: tuple[str, ...] = (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".zip")
    post_install_hook: str = ""


@dataclass(frozen=True)
class ServerSettings:
    bind_host: str = "127.0.0.1"
    bind_port: int = 8088
    secret_key: str = "change-me"
    session_cookie_secure: bool = False
    session_cookie_httponly: bool = True
    session_cookie_samesite: str = "Lax"
    web_allowed_users: tuple[str, ...] = ()


@dataclass(frozen=True)
class TLSSettings:
    enabled: bool = True
    mode: str = "adhoc"  # "adhoc" | "explicit"
    certfile: str = ""
    keyfile: str = ""
    min_version: str = "TLSv1.2"


@dataclass(frozen=True)
class APISettings:
    tokens: tuple[str, ...] = ()
    require_token: bool = True


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "INFO"
    file: str = ""
    format: str = "%(asctime)s %(levelname)s %(name)s %(message)s"


@dataclass(frozen=True)
class Config:
    server: ServerSettings
    api: APISettings
    upgrade: UpgradeSettings
    logging: LoggingSettings
    tls: TLSSettings
    source_path: str = ""

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        chosen = _resolve_config_path(path)
        with open(chosen, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        raw = _apply_env_overrides(raw)
        return _build(chosen, raw)

    def as_flask_config(self) -> dict[str, Any]:
        """Flask-friendly dict for app.config.update(...)."""
        return {
            "SECRET_KEY": self.server.secret_key,
            "SESSION_COOKIE_SECURE": self.server.session_cookie_secure,
            "SESSION_COOKIE_HTTPONLY": self.server.session_cookie_httponly,
            "SESSION_COOKIE_SAMESITE": self.server.session_cookie_samesite,
            # Application-level config (consumed by our blueprints, not Flask).
            "AGENT_MANAGER_CONFIG": self,
        }


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _resolve_config_path(path: str | None) -> str:
    candidates: list[str] = []
    if path:
        candidates.append(path)
    env_path = os.environ.get(_ENV_PATH)
    if env_path:
        candidates.append(env_path)
    candidates.extend(_DEFAULT_CANDIDATES)
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise FileNotFoundError(
        "config.yaml not found. Searched: "
        + ", ".join(candidates)
    )


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Walk env vars and inject matching values into the config tree."""
    out = dict(data)
    prefix = "AGENT_MANAGER_"
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix) or env_key == _ENV_PATH:
            continue
        path = env_key[len(prefix):].lower().split("__")
        if not path:
            continue
        _set_nested(out, path, env_val)
    return out


def _set_nested(data: dict[str, Any], path: list[str], value: str) -> None:
    cur = data
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    # Try to keep ints as ints; everything else stays as str.
    cur[path[-1]] = _coerce(value)


def _coerce(value: str) -> Any:
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    return value


def _build(source_path: str, raw: dict[str, Any]) -> Config:
    server_raw = raw.get("server", {}) or {}
    api_raw = raw.get("api", {}) or {}
    upgrade_raw = raw.get("upgrade", {}) or {}
    logging_raw = raw.get("logging", {}) or {}
    tls_raw = raw.get("tls", {}) or {}

    ftp_raw = upgrade_raw.get("ftp", {}) or {}
    ftp = FTPSettings(
        url=ftp_raw.get("url", ""),
        username_env=ftp_raw.get("username_env", "AGENT_MANAGER_FTP_USER"),
        password_env=ftp_raw.get("password_env", "AGENT_MANAGER_FTP_PASS"),
        timeout=int(ftp_raw.get("timeout", 60)),
        verify_tls=bool(ftp_raw.get("verify_tls", True)),
    )

    upgrade = UpgradeSettings(
        work_dir=str(upgrade_raw.get("work_dir", "/var/lib/agent-manager/work")),
        install_root=str(upgrade_raw.get("install_root", "/opt/myagent")),
        keep_releases=int(upgrade_raw.get("keep_releases", 3)),
        systemd_unit=str(upgrade_raw.get("systemd_unit", "")),
        ftp=ftp,
        archive_formats=tuple(upgrade_raw.get(
            "archive_formats",
            [".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".zip"],
        )),
        post_install_hook=str(upgrade_raw.get("post_install_hook", "")),
    )

    server = ServerSettings(
        bind_host=str(server_raw.get("bind_host", "127.0.0.1")),
        bind_port=int(server_raw.get("bind_port", 8088)),
        secret_key=str(server_raw.get("secret_key", "change-me")),
        session_cookie_secure=bool(server_raw.get("session_cookie_secure", False)),
        session_cookie_httponly=bool(server_raw.get("session_cookie_httponly", True)),
        session_cookie_samesite=str(server_raw.get("session_cookie_samesite", "Lax")),
        web_allowed_users=tuple(server_raw.get("web_allowed_users", []) or []),
    )

    api = APISettings(
        tokens=tuple(api_raw.get("tokens", []) or []),
        require_token=bool(api_raw.get("require_token", True)),
    )

    logging_cfg = LoggingSettings(
        level=str(logging_raw.get("level", "INFO")),
        file=str(logging_raw.get("file", "")),
        format=str(logging_raw.get("format", "%(asctime)s %(levelname)s %(name)s %(message)s")),
    )

    tls = TLSSettings(
        enabled=bool(tls_raw.get("enabled", True)),
        mode=str(tls_raw.get("mode", "adhoc")).lower(),
        certfile=str(tls_raw.get("certfile", "")),
        keyfile=str(tls_raw.get("keyfile", "")),
        min_version=str(tls_raw.get("min_version", "TLSv1.2")),
    )
    if tls.mode not in ("adhoc", "explicit"):
        raise ValueError(
            f"tls.mode must be 'adhoc' or 'explicit', got {tls.mode!r}"
        )
    if tls.mode == "explicit":
        if not tls.certfile or not tls.keyfile:
            raise ValueError(
                "tls.mode=explicit requires both tls.certfile and tls.keyfile"
            )

    return Config(
        server=server,
        api=api,
        upgrade=upgrade,
        logging=logging_cfg,
        tls=tls,
        source_path=source_path,
    )


__all__ = [
    "Config",
    "ServerSettings",
    "APISettings",
    "UpgradeSettings",
    "FTPSettings",
    "LoggingSettings",
    "TLSSettings",
]