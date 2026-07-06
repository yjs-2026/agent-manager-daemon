"""API token authentication.

Tokens are configured in ``config.yaml`` as **SHA-256 hex digests** of
the real bearer strings. The real strings never appear in the config
file. Callers send ``Authorization: Bearer <token>``; we hash and
constant-time-compare against the configured digests.

Why hashes-only-at-rest?
  * Operators can commit ``config.yaml`` to a config-mgmt repo without
    leaking prod tokens.
  * ``ps``/core dumps never reveal raw tokens.
  * Rotating a token = regenerate the hash and add it to the list;
    no plaintext comparison step is ever needed.
"""

from __future__ import annotations

import hashlib
import hmac
from functools import wraps
from typing import Iterable, Optional

from flask import current_app, jsonify, request


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of ``token``."""
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _extract_bearer() -> Optional[str]:
    """Pull ``Bearer <token>`` from the Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _configured_hashes() -> tuple[str, ...]:
    cfg = current_app.config["AGENT_MANAGER_CONFIG"]
    return tuple(cfg.api.tokens)


def _require_token_enabled() -> bool:
    cfg = current_app.config["AGENT_MANAGER_CONFIG"]
    return bool(cfg.api.require_token)


def require_api_token(view):
    """Decorator: reject with 401 unless the request bears a valid token.

    If ``api.require_token`` is false the decorator is a no-op (useful
    for local dev). With the default config (``require_token: true``)
    every /api/v1/* call must present a valid Bearer token.
    """

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _require_token_enabled():
            return view(*args, **kwargs)

        token = _extract_bearer()
        if not token:
            return _unauth("missing bearer token")

        presented = hash_token(token)
        if not _matches(presented, _configured_hashes()):
            return _unauth("invalid bearer token")

        request.api_user = "token"  # type: ignore[attr-defined]
        return view(*args, **kwargs)

    return wrapper


def _matches(presented: str, hashes: Iterable[str]) -> bool:
    """Constant-time membership test."""
    presented_bytes = presented.encode("utf-8")
    for h in hashes:
        if hmac.compare_digest(presented_bytes, h.encode("utf-8")):
            return True
    return False


def _unauth(msg: str):
    resp = jsonify({"error": msg})
    resp.status_code = 401
    resp.headers["WWW-Authenticate"] = 'Bearer realm="agent-manager"'
    return resp


__all__ = ["hash_token", "require_api_token"]