#!/usr/bin/env bash
# install.sh — automated installer for agent-manager-daemon.
#
# What it does (default invocation):
#   1. Preflight:  python3.12, uv, systemd, root
#   2. Create /opt/agent-manager-daemon/.venv and install deps
#   3. Stage files: project source under INSTALL_ROOT, config under
#      /etc/agent-manager, unit under /etc/systemd/system
#   4. Create runtime dirs (/var/lib/agent-manager, /var/log/agent-manager)
#   5. Enable + start the systemd unit
#
# Modes:
#   ./install.sh                full install (default)
#   ./install.sh -u             uninstall (stops unit, removes files we own)
#   ./install.sh -n             dry-run (print commands, do not execute)
#   ./install.sh -p DIR         install into DIR instead of /opt/agent-manager-daemon
#   ./install.sh --system-python  skip venv, use system python3 with --break-system-packages
#   ./install.sh --skip-systemd   only stage files; don't touch systemd
#
# Idempotent: re-running on an installed system is safe. It refuses to
# overwrite an existing /etc/agent-manager/config.yaml unless --force-config.

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

INSTALL_ROOT="/opt/agent-manager-daemon"
CONFIG_DIR="/etc/agent-manager"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"
UNIT_NAME="agent-manager.service"
UNIT_SRC_PATH="systemd/${UNIT_NAME}"
WORK_DIR="/var/lib/agent-manager"
LOG_DIR="/var/log/agent-manager"
# Where the agent will be installed at runtime (config.yaml's
# upgrade.install_root). We pre-create it so the systemd unit's
# mount namespace setup doesn't fail with "No such file or directory".
AGENT_INSTALL_ROOT="/opt/myagent"
PYTHON_VERSION="3.12"
RUN_AS_USER="root"
WITH_SYSTEMD=1
WITH_VENV=1
FORCE_CONFIG=0
DRY_RUN=0
UNINSTALL=0

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Tool resolution (handles sudo's stripped PATH)
#
# `sudo` defaults to a `secure_path` that does NOT include
# $HOME/.local/bin — so `uv` installed via the official installer is
# invisible to the sudo-ed script. We resolve common install locations
# up front and use absolute paths everywhere we shell out.
# ---------------------------------------------------------------------------

resolve_tool() {
    local tool="$1"
    shift
    for dir in "$@"; do
        if [[ -x "$dir/$tool" ]]; then
            printf '%s\n' "$dir/$tool"
            return 0
        fi
    done
    # Fall back to whatever's on the current PATH (works for root,
    # in CI containers, and when sudo's PATH is unmodified).
    command -v "$tool" 2>/dev/null || return 1
}

UV_BIN="$(resolve_tool uv \
    "${HOME}/.local/bin" \
    "${HOME}/.cargo/bin" \
    /usr/local/bin /usr/bin /opt/agent-manager-daemon/.venv/bin)" \
    || UV_BIN=""

PY_BIN="$(resolve_tool "python${PYTHON_VERSION}" \
    /usr/local/bin /usr/bin /opt/agent-manager-daemon/.venv/bin)" \
    || PY_BIN=""
[[ -z "$PY_BIN" ]] && PY_BIN="$(resolve_tool python3 /usr/local/bin /usr/bin)" || true

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARN: %s\n' "$*" >&2; }
die()  { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

# Print + run, prefixing for dry-run visibility. If DRY_RUN=1 we only print.
run() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf '  would run:'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

# Like run(), but runs through sudo if current uid isn't 0.
maybe_sudo() {
    if [[ "$(id -u)" -eq 0 ]]; then
        run "$@"
    else
        run sudo "$@"
    fi
}

usage() {
    sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        -u|--uninstall)        UNINSTALL=1 ;;
        -n|--dry-run)          DRY_RUN=1 ;;
        -p|--prefix)           INSTALL_ROOT="$2"; shift ;;
        --config-dir)          CONFIG_DIR="$2"; CONFIG_FILE="${CONFIG_DIR}/config.yaml"; shift ;;
        --work-dir)            WORK_DIR="$2"; shift ;;
        --log-dir)             LOG_DIR="$2"; shift ;;
        --agent-install-root)  AGENT_INSTALL_ROOT="$2"; shift ;;
        --system-python)       WITH_VENV=0 ;;
        --skip-systemd)        WITH_SYSTEMD=0 ;;
        --force-config)        FORCE_CONFIG=1 ;;
        --user)                RUN_AS_USER="$2"; shift ;;
        -h|--help)             usage 0 ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

preflight() {
    log "preflight checks"
    command -v bash >/dev/null || die "bash is required"
    [[ -d "$SCRIPT_DIR/src/agent_manager" ]] || die "script must run from the project root (src/agent_manager not found at $SCRIPT_DIR)"
    [[ -f "$SCRIPT_DIR/pyproject.toml" ]] || die "pyproject.toml missing — wrong directory?"
    [[ -f "$SCRIPT_DIR/$UNIT_SRC_PATH" ]] || die "systemd unit not found at $SCRIPT_DIR/$UNIT_SRC_PATH"

    if [[ "$WITH_SYSTEMD" -eq 1 ]]; then
        if [[ "$(id -u)" -ne 0 ]] && ! command -v sudo >/dev/null; then
            die "systemd registration needs root; sudo not found either. Re-run as root or pass --skip-systemd."
        fi
        if [[ ! -d /run/systemd/system ]]; then
            warn "no /run/systemd/system — this host doesn't seem to be running systemd. Continuing without unit registration."
            WITH_SYSTEMD=0
        fi
    fi

    if [[ "$WITH_VENV" -eq 1 ]]; then
        if [[ -z "$UV_BIN" ]]; then
            warn "uv not found in common locations; will try system python with venv (you may need python${PYTHON_VERSION}-venv installed)"
            if [[ -z "$PY_BIN" ]]; then
                warn "no python${PYTHON_VERSION} on PATH either — set PY_BIN or install one"
            fi
        fi
    fi

    log "preflight OK"
}

# ---------------------------------------------------------------------------
# Venv + deps
# ---------------------------------------------------------------------------

create_venv() {
    local venv="${INSTALL_ROOT}/.venv"

    if [[ -d "$venv" ]]; then
        log "venv already exists at $venv (reusing)"
        return
    fi

    if [[ -n "$UV_BIN" ]]; then
        log "creating venv with uv ($PYTHON_VERSION) at $UV_BIN"
        run mkdir -p "$INSTALL_ROOT"
        run "$UV_BIN" venv --python "$PYTHON_VERSION" "$venv"
        log "installing project + deps into venv (non-editable)"
        # Non-editable: copies src/agent_manager into site-packages so
        # the daemon finds the module even if $INSTALL_ROOT is later
        # moved or the .pth file paths go stale.
        run "$UV_BIN" pip install --python "$venv/bin/python" "$SCRIPT_DIR"
        _stage_web_assets "$venv"
    elif [[ -n "$PY_BIN" ]]; then
        log "creating venv with $PY_BIN ($PYTHON_VERSION)"
        run mkdir -p "$INSTALL_ROOT"
        if ! run "$PY_BIN" -m venv "$venv"; then
            die "venv creation failed — install ${PYTHON_VERSION}-venv (apt: python${PYTHON_VERSION}-venv), install uv first, or pass --system-python"
        fi
        log "installing project + deps into venv (non-editable)"
        run "$venv/bin/pip" install --upgrade pip
        run "$venv/bin/pip" install "$SCRIPT_DIR"
        _stage_web_assets "$venv"
    else
        die "neither uv nor python${PYTHON_VERSION} found on this host; install one of them and re-run"
    fi
}

install_system_python() {
    [[ -n "$PY_BIN" ]] || die "no system python found; install ${PYTHON_VERSION} or drop --system-python"
    log "using system python at $PY_BIN (no venv)"
    "$PY_BIN" -c "import sys; assert sys.version_info[:2] == (3,12), 'need 3.12, got '+sys.version" \
        || die "system python is not 3.12 — drop --system-python and use a venv"
    run "$PY_BIN" -m pip install --break-system-packages "$SCRIPT_DIR"
}


# Copy templates/ and static/ into the venv's
# site-packages/agent_manager/ so the daemon can find them with a
# stable per-package relative path (../templates, ../static). Without
# this, the daemon's WorkingDirectory-based relative paths fail when
# systemd sets a different cwd, and every render raises
# TemplateNotFound.
_stage_web_assets() {
    local venv="$1"
    local pkg_dir="${venv}/lib/python${PYTHON_VERSION}/site-packages/agent_manager"
    log "staging templates/ + static/ into ${pkg_dir}"
    run mkdir -p "${pkg_dir}/templates" "${pkg_dir}/static"
    run cp -r "${SCRIPT_DIR}/templates/." "${pkg_dir}/templates/"
    run cp -r "${SCRIPT_DIR}/static/." "${pkg_dir}/static/"
}

# ---------------------------------------------------------------------------
# File staging
# ---------------------------------------------------------------------------

stage_files() {
    log "staging project under $INSTALL_ROOT"
    run mkdir -p "$INSTALL_ROOT"
    # Copy source tree (rsync-style: skip .venv, .git, __pycache__, tests
    # not strictly required in prod but harmless to ship).
    if command -v rsync >/dev/null; then
        run rsync -a --delete \
            --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
            --exclude '.pytest_cache' --exclude '*.pyc' --exclude '.mypy_cache' \
            "$SCRIPT_DIR/" "$INSTALL_ROOT/"
    else
        # Portable fallback: tar pipe. Excludes are coarse but cover the common cases.
        run bash -c "
            set -e
            tmp=\$(mktemp -d)
            tar --exclude='.venv' --exclude='.git' --exclude='__pycache__' \\
                --exclude='.pytest_cache' --exclude='*.pyc' \\
                -cf - -C '$SCRIPT_DIR' . | tar -xf - -C \"\$tmp\"
            rm -rf '$INSTALL_ROOT'/*
            cp -a \"\$tmp\"/. '$INSTALL_ROOT'/
            rm -rf \"\$tmp\"
        "
    fi

    log "creating runtime dirs"
    # Runtime dirs usually need root. If we're not root AND not skipping
    # systemd (where the unit runs as root anyway), just best-effort
    # try as the current user — non-fatal if it fails.
    #
    # We also pre-create cfg.upgrade.work_dir (the *parent* of which
    # is what the daemon's `mkdir(parents=True)` call needs to already
    # exist + own). Without this, a non-root invocation of
    # `python -m agent_manager` would fail with EACCES trying to
    # create .../work under a root-owned /var/lib/agent-manager.
    if [[ "$(id -u)" -eq 0 ]]; then
        run mkdir -p "$WORK_DIR" "$LOG_DIR"
        run chown -R "${RUN_AS_USER}:${RUN_AS_USER}" "$WORK_DIR" "$LOG_DIR"
    elif [[ "$WITH_SYSTEMD" -eq 0 ]]; then
        log "not root and --skip-systemd: attempting runtime dirs as current user"
        run mkdir -p "$WORK_DIR" || warn "could not create $WORK_DIR as $(id -un) — re-run as root"
        run mkdir -p "$LOG_DIR" || warn "could not create $LOG_DIR as $(id -un) — re-run as root"
    else
        # systemd unit runs as root and will create these on demand
        # (ReadWritePaths already grants access). Best-effort:
        run sudo -n mkdir -p "$WORK_DIR" "$LOG_DIR" || \
            log "sudo -n unavailable; will create $WORK_DIR/$LOG_DIR at runtime"
    fi

    # Pre-create the agent install_root too. systemd's mount-namespace
    # setup refuses to start the unit if any directory in the namespace
    # doesn't exist, and ReadWritePaths=/opt/myagent in the unit
    # silently fails on first boot before any upgrade has happened.
    # config.yaml's default install_root is /opt/myagent; we make
    # that path exist here so the unit starts clean.
    log "creating agent install_root at $AGENT_INSTALL_ROOT"
    if [[ "$(id -u)" -eq 0 ]]; then
        run mkdir -p "$AGENT_INSTALL_ROOT/releases"
        run chown -R "${RUN_AS_USER}:${RUN_AS_USER}" "$AGENT_INSTALL_ROOT"
    elif [[ "$WITH_SYSTEMD" -eq 0 ]]; then
        run mkdir -p "$AGENT_INSTALL_ROOT/releases" || \
            warn "could not pre-create $AGENT_INSTALL_ROOT as $(id -un)"
    else
        run sudo -n mkdir -p "$AGENT_INSTALL_ROOT/releases" || \
            warn "could not pre-create $AGENT_INSTALL_ROOT (sudo -n unavailable) — daemon may fail to start"
    fi

    log "installing config to $CONFIG_FILE"
    # Skip sudo when --skip-systemd is set AND we're already going to
    # write to a path under INSTALL_ROOT (which we own). Otherwise
    # /etc/... requires root.
    if [[ "$WITH_SYSTEMD" -eq 0 && "$CONFIG_DIR" == "$INSTALL_ROOT"* ]]; then
        run mkdir -p "$CONFIG_DIR"
        if [[ -f "$CONFIG_FILE" && "$FORCE_CONFIG" -eq 0 ]]; then
            warn "$CONFIG_FILE already exists; leaving it untouched (pass --force-config to overwrite)"
        else
            if [[ "$FORCE_CONFIG" -eq 1 && -f "$CONFIG_FILE" ]]; then
                run cp "$CONFIG_FILE" "${CONFIG_FILE}.bak-$(date +%Y%m%d%H%M%S)"
            fi
            run cp "$SCRIPT_DIR/config.yaml" "$CONFIG_FILE"
            run chmod 0600 "$CONFIG_FILE"
        fi
    else
        maybe_sudo mkdir -p "$CONFIG_DIR"
        if [[ -f "$CONFIG_FILE" && "$FORCE_CONFIG" -eq 0 ]]; then
            warn "$CONFIG_FILE already exists; leaving it untouched (pass --force-config to overwrite)"
        else
            if [[ "$FORCE_CONFIG" -eq 1 && -f "$CONFIG_FILE" ]]; then
                maybe_sudo cp "$CONFIG_FILE" "${CONFIG_FILE}.bak-$(date +%Y%m%d%H%M%S)"
            fi
            maybe_sudo cp "$SCRIPT_DIR/config.yaml" "$CONFIG_FILE"
            maybe_sudo chmod 0600 "$CONFIG_FILE"
        fi
    fi
}

# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------

install_systemd_unit() {
    [[ "$WITH_SYSTEMD" -eq 1 ]] || return 0

    log "installing systemd unit"
    maybe_sudo cp "$SCRIPT_DIR/$UNIT_SRC_PATH" "/etc/systemd/system/${UNIT_NAME}"
    maybe_sudo systemctl daemon-reload
    maybe_sudo systemctl enable "${UNIT_NAME}"
    maybe_sudo systemctl restart "${UNIT_NAME}"

    sleep 1
    if [[ "$DRY_RUN" -eq 1 ]]; then
        log "dry-run: skipping systemd active check"
    elif maybe_sudo systemctl is-active --quiet "${UNIT_NAME}"; then
        log "systemd unit ${UNIT_NAME} is active"
    else
        warn "systemd unit ${UNIT_NAME} is NOT active; check: journalctl -u ${UNIT_NAME} -n 50"
    fi
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

uninstall() {
    log "uninstall mode"
    if [[ "$WITH_SYSTEMD" -eq 1 ]] && maybe_sudo systemctl list-unit-files "${UNIT_NAME}" >/dev/null 2>&1; then
        log "stopping + disabling unit"
        maybe_sudo systemctl disable --now "${UNIT_NAME}" || true
    fi
    if [[ "$WITH_SYSTEMD" -eq 1 ]] && [[ -f "/etc/systemd/system/${UNIT_NAME}" ]]; then
        log "removing /etc/systemd/system/${UNIT_NAME}"
        maybe_sudo rm -f "/etc/systemd/system/${UNIT_NAME}"
        maybe_sudo systemctl daemon-reload
    fi

    if [[ -d "$INSTALL_ROOT" ]]; then
        log "removing $INSTALL_ROOT"
        run rm -rf "$INSTALL_ROOT"
    fi
    if [[ -d "$CONFIG_DIR" ]]; then
        log "removing $CONFIG_DIR"
        maybe_sudo rm -rf "$CONFIG_DIR"
    fi
    log "NOTE: $WORK_DIR, $LOG_DIR, and $AGENT_INSTALL_ROOT left intact (they may contain runtime data + logs)."
    log "Delete manually with: sudo rm -rf $WORK_DIR $LOG_DIR $AGENT_INSTALL_ROOT"
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print_summary() {
    cat <<EOF

========== install summary ==========
install_root     : $INSTALL_ROOT
config_file      : $CONFIG_FILE
work_dir         : $WORK_DIR
log_dir          : $LOG_DIR
agent_install_root: $AGENT_INSTALL_ROOT
systemd unit     : $UNIT_NAME  ($([ "$WITH_SYSTEMD" -eq 1 ] && echo enabled || echo disabled))
python venv  : $([ "$WITH_VENV" -eq 1 ] && echo "$INSTALL_ROOT/.venv" || echo "(system python)")
=====================================

Quick start:
  sudoedit $CONFIG_FILE             # set FTP creds + (recommended) real TLS cert
  sudo systemctl restart $UNIT_NAME
  curl -ks https://127.0.0.1:8443/login

Logs:
  journalctl -u $UNIT_NAME -f
  tail -F $LOG_DIR/*.log

Uninstall:
  sudo $SCRIPT_DIR/install.sh -u
EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

preflight

if [[ "$UNINSTALL" -eq 1 ]]; then
    uninstall
else
    log "installing agent-manager-daemon into $INSTALL_ROOT (dry-run=$DRY_RUN)"
    stage_files
    if [[ "$WITH_VENV" -eq 1 ]]; then
        create_venv
    else
        install_system_python
    fi
    install_systemd_unit
    # Post-install sanity: the systemd unit will refuse to start (226)
    # if cfg.upgrade.install_root doesn't exist on disk. We pre-create
    # it in stage_files(); verify here so the operator sees a clear
    # "install incomplete" message instead of a cryptic NAMESPACE error.
    if [[ "$DRY_RUN" -eq 0 && ! -d "$AGENT_INSTALL_ROOT" ]]; then
        warn "post-install check: $AGENT_INSTALL_ROOT still missing — daemon will fail to start. Re-run as root or set --agent-install-root."
    fi
    print_summary
fi

log "done."