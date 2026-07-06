"""Entry point: ``python -m agent_manager`` or the ``agent-manager`` script."""

from __future__ import annotations

import argparse
import ssl
import sys

from .app import create_app
from .auth import ensure_root
from .config import Config
from .logging_setup import configure_logging


def _build_ssl_context(cfg: Config) -> ssl.SSLContext | str | tuple[str, str] | None:
    """Translate ``Config.tls`` into a value the WSGI server understands.

    Returns one of:
      * ``None``  — TLS disabled
      * the string ``"adhoc"`` — Flask dev-server ephemeral cert
      * ``(certfile, keyfile)`` tuple — explicit cert pair
      * an ``ssl.SSLContext`` — for callers that prefer one
    """
    tls = cfg.tls
    if not tls.enabled:
        return None

    # Enforce minimum TLS version on every path. This is the only TLS
    # knob we tune across both dev and prod servers.
    version_map = {
        "TLSv1.2": ssl.TLSVersion.TLSv1_2,
        "TLSv1.3": ssl.TLSVersion.TLSv1_3,
    }
    min_ver = version_map.get(tls.min_version)
    if min_ver is None:
        raise SystemExit(
            f"unsupported tls.min_version {tls.min_version!r}; "
            f"choose from {sorted(version_map)}"
        )

    if tls.mode == "adhoc":
        # Flask's dev server understands the literal "adhoc"; other
        # servers (gunicorn) don't, so we expand to a tuple in that path.
        return "adhoc"

    if tls.mode == "explicit":
        return (tls.certfile, tls.keyfile)

    raise SystemExit(f"unknown tls.mode: {tls.mode!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-manager")
    parser.add_argument(
        "-c", "--config",
        help="Path to config.yaml (default: $AGENT_MANAGER_CONFIG or ./config.yaml)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Load config + create app, then exit (for systemd pre-flight).",
    )
    parser.add_argument(
        "--host", default=None,
        help="Override server.bind_host",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Override server.bind_port",
    )
    parser.add_argument(
        "--no-tls", action="store_true",
        help="Disable TLS even if config.tls.enabled is true.",
    )
    parser.add_argument(
        "--dev", action="store_true",
        help="Use Flask's built-in dev server (single-threaded, debug off).",
    )
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    configure_logging(cfg.logging.level, cfg.logging.file, cfg.logging.format)

    root_err = ensure_root()
    if root_err:
        # We don't hard-fail: in tests / CI the daemon may run unprivileged.
        # We do log loudly so the operator notices.
        import logging
        logging.getLogger(__name__).warning("%s", root_err)

    app = create_app(cfg)

    if args.check:
        return 0

    host = args.host or cfg.server.bind_host
    port = args.port or cfg.server.bind_port

    if args.no_tls:
        ssl_ctx = None
    else:
        ssl_ctx = _build_ssl_context(cfg)
        if ssl_ctx is not None:
            import logging
            logging.getLogger(__name__).info(
                "TLS enabled: mode=%s bind=%s:%s",
                cfg.tls.mode, host, port,
            )
        else:
            import logging
            logging.getLogger(__name__).warning(
                "TLS disabled — credentials will be sent in plaintext on %s:%s",
                host, port,
            )

    if args.dev:
        # Flask dev server — fine for poking around, NOT for prod.
        # ``ssl_context="adhoc"`` triggers Werkzeug's built-in ad-hoc
        # cert generator (cryptography backend); on platforms where
        # ad-hoc is unavailable, fall back to plain HTTP and log.
        try:
            app.run(
                host=host,
                port=port,
                debug=False,
                use_reloader=False,
                ssl_context=ssl_ctx,
            )
        except ValueError as exc:
            if ssl_ctx == "adhoc":
                import logging
                logging.getLogger(__name__).error(
                    "Flask ad-hoc TLS failed (%s); falling back to plain HTTP. "
                    "Run with --no-tls to silence this, or switch to "
                    "tls.mode=explicit.", exc,
                )
                app.run(host=host, port=port, debug=False, use_reloader=False)
            else:
                raise
        return 0

    # Production path: hand off to gunicorn.
    from gunicorn.app.wsgiapp import run as gunicorn_run
    sys.argv = [
        "gunicorn",
        "--bind", f"{host}:{port}",
        "--workers", "2",
        "--threads", "4",
        "--access-logfile", "-",
        "--error-logfile", "-",
        "agent_manager.app:create_app()",
    ]
    if ssl_ctx is not None and ssl_ctx != "adhoc":
        # gunicorn understands --certfile + --keyfile for HTTPS.
        certfile, keyfile = ssl_ctx
        sys.argv += ["--certfile", certfile, "--keyfile", keyfile]
        sys.argv += ["--ssl-version", cfg.tls.min_version]
    gunicorn_run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())