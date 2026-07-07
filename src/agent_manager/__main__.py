"""Entry point: ``python -m agent_manager`` or the ``agent-manager`` script."""

from __future__ import annotations

import argparse
import datetime as _dt
import ipaddress
import os
import ssl
import sys
from pathlib import Path

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


def _generate_adhoc_cert(work_dir: str = "/var/lib/agent-manager"):
    """Generate a one-shot self-signed cert for tls.mode=adhoc + gunicorn.

    Gunicorn accepts only ``--certfile`` + ``--keyfile`` (no inline / magic
    adhoc), so when the config says ``adhoc`` we generate a fresh cert on
    disk and point gunicorn at it. The cert is short-lived (24h); if the
    daemon runs longer than that the browser will warn on the next visit
    and we re-generate on restart.

    Returns ``(cert_path, key_path)``.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    os.makedirs(work_dir, exist_ok=True)
    cert_path = os.path.join(work_dir, "adhoc-cert.pem")
    key_path = os.path.join(work_dir, "adhoc-key.pem")

    # If both files already exist (e.g. daemon was just restarted a few
    # seconds ago), skip regeneration. 24h is plenty for dev / smoke-test.
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        age = _dt.datetime.now().timestamp() - os.path.getmtime(cert_path)
        if age < 86400:
            return cert_path, key_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "agent-manager-daemon (adhoc)"),
    ])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(hours=24))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.DNSName("agent-manager-daemon"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                x509.IPAddress(ipaddress.IPv6Address("::1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    os.chmod(key_path, 0o600)
    return cert_path, key_path


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

    # gunicorn's default control-socket directory is $HOME/.gunicorn.
    # Under systemd with ProtectHome=true that's a read-only bind
    # mount and gunicorn fails with "[Errno 30] Read-only file system".
    # Redirect $HOME to a writable path under our StateDirectory.
    # Default to the parent of cfg.upgrade.work_dir (e.g.
    # /var/lib/agent-manager), or honor an explicit override.
    gunicorn_home = os.environ.get(
        "AGENT_MANAGER_STATE_DIR",
        str(Path(cfg.upgrade.work_dir).parent),
    )
    os.environ["HOME"] = gunicorn_home

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
    elif ssl_ctx == "adhoc":
        # gunicorn has no 'adhoc' magic — we generate a self-signed cert
        # on the fly under $HOME and pass it explicitly.
        cert_path, key_path = _generate_adhoc_cert(work_dir=gunicorn_home)
        sys.argv += ["--certfile", cert_path, "--keyfile", key_path]
        sys.argv += ["--ssl-version", cfg.tls.min_version]
    gunicorn_run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())