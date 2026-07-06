"""Tests for ``agent_manager.config`` TLS settings + validation."""

from __future__ import annotations

import pytest

from agent_manager.config import Config


def _minimal_yaml(extra_top: dict | None = None) -> str:
    """Build a config.yaml string with all required sections."""
    return """
server:
  bind_host: "127.0.0.1"
  bind_port: 8443
  secret_key: "x"
api:
  tokens: []
  require_token: true
upgrade:
  work_dir: "/tmp/w"
  install_root: "/tmp/i"
logging:
  level: "INFO"
tls:
  enabled: true
  mode: "adhoc"
  certfile: ""
  keyfile: ""
  min_version: "TLSv1.2"
"""


def test_tls_defaults(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(_minimal_yaml())
    cfg = Config.load(str(p))
    assert cfg.tls.enabled is True
    assert cfg.tls.mode == "adhoc"
    assert cfg.tls.min_version == "TLSv1.2"


def test_tls_explicit_requires_both_files(tmp_path):
    p = tmp_path / "c.yaml"
    body = _minimal_yaml().replace('mode: "adhoc"', 'mode: "explicit"')
    p.write_text(body)
    with pytest.raises(ValueError, match="certfile and tls.keyfile"):
        Config.load(str(p))


def test_tls_explicit_with_both_files_ok(tmp_path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("dummy")
    key.write_text("dummy")
    p = tmp_path / "c.yaml"
    body = _minimal_yaml().replace('mode: "adhoc"', 'mode: "explicit"')
    body = body.replace('certfile: ""', f'certfile: "{cert}"')
    body = body.replace('keyfile: ""', f'keyfile: "{key}"')
    p.write_text(body)
    cfg = Config.load(str(p))
    assert cfg.tls.certfile == str(cert)
    assert cfg.tls.keyfile == str(key)


def test_tls_unknown_mode_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    body = _minimal_yaml().replace('mode: "adhoc"', 'mode: "foobar"')
    p.write_text(body)
    with pytest.raises(ValueError, match="must be 'adhoc' or 'explicit'"):
        Config.load(str(p))


def test_tls_can_be_disabled(tmp_path):
    p = tmp_path / "c.yaml"
    body = _minimal_yaml().replace("enabled: true", "enabled: false")
    p.write_text(body)
    cfg = Config.load(str(p))
    assert cfg.tls.enabled is False