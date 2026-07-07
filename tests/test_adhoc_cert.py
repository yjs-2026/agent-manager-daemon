"""Tests for the in-process adhoc TLS cert generator used by gunicorn."""

from __future__ import annotations

import os
import ssl
import time
from pathlib import Path

import pytest


def test_generate_adhoc_cert_writes_files(tmp_path: Path):
    from agent_manager.__main__ import _generate_adhoc_cert

    cert, key = _generate_adhoc_cert(work_dir=str(tmp_path))
    assert Path(cert).is_file()
    assert Path(key).is_file()
    # Key must be mode 0600 — it sits in StateDirectory which other
    # local users could otherwise read.
    assert oct(os.stat(key).st_mode & 0o777) == "0o600"

    # Both must be valid PEM + parse as a real cert/key pair.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    # If load_cert_chain didn't raise, we have a valid pair.


def test_generate_adhoc_cert_is_cached(tmp_path: Path):
    from agent_manager.__main__ import _generate_adhoc_cert

    cert1, key1 = _generate_adhoc_cert(work_dir=str(tmp_path))
    # Regenerate and ensure we got the *same* paths (i.e. cache hit).
    cert2, key2 = _generate_adhoc_cert(work_dir=str(tmp_path))
    assert cert1 == cert2
    assert key1 == key2


def test_generate_adhoc_cert_creates_dir(tmp_path: Path):
    from agent_manager.__main__ import _generate_adhoc_cert

    target = tmp_path / "nested" / "dir"
    assert not target.exists()
    _generate_adhoc_cert(work_dir=str(target))
    assert target.is_dir()