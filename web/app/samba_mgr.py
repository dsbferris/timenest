"""Thin wrapper around the Samba container.

We shell out to ``docker exec`` rather than re-implementing Samba's
passdb protocol because tdbsam is version-sensitive and a plain shell
call is trivially auditable. The helper scripts in ./scripts/ do the
actual work inside the container.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_SESSIONS_RE = re.compile(
    r"^(?P<pid>\d+)\s+(?P<user>\S+)\s+(?P<group>\S+)\s+"
    r"(?P<machine>\S+)\s+\((?P<ip>[^)]+)\)\s+(?P<proto>\S+)",
    re.MULTILINE,
)

# Time Machine drops this marker inside the sparsebundle when it finishes
# a checkpoint, so its mtime is the closest thing to a "last backup" time.
_TM_MARKER = ".com.apple.timemachine.supported"


@dataclass(frozen=True, slots=True)
class DirUsage:
    """Result of one walk over a user's backup directory."""

    used_bytes: int
    last_backup_ts: int | None


@dataclass(frozen=True, slots=True)
class TimeNestUser:
    username: str
    # None when the share fragment has no parseable quota, which is not the
    # same as a quota of zero -- the UI renders it as "unknown".
    quota_gb: int | None
    path: Path
    used_bytes: int
    last_backup_ts: int | None


@dataclass(frozen=True, slots=True)
class SmbSession:
    pid: int
    user: str
    machine: str
    ip: str
    protocol: str


class SambaManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------ users

    async def create_user(self, username: str, password: str, quota_gb: int) -> None:
        self._validate_username(username)
        if quota_gb < 10:
            raise ValueError("quota must be at least 10 GB")
        if quota_gb > self.settings.max_quota_gb:
            raise ValueError(
                f"quota must be at most {self.settings.max_quota_gb} GB"
            )
        # The password goes over stdin, never argv: `docker exec` arguments
        # are visible in `ps` to every user on the host and are recorded by
        # the docker daemon.
        await self._exec(
            "/usr/local/bin/create-user.sh",
            username,
            str(quota_gb),
            stdin=password + "\n",
        )
        invalidate_usage(self.settings.backup_path / username)

    async def delete_user(self, username: str, purge: bool = False) -> None:
        self._validate_username(username)
        args = ["/usr/local/bin/delete-user.sh", username]
        if purge:
            args.append("--purge")
        await self._exec(*args)
        invalidate_usage(self.settings.backup_path / username)

    def list_users(self) -> list[TimeNestUser]:
        """Enumerate users from the share fragments.

        Blocking: it stats the backup tree (cached, see ``_scan_usage``).
        Call it from a thread when you are on the event loop -- see
        ``alist_users``.
        """
        shares_dir = self.settings.shares_path
        if not shares_dir.is_dir():
            log.warning("shares directory %s is not mounted", shares_dir)
            return []

        users: list[TimeNestUser] = []
        for conf in shares_dir.glob("*.conf"):
            username = conf.stem
            user_dir = self.settings.backup_path / username
            usage = _scan_usage(user_dir, self.settings.usage_cache_ttl)
            users.append(
                TimeNestUser(
                    username=username,
                    quota_gb=self._parse_quota(conf),
                    path=user_dir,
                    used_bytes=usage.used_bytes,
                    last_backup_ts=usage.last_backup_ts,
                )
            )
        users.sort(key=lambda u: u.username)
        return users

    async def alist_users(self) -> list[TimeNestUser]:
        """``list_users`` off the event loop, for async callers."""
        return await asyncio.to_thread(self.list_users)

    # --------------------------------------------------------------- sessions

    async def list_sessions(self) -> list[SmbSession]:
        try:
            out = await self._exec("smbstatus", "-b")
        except RuntimeError as exc:
            log.warning("smbstatus failed: %s", exc)
            return []
        return [
            SmbSession(
                pid=int(m["pid"]),
                user=m["user"],
                machine=m["machine"],
                ip=m["ip"],
                protocol=m["proto"],
            )
            for m in _SESSIONS_RE.finditer(out)
        ]

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _validate_username(username: str) -> None:
        # Kept in lockstep with the identical check in scripts/create-user.sh
        # and scripts/delete-user.sh; the scripts are the real gate, this is
        # for a friendly error message.
        if not _USERNAME_RE.match(username):
            raise ValueError(
                "username must match [a-z_][a-z0-9_-]{0,31}"
            )

    @staticmethod
    def _parse_quota(conf: Path) -> int | None:
        try:
            for line in conf.read_text().splitlines():
                key, _, val = line.strip().partition("=")
                if key.strip() == "fruit:time machine max size":
                    return int(val.strip().rstrip("Gg"))
        except (OSError, ValueError):
            pass
        return None

    async def _exec(self, *args: str, stdin: str | None = None) -> str:
        cmd = [
            "docker",
            "exec",
            "-i",
            self.settings.samba_container,
            *args,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=subprocess.PIPE if stdin is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            # Translate "docker CLI missing" into RuntimeError so callers
            # that already catch RuntimeError (list_sessions) degrade
            # gracefully instead of returning a 500 to the browser.
            raise RuntimeError(
                f"{cmd[0]} not found in PATH; install docker CLI or mount "
                f"the host binary into the web container"
            ) from exc
        stdout, stderr = await proc.communicate(
            stdin.encode() if stdin is not None else None
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"{' '.join(cmd[:4])} failed ({proc.returncode}): "
                f"{stderr.decode().strip() or stdout.decode().strip()}"
            )
        return stdout.decode()


# --------------------------------------------------------------------- usage

# path -> (expires_at, usage). A 500 GB sparsebundle is ~64k band files;
# walking it takes well over half a second warm and far longer on a drive
# that has to spin up, and both the dashboard and every Prometheus scrape
# want the number. Cache it.
_usage_cache: dict[str, tuple[float, DirUsage]] = {}


def invalidate_usage(path: Path | None = None) -> None:
    """Drop cached usage, for one path or all of them."""
    if path is None:
        _usage_cache.clear()
    else:
        _usage_cache.pop(str(path), None)


def _scan_usage(path: Path, ttl: int) -> DirUsage:
    key = str(path)
    now = time.monotonic()
    hit = _usage_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]

    usage = _walk_usage(path)
    _usage_cache[key] = (now + ttl, usage)
    return usage


def _walk_usage(path: Path) -> DirUsage:
    """Total size and newest Time Machine checkpoint in a single walk.

    Uses os.scandir rather than Path.rglob: this runs over tens of
    thousands of sparsebundle band files, and scandir reuses the stat
    data the directory read already returned instead of allocating a Path
    and issuing a fresh stat() per entry.
    """
    total = 0
    newest = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        st = entry.stat(follow_symlinks=False)
                        total += st.st_size
                        if entry.name == _TM_MARKER:
                            newest = max(newest, int(st.st_mtime))
                    except OSError:
                        continue
        except OSError:
            continue

    if newest:
        return DirUsage(used_bytes=total, last_backup_ts=newest)
    # No checkpoint marker yet: fall back to the directory's own mtime.
    try:
        return DirUsage(used_bytes=total, last_backup_ts=int(path.stat().st_mtime))
    except OSError:
        return DirUsage(used_bytes=total, last_backup_ts=None)
