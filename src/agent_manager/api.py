"""Public HTTP API for agent lifecycle management.

Endpoints (all under ``/api/v1``):

    GET  /health                       — liveness probe
    POST /upgrades                     — kick off an upgrade (returns job_id)
    GET  /upgrades                     — list recent jobs
    GET  /upgrades/<job_id>            — job status + log
    POST /upgrades/<job_id>/rollback   — point current symlink at the previous release

All endpoints require ``Authorization: Bearer <token>`` (see
:mod:`agent_manager.api_auth`) unless ``api.require_token`` is false.
"""

from __future__ import annotations

import threading
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from .api_auth import require_api_token
from .upgrade import UpgradeManager, UpgradeRequest

bp = Blueprint("api", __name__, url_prefix="/api/v1")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cfg() -> Any:
    return current_app.config["AGENT_MANAGER_CONFIG"]


def _manager() -> UpgradeManager:
    return current_app.extensions["agent_manager.upgrade_manager"]


def _err(msg: str, code: int = 400):
    resp = jsonify({"error": msg})
    resp.status_code = code
    return resp


def _validate_upgrade_payload(data: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Return (errors, normalized). Errors are non-empty on failure."""
    errors: dict[str, str] = {}
    if not isinstance(data, dict):
        return {"body": "expected JSON object"}, {}
    out: dict[str, str] = {}
    for field in ("job_id", "filename", "version"):
        v = data.get(field)
        if not isinstance(v, str) or not v.strip():
            errors[field] = "required"
        else:
            out[field] = v.strip()
    ftp_url = data.get("ftp_url")
    if ftp_url is not None:
        if not isinstance(ftp_url, str) or not ftp_url.strip():
            errors["ftp_url"] = "must be a non-empty string when provided"
        else:
            out["ftp_url"] = ftp_url.strip()
    return errors, out


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@bp.get("/health")
@require_api_token
def health():
    cfg = _cfg()
    return jsonify(
        {
            "status": "ok",
            "version": "0.1.0",
            "install_root": cfg.upgrade.install_root,
            "systemd_unit": cfg.upgrade.systemd_unit,
        }
    )


@bp.post("/upgrades")
@require_api_token
def create_upgrade():
    payload = request.get_json(silent=True)
    errors, normalized = _validate_upgrade_payload(payload)
    if errors:
        return _err(f"invalid payload: {errors}", 400)

    mgr = _manager()
    try:
        # If a job with this id is already terminal, refuse — operators
        # should pick a fresh id. If it's in flight, also refuse.
        existing = mgr._registry.get(normalized["job_id"])
    except Exception:  # noqa: BLE001
        existing = None
    if existing is not None:
        return _err(
            f"job_id {normalized['job_id']!r} already exists (status={existing.status.value})",
            409,
        )

    req = UpgradeRequest(
        job_id=normalized["job_id"],
        filename=normalized["filename"],
        version=normalized["version"],
        ftp_url=normalized.get("ftp_url", ""),
    )

    # Run the upgrade in a worker thread so the HTTP call returns
    # immediately with the job_id. The client polls GET /upgrades/<id>
    # to follow progress.
    thread = threading.Thread(
        target=_safe_run_upgrade,
        args=(mgr, req),
        name=f"upgrade-{req.job_id}",
        daemon=True,
    )
    thread.start()

    resp = jsonify(
        {
            "job_id": req.job_id,
            "status": "pending",
            "poll_url": f"/api/v1/upgrades/{req.job_id}",
        }
    )
    resp.status_code = 202
    return resp


@bp.get("/upgrades")
@require_api_token
def list_upgrades():
    mgr = _manager()
    jobs = mgr._registry.list()
    return jsonify({"upgrades": [j.to_dict() for j in jobs]})


@bp.get("/upgrades/<job_id>")
@require_api_token
def get_upgrade(job_id: str):
    mgr = _manager()
    job = mgr._registry.get(job_id)
    if job is None:
        return _err(f"no such job: {job_id}", 404)
    return jsonify(job.to_dict())


@bp.post("/upgrades/<job_id>/rollback")
@require_api_token
def rollback_upgrade(job_id: str):
    mgr = _manager()
    if mgr._registry.get(job_id) is None:
        return _err(f"no such job: {job_id}", 404)

    thread = threading.Thread(
        target=_safe_run_rollback,
        args=(mgr, job_id),
        name=f"rollback-{job_id}",
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id, "status": "pending"}), 202


# ---------------------------------------------------------------------------
# worker thread wrappers — keep exceptions from killing the daemon silently
# ---------------------------------------------------------------------------


def _safe_run_upgrade(mgr: UpgradeManager, req: UpgradeRequest) -> None:
    try:
        mgr.upgrade(req)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("upgrade worker crashed for %s", req.job_id)


def _safe_run_rollback(mgr: UpgradeManager, job_id: str) -> None:
    try:
        mgr.rollback(job_id)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("rollback worker crashed for %s", job_id)


__all__ = ["bp"]