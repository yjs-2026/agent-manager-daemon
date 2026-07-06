"""Flask application factory.

Wires the blueprints and the ``UpgradeManager`` extension. The
manager is built once at startup so its :class:`JobRegistry` picks up
the on-disk history; subsequent calls reuse the same instance via
``current_app.extensions``.
"""

from __future__ import annotations

from typing import Optional

from flask import Flask

from .api import bp as api_bp
from .config import Config
from .upgrade import UpgradeManager, build_registry
from .web import bp as web_bp


def create_app(config: Optional[Config] = None) -> Flask:
    cfg = config or Config.load()
    app = Flask(
        __name__,
        template_folder="../../templates",
        static_folder="../../static",
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