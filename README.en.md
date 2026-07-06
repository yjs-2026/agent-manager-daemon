# Agent Manager Daemon

A small Linux daemon (Python 3.12 + Flask) that does two things:

1. **Web UI for OS password self-service.** Users sign in with their
   Linux account, re-authenticate, and pick a new password. Backed by
   `crypt(3)` for verification and `chpasswd` for updates — no PAM
   stack gymnastics, no shell escaping surface.
2. **HTTP API for agent lifecycle management.** External callers
   (CI/CD, control plane) `POST` an upgrade request; the daemon
   downloads the artifact from an FTP/FTPS server, extracts it into
   `/opt/<agent>/releases/<version>/`, atomically swaps the
   `current` symlink, runs an optional post-install hook, and
   restarts the configured systemd unit. Concurrent upgrades are
   serialised per-`install_root`.

```
                        +--------------------+
   browser  ─HTTP────► |  Flask app (gunicorn)
   (login + form)      |  ├── web blueprint  │
                        |  └── api blueprint │ ──Bearer token──► external caller
                        |        │
                        |        ▼
                        |  UpgradeManager
                        |   ├── FtpDownloader (urllib)
                        |   ├── ArchiveExtractor
                        |   ├── JobRegistry (JSON on disk)
                        |   └── systemctl / chpasswd
                        +--------------------+
```

## Layout

```
src/agent_manager/
├── app.py            Flask factory
├── __main__.py       entry point (python -m agent_manager)
├── config.py         YAML + env-var config loader
├── auth.py           /etc/shadow authenticate + chpasswd change
├── web.py            login / logout / change-password UI
├── api.py            /api/v1/* endpoints
├── api_auth.py       Bearer-token decorator (SHA-256 hashes at rest)
├── upgrade.py        FTP fetch + extract + symlink + systemctl
└── logging_setup.py
templates/             base.html, login.html, change_password.html
static/                style.css
systemd/               agent-manager.service
tests/                 pytest suite
config.yaml            default runtime config
```

## Install

```bash
# requires Python 3.12 (uv will fetch one if missing)
uv venv --python 3.12
uv pip install -e ".[dev]"

# copy the example config and edit for your site
sudo install -d /etc/agent-manager
sudo cp config.yaml /etc/agent-manager/config.yaml
sudoedit /etc/agent-manager/config.yaml

# install the systemd unit
sudo install -m 0644 systemd/agent-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-manager
```

The daemon binds to `0.0.0.0:8443` over **HTTPS** by default
(`tls.mode: adhoc` → self-signed cert per startup; you'll see a
browser warning every restart). For real deployments switch to
`tls.mode: explicit` and point `certfile`/`keyfile` at a Let's
Encrypt or internal-CA pair. See `config.yaml` for every knob, and
the *Security notes* section for cookie hardening implications.

## Configuration

All values live in `config.yaml`. Any key may be overridden by an
environment variable of the form `AGENT_MANAGER_<UPPER_SNAKE>`,
with `__` separating nested keys:

```bash
export AGENT_MANAGER_SERVER__BIND_PORT=9090
export AGENT_MANAGER_UPGRADE__FTP__URL=ftp://ftp.example.com/builds
export AGENT_MANAGER_FTP_USER=deployer
export AGENT_MANAGER_FTP_PASS=...
```

### API tokens

Tokens in `config.yaml` are stored as **SHA-256 hex digests** of the
real bearer strings. Generate one with:

```bash
python -c "from agent_manager.api_auth import hash_token; print(hash_token('my-real-token'))"
```

Paste the resulting hex into `api.tokens[]`. Callers send
`Authorization: Bearer my-real-token`.

## API

All routes require `Authorization: Bearer <token>` unless
`api.require_token: false`.

| Method | Path | Body | Description |
|--------|------|------|-------------|
| GET | `/api/v1/health` | — | liveness, returns version + config summary |
| POST | `/api/v1/upgrades` | `{job_id, filename, version, ftp_url?}` | kick off an upgrade, returns 202 |
| GET | `/api/v1/upgrades` | — | list jobs (most recent first) |
| GET | `/api/v1/upgrades/<id>` | — | job status, log, error |
| POST | `/api/v1/upgrades/<id>/rollback` | — | switch `current` symlink to previous release |

`POST /api/v1/upgrades` is asynchronous — it spawns a worker thread
and returns `202 Accepted` immediately. Poll `GET /api/v1/upgrades/<id>`
to follow progress.

### Example

```bash
TOKEN=my-real-token
curl -ks -X POST https://localhost:8443/api/v1/upgrades \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"job_id":"u-$(date +%s)","filename":"myagent-1.4.2.tar.gz","version":"1.4.2"}'
# {"job_id":"u-1700000000","status":"pending","poll_url":"/api/v1/upgrades/u-1700000000"}

curl -ks -H "Authorization: Bearer $TOKEN" \
  https://localhost:8443/api/v1/upgrades/u-1700000000
```

## Layout on disk after upgrades

```
/opt/myagent/
├── current -> releases/1.4.2          # active release (atomic symlink)
└── releases/
    ├── 1.4.2/
    │   └── bin/myagent
    ├── 1.4.1/
    └── 1.3.7/

/var/lib/agent-manager/
├── work/
│   ├── jobs.json                       # persistent job history
│   └── agent-1.4.2.tar.gz              # last downloaded artifact
└── ...
```

`upgrade.keep_releases` controls retention; older releases are
pruned after a successful upgrade.

## Security notes

* The daemon **must run as root** — it reads `/etc/shadow`, calls
  `chpasswd`, and restarts systemd units. The supplied systemd unit
  hardens what it can (`ProtectSystem=strict`, `PrivateTmp`, etc.)
  but cannot drop privileges entirely.
* API tokens are stored as SHA-256 hashes only. Rotate by adding a
  new hash; remove old ones when callers have migrated.
* Web logins are Flask session cookies. With HTTPS enabled,
  `server.session_cookie_secure: true` is on by default — never
  turn it off, or the browser will happily send the cookie back over
  plain HTTP.
* `web_allowed_users` is an optional allow-list — leave empty to
  permit any account present on the system.
* New passwords are validated client-side (≥8 chars, ≤128, no control
  bytes). Operators wanting stronger checks should layer `pam_pwquality`
  / `passwdqc` — the daemon uses `chpasswd`, which honours those PAM
  rules automatically.
* TLS: `tls.mode: adhoc` is fine for development. In production use
  `tls.mode: explicit` with a real cert (e.g. Let's Encrypt via
  certbot, or an internal CA). The daemon refuses to start if
  `tls.enabled: true` but no usable cert is available.

## Tests

```bash
uv run pytest -v
```

The suite fakes `/etc/shadow`, `/etc/passwd`, the FTP server (via a
local HTTP file-server), and `systemctl` — no real users or services
are touched.

### Known Python-version warnings

* `crypt` and `spwd` are deprecated in 3.12 and **removed in 3.13**.
  On 3.13+ migrate to `python:passlib` or `cryptography` for shadow
  verification. The daemon targets Python 3.12 as the user requested.
* `tarfile.TarFile.extractall` will require a `filter=` argument in
  Python 3.14 (security hardening against zip-slip / metadata
  smuggling). Add `filter="data"` before upgrading.

## Manual smoke test (dev mode)

```bash
uv run python -m agent_manager --dev
```

Then point a browser at <https://localhost:8443/login>. Expect a
"connection not secure" warning because the daemon uses an ad-hoc
self-signed certificate by default — click through, or supply
`tls.certfile` / `tls.keyfile` for a trusted one.