"""Flask application factory.

Wires the blueprints and the ``UpgradeManager`` extension. The
manager is built once at startup so its :class:`JobRegistry` picks up
the on-disk history; subsequent calls reuse the same instance via
``current_app.extensions``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from flask import Flask

from .api import bp as api_bp
from .config import Config
from .upgrade import UpgradeManager, build_registry
from .web import bp as web_bp


def create_app(config: Optional[Config] = None) -> Flask:
    cfg = config or Config.load()
    # Resolve templates/ and static/. Two cases, in priority order:
    #
    #   1. Next to this file (non-editable install):
    #        <prefix>/.venv/lib/python3.12/site-packages/agent_manager/
    #      install.sh copies templates/ and static/ here so the daemon
    #      can use a stable per-package path even when systemd sets
    #      WorkingDirectory= somewhere unrelated.
    #
    #   2. Relative to the source tree (editable dev install):
    #        src/agent_manager/app.py
    #        ../../templates  +  ../../static
    pkg_dir = Path(__file__).resolve().parent
    candidates_t = [pkg_dir / "templates", pkg_dir.parent.parent / "templates"]
    candidates_s = [pkg_dir / "static", pkg_dir.parent.parent / "static"]
    template_dir = next((p for p in candidates_t if p.is_dir()), candidates_t[0])
    static_dir = next((p for p in candidates_s if p.is_dir()), candidates_s[0])

    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir),
    )
    app.config.update(cfg.as_flask_config())

    # Build the upgrade subsystem once and stash it on extensions.
    registry = build_registry(cfg)
    manager = UpgradeManager(cfg=cfg, registry=registry)
    app.extensions["agent_manager.upgrade_manager"] = manager
    app.extensions["agent_manager.job_registry"] = registry

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp)

    return app


__all__ = ["create_app"]