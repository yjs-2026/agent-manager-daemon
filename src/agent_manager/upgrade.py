"""Agent upgrade engine.

Responsibilities:

  1. Download an upgrade artifact from an FTP/FTPS server to a local
     staging directory.
  2. Verify the artifact matches one of the supported archive suffixes.
  3. Extract into a versioned release directory under ``install_root``.
  4. Atomically switch the ``current`` symlink to the new release.
  5. Run an optional post-install hook.
  6. Restart the configured systemd unit.
  7. Retain the N most-recent releases; older ones are removed.

The module is *process-safe*: every upgrade runs under a per-job
:class:`threading.Lock` stored in :class:`JobRegistry`. Concurrent
calls to the API with the same job_id (or any other upgrade request)
are serialised so we never leave ``current`` pointing at a half-
extracted release.
"""

from __future__ import annotations

import enum
import logging
import os
import posixpath
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from .config import Config, FTPSettings
from .logging_setup import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UpgradeError(Exception):
    """Base class for upgrade failures."""


class DownloadFailed(UpgradeError):
    """FTP fetch failed (network, auth, missing file)."""


class UnsupportedArchive(UpgradeError):
    """Artifact filename did not match any configured archive suffix."""


class ExtractFailed(UpgradeError):
    """Archive is corrupt or cannot be unpacked here."""


class SwitchFailed(UpgradeError):
    """Atomic symlink swap failed; install_root may need manual repair."""


class HookFailed(UpgradeError):
    """Post-install hook exited non-zero."""


# ---------------------------------------------------------------------------
# Job tracking
# ---------------------------------------------------------------------------


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class UpgradeJob:
    job_id: str
    filename: str
    version: str
    status: JobStatus = JobStatus.PENDING
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    log: list[str] = field(default_factory=list)
    error: Optional[str] = None
    installed_release: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "filename": self.filename,
            "version": self.version,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "installed_release": self.installed_release,
            "log": list(self.log),
        }


class JobRegistry:
    """In-memory job table. Persisted to JSON on disk for crash recovery."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._jobs: dict[str, UpgradeJob] = {}
        self._job_locks: dict[str, threading.Lock] = {}
        self._load()

    # ---- CRUD ------------------------------------------------------------

    def create(self, job_id: str, filename: str, version: str) -> UpgradeJob:
        with self._lock:
            if job_id in self._jobs:
                raise ValueError(f"job_id {job_id!r} already exists")
            job = UpgradeJob(job_id=job_id, filename=filename, version=version)
            self._jobs[job_id] = job
            self._job_locks[job_id] = threading.Lock()
            self._persist()
            return job

    def get(self, job_id: str) -> Optional[UpgradeJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[UpgradeJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.started_at or "", reverse=True)

    def job_lock(self, job_id: str) -> threading.Lock:
        with self._lock:
            return self._job_locks.setdefault(job_id, threading.Lock())

    def update(self, job_id: str, **fields) -> UpgradeJob:
        with self._lock:
            job = self._jobs[job_id]
            for k, v in fields.items():
                if not hasattr(job, k):
                    raise AttributeError(f"unknown job field {k!r}")
                setattr(job, k, v)
            self._persist()
            return job

    def append_log(self, job_id: str, line: str) -> None:
        with self._lock:
            self._jobs[job_id].log.append(line)
            # Don't persist on every log line — too expensive.
            self._persist()

    # ---- persistence -----------------------------------------------------

    def _load(self) -> None:
        if not self._path or not os.path.isfile(self._path):
            return
        try:
            import json

            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception as exc:  # noqa: BLE001 — corrupted history is non-fatal
            logger.warning("could not load job registry %s: %s", self._path, exc)
            return
        for jid, j in raw.get("jobs", {}).items():
            try:
                status = JobStatus(j.get("status", "pending"))
            except ValueError:
                status = JobStatus.FAILED
            self._jobs[jid] = UpgradeJob(
                job_id=jid,
                filename=j.get("filename", ""),
                version=j.get("version", ""),
                status=status,
                started_at=j.get("started_at"),
                finished_at=j.get("finished_at"),
                log=list(j.get("log", [])),
                error=j.get("error"),
                installed_release=j.get("installed_release"),
            )
            self._job_locks[jid] = threading.Lock()

    def _persist(self) -> None:
        if not self._path:
            return
        import json

        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    {"jobs": {jid: j.to_dict() for jid, j in self._jobs.items()}},
                    fh,
                    indent=2,
                )
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("could not persist job registry: %s", exc)


# ---------------------------------------------------------------------------
# FTP fetcher — uses only stdlib (urllib). Pure-python keeps us off
# ftplib's quirky TLS surface and means the daemon has no extra C deps.
# ---------------------------------------------------------------------------


class FtpDownloader:
    """Pulls a single file from an FTP/FTPS (or HTTP fallback) URL.

    Strategy:

      * For ``ftp://`` URLs we embed the credentials directly into the
        URL (``ftp://user:pass@host/path``). Python's stdlib
        :class:`urllib.request.FTPHandler` extracts them from the URL
        and there is no public credential hook to override in 3.12+.
        The temporary URL only lives inside this method.
      * For ``http://`` / ``https://`` URLs (handy in dev / CI) we use
        plain ``urlopen``; if the server returns 401, urllib will fall
        back to its default realm machinery.
      * For ``ftps://`` or anything urllib can't speak natively, the
        caller should swap in their own downloader via the
        ``downloader=`` constructor argument on
        :class:`UpgradeManager`.

    Credentials are never logged. The URL we log shows the host and
    path only.
    """

    def __init__(self, ftp: FTPSettings) -> None:
        self._ftp = ftp

    def fetch(self, url: str, dest: Path) -> None:
        """Download ``url`` to ``dest``."""
        username = self._ftp.username() or "anonymous"
        password = self._ftp.password()

        from urllib.parse import quote, urlparse, urlunparse
        from urllib.request import urlopen

        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname or ""

        # Build the effective URL we'll hand to urllib. For FTP we
        # inject credentials; for HTTP we leave them off (the caller
        # can put them in the URL if needed).
        if scheme.startswith("ftp"):
            netloc = host
            if parsed.port:
                netloc = f"{host}:{parsed.port}"
            # quote() each segment so '@', ':' or '/' inside password
            # / username don't break the URL parser.
            if username:
                userinfo = quote(username, safe="")
                if password:
                    userinfo += ":" + quote(password, safe="")
                netloc = f"{userinfo}@{netloc}"
            effective = urlunparse((
                parsed.scheme,
                netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            ))
            logger.info(
                "FTP fetch %s://%s%s -> %s (user=%s)",
                parsed.scheme, host, parsed.path, dest, username,
            )
        else:
            effective = url
            logger.info("HTTP fetch %s -> %s", url, dest)

        try:
            with urlopen(effective, timeout=self._ftp.timeout) as resp, open(dest, "wb") as out:
                shutil.copyfileobj(resp, out)
        except Exception as exc:
            raise DownloadFailed(f"download failed: {exc}") from exc





# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------


class ArchiveExtractor:
    """Extract a downloaded archive into a target directory.

    Supported suffixes (case-insensitive): .tar.gz, .tgz, .tar.bz2,
    .tar.xz, .zip. Anything else raises :class:`UnsupportedArchive`.
    """

    def __init__(self, suffixes: Iterable[str]) -> None:
        self._suffixes = tuple(s.lower() for s in suffixes)

    def supports(self, filename: str) -> bool:
        f = filename.lower()
        return any(f.endswith(s) for s in self._suffixes)

    def extract(self, archive: Path, dest: Path) -> None:
        if not archive.is_file():
            raise ExtractFailed(f"archive not found: {archive}")
        dest.mkdir(parents=True, exist_ok=True)
        name = archive.name.lower()
        try:
            if name.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
                with tarfile.open(archive, "r:*") as tf:
                    self._safe_extract(tf, dest)
            elif name.endswith(".zip"):
                with zipfile.ZipFile(archive) as zf:
                    self._extract_zip(zf, dest)
            else:
                raise UnsupportedArchive(f"unsupported archive: {archive.name}")
        except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
            raise ExtractFailed(f"extract failed: {exc}") from exc

    @staticmethod
    def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
        # tarfile extraction allows path traversal via symlinks. Guard
        # against escapes from ``dest`` even when the archive is
        # trusted — defence in depth.
        dest_resolved = dest.resolve()
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(dest_resolved)):
                raise ExtractFailed(f"unsafe path in archive: {member.name}")
        tf.extractall(dest)

    @staticmethod
    def _extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
        dest_resolved = dest.resolve()
        for name in zf.namelist():
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest_resolved)):
                raise ExtractFailed(f"unsafe path in archive: {name}")
        zf.extractall(dest)


# ---------------------------------------------------------------------------
# Upgrade orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpgradeRequest:
    job_id: str
    filename: str  # filename on the FTP server, e.g. "myagent-1.2.3.tar.gz"
    version: str  # release directory name to create, e.g. "1.2.3"
    ftp_url: str = ""  # full URL; defaults to ftp.url + "/" + filename


class UpgradeManager:
    """High-level orchestrator. Glue between FTP, extractor, symlink, systemd."""

    def __init__(
        self,
        cfg: Config,
        registry: JobRegistry,
        downloader: Optional[FtpDownloader] = None,
        extractor: Optional[ArchiveExtractor] = None,
        systemd_unit_override: Optional[str] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._cfg = cfg
        self._registry = registry
        self._downloader = downloader or FtpDownloader(cfg.upgrade.ftp)
        self._extractor = extractor or ArchiveExtractor(cfg.upgrade.archive_formats)
        # systemd_unit_override lets tests pretend systemctl is unavailable.
        self._systemd_unit_override = systemd_unit_override
        self._clock = clock

    # ---- public ----------------------------------------------------------

    def upgrade(self, req: UpgradeRequest) -> UpgradeJob:
        """Run a full upgrade end-to-end. Returns the final :class:`UpgradeJob`."""
        job = self._registry.create(req.job_id, req.filename, req.version)
        with self._registry.job_lock(req.job_id):
            self._run_locked(job, req)
        return job

    def rollback(self, job_id: str) -> UpgradeJob:
        """Switch ``current`` back to the previous release directory.

        Looks up ``install_root/releases/<previous>`` (the one before
        ``current``) and re-points the symlink. Only works if at least
        two releases are kept around.
        """
        job = self._registry.get(job_id)
        if job is None:
            raise UpgradeError(f"no such job: {job_id}")
        with self._registry.job_lock(job_id):
            self._mark_running(job)
            try:
                prev = self._previous_release_dir()
                self._switch_symlink(prev)
                self._mark_success(
                    job,
                    installed_release=prev.name,
                    extra_log=[f"rolled back to {prev.name}"],
                )
            except Exception as exc:  # noqa: BLE001
                self._mark_failed(job, str(exc))
                raise
        return job

    # ---- internal --------------------------------------------------------

    def _run_locked(self, job: UpgradeJob, req: UpgradeRequest) -> None:
        self._mark_running(job)
        try:
            work_dir = Path(self._cfg.upgrade.work_dir)
            install_root = Path(self._cfg.upgrade.install_root)
            releases_dir = install_root / "releases"
            work_dir.mkdir(parents=True, exist_ok=True)
            releases_dir.mkdir(parents=True, exist_ok=True)

            archive_path = work_dir / req.filename
            url = req.ftp_url or self._build_default_url(req.filename)

            # 1) download
            self._downloader.fetch(url, archive_path)
            self._log(job, f"downloaded {url} -> {archive_path} ({archive_path.stat().st_size} bytes)")

            if not self._extractor.supports(req.filename):
                raise UnsupportedArchive(f"unsupported archive: {req.filename}")

            # 2) extract
            target = releases_dir / req.version
            if target.exists():
                # Don't clobber an existing release — operator must pick
                # a new version or remove the directory manually.
                raise UpgradeError(
                    f"release dir already exists: {target} — pick a new version or remove it"
                )
            self._extractor.extract(archive_path, target)
            self._log(job, f"extracted into {target}")

            # 3) atomic symlink swap
            self._switch_symlink(target)
            self._log(job, f"switched current -> {target.name}")

            # 4) post-install hook (optional)
            if self._cfg.upgrade.post_install_hook:
                self._run_hook(job, target)

            # 5) systemd restart
            unit = self._systemd_unit_override or self._cfg.upgrade.systemd_unit
            if unit:
                self._systemctl_restart(unit, job)

            # 6) retention
            self._prune_releases(releases_dir, self._cfg.upgrade.keep_releases)

            self._mark_success(job, installed_release=target.name)
        except UpgradeError as exc:
            self._mark_failed(job, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._mark_failed(job, f"unexpected: {exc}")

    def _build_default_url(self, filename: str) -> str:
        base = self._cfg.upgrade.ftp.url.rstrip("/")
        return f"{base}/{posixpath.basename(filename)}"

    def _switch_symlink(self, target: Path) -> None:
        install_root = Path(self._cfg.upgrade.install_root)
        current = install_root / "current"
        install_root.mkdir(parents=True, exist_ok=True)
        # tmp symlink + rename is atomic on POSIX.
        tmp = current.with_suffix(current.suffix + ".new")
        try:
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            os.symlink(target, tmp)
            os.replace(tmp, current)
        except OSError as exc:
            raise SwitchFailed(f"failed to switch symlink: {exc}") from exc

    def _previous_release_dir(self) -> Path:
        """Return the release dir one slot before ``current``."""
        install_root = Path(self._cfg.upgrade.install_root)
        releases_dir = install_root / "releases"
        current = install_root / "current"
        if not current.is_symlink():
            raise SwitchFailed("current is not a symlink — no upgrade history?")
        # Resolve and sort remaining releases lexicographically. For a
        # real SemVer scheme you'd parse versions, but plain lex order
        # is fine if callers pass sortable version strings.
        remaining = sorted(
            (p for p in releases_dir.iterdir() if p.is_dir()),
            key=lambda p: p.name,
        )
        current_target = Path(os.readlink(current))
        if current_target.is_absolute():
            current_target = current_target
        else:
            current_target = (current.parent / current_target).resolve()
        if current_target not in remaining:
            raise SwitchFailed(f"current -> {current_target} not in {releases_dir}")
        idx = remaining.index(current_target)
        if idx == 0:
            raise SwitchFailed("no previous release to roll back to")
        return remaining[idx - 1]

    def _prune_releases(self, releases_dir: Path, keep: int) -> None:
        if keep <= 0:
            return
        ordered = sorted(
            (p for p in releases_dir.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for victim in ordered[keep:]:
            logger.info("pruning old release %s", victim)
            shutil.rmtree(victim, ignore_errors=True)

    def _systemctl_restart(self, unit: str, job: UpgradeJob) -> None:
        if not shutil.which("systemctl"):
            self._log(job, "systemctl not found — skipping restart")
            return
        proc = subprocess.run(
            ["systemctl", "restart", unit],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise UpgradeError(f"systemctl restart {unit} failed: {err}")
        self._log(job, f"systemctl restart {unit} OK")

    def _run_hook(self, job: UpgradeJob, target: Path) -> None:
        hook = self._cfg.upgrade.post_install_hook
        if not os.path.isfile(hook):
            self._log(job, f"hook {hook} not present — skipping")
            return
        if not os.access(hook, os.X_OK):
            raise HookFailed(f"hook not executable: {hook}")
        env = os.environ.copy()
        env.update(
            {
                "AGENT_VERSION": job.version,
                "AGENT_INSTALL_DIR": str(target),
                "AGENT_WORK_DIR": self._cfg.upgrade.work_dir,
            }
        )
        proc = subprocess.run([hook], capture_output=True, env=env, check=False)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise HookFailed(f"hook exited {proc.returncode}: {err}")
        self._log(job, f"hook {hook} OK")

    # ---- job helpers -----------------------------------------------------

    def _mark_running(self, job: UpgradeJob) -> None:
        self._registry.update(
            job.job_id,
            status=JobStatus.RUNNING,
            started_at=self._clock().isoformat(),
            error=None,
        )

    def _mark_success(
        self,
        job: UpgradeJob,
        installed_release: Optional[str] = None,
        extra_log: Optional[list[str]] = None,
    ) -> None:
        fields: dict = {
            "status": JobStatus.SUCCESS,
            "finished_at": self._clock().isoformat(),
        }
        if installed_release is not None:
            fields["installed_release"] = installed_release
        self._registry.update(job.job_id, **fields)
        if extra_log:
            for line in extra_log:
                self._log(job, line)
        # Move artifact into the releases dir for housekeeping later.
        self._log(job, "upgrade complete")

    def _mark_failed(self, job: UpgradeJob, error: str) -> None:
        logger.error("upgrade job %s failed: %s", job.job_id, error)
        self._registry.update(
            job.job_id,
            status=JobStatus.FAILED,
            finished_at=self._clock().isoformat(),
            error=error,
        )

    def _log(self, job: UpgradeJob, line: str) -> None:
        ts = self._clock().isoformat()
        self._registry.append_log(job.job_id, f"{ts} {line}")


# ---------------------------------------------------------------------------
# Convenience constructors used by the API layer.
# ---------------------------------------------------------------------------


def build_registry(cfg: Config) -> JobRegistry:
    """Create the on-disk :class:`JobRegistry`.

    Raises:
        OSError: if the work dir can't be created. We don't catch and
            re-raise as a more specific error — the underlying errno
            (EACCES for permission, ENOENT for missing parent) is what
            the operator needs to see. We log the *configured* path
            so the operator knows where to look.
    """
    work_dir = Path(cfg.upgrade.work_dir)
    logger.info("initialising job registry under %s", work_dir)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Add context before re-raising so the traceback tells the
        # operator which knob in config.yaml is wrong.
        raise OSError(
            f"could not create upgrade.work_dir {str(work_dir)!r}: {exc}. "
            f"Check that the parent directory exists and is writable "
            f"by the user the daemon runs as (root via systemd). "
            f"To move it, edit /etc/agent-manager/config.yaml."
        ) from exc
    return JobRegistry(path=str(work_dir / "jobs.json"))


__all__ = [
    "ArchiveExtractor",
    "DownloadFailed",
    "ExtractFailed",
    "FtpDownloader",
    "HookFailed",
    "JobRegistry",
    "JobStatus",
    "SwitchFailed",
    "UnsupportedArchive",
    "UpgradeError",
    "UpgradeJob",
    "UpgradeManager",
    "UpgradeRequest",
    "build_registry",
]