"""Disk usage and SMART health for the admin dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiskUsage:
    mount: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent: float


@dataclass(frozen=True, slots=True)
class SmartStatus:
    device: str
    model: str
    serial: str
    healthy: bool
    temperature_c: int | None
    power_on_hours: int | None
    hours_until_replace_est: int | None


def usage(mount: Path | str) -> DiskUsage | None:
    try:
        s = psutil.disk_usage(str(mount))
    except OSError:
        return None
    return DiskUsage(
        mount=str(mount),
        total_bytes=s.total,
        used_bytes=s.used,
        free_bytes=s.free,
        percent=s.percent,
    )


# device -> (expires_at, status). smartctl wakes a sleeping drive, which on
# a backup appliance is exactly what we do not want to do on every page
# view, so results are cached for a good while.
_smart_cache: dict[str, tuple[float, SmartStatus | None]] = {}


async def smart_all(devices: list[str], ttl: int) -> list[SmartStatus]:
    """Probe every device concurrently, skipping the ones that are cached."""
    results = await asyncio.gather(*(smart(d, ttl) for d in devices))
    return [r for r in results if r is not None]


async def smart(device: str, ttl: int = 0) -> SmartStatus | None:
    """Run smartctl and parse the JSON output. Returns None if unavailable."""
    now = time.monotonic()
    hit = _smart_cache.get(device)
    if hit and hit[0] > now:
        return hit[1]

    result = await _probe(device)
    if ttl:
        _smart_cache[device] = (now + ttl, result)
    return result


async def _probe(device: str) -> SmartStatus | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "smartctl",
            "-a",
            "-j",
            device,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as exc:
        # smartmontools missing, or the container cannot exec it. Same
        # contract as any other unavailable device: degrade, don't 500.
        log.debug("cannot run smartctl for %s: %s", device, exc)
        return None
    stdout, stderr = await proc.communicate()
    if proc.returncode not in (0, 2, 4):
        # smartctl uses bitmask exit codes; 2 = "opened read-only", 4 =
        # "some SMART commands failed" - both still yield parseable JSON.
        log.debug("smartctl %s exited %d: %s", device, proc.returncode, stderr)
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None

    healthy = data.get("smart_status", {}).get("passed", False)
    temp = data.get("temperature", {}).get("current")
    poh = data.get("power_on_time", {}).get("hours")
    return SmartStatus(
        device=device,
        model=data.get("model_name", "unknown"),
        serial=data.get("serial_number", "unknown"),
        healthy=bool(healthy),
        temperature_c=temp,
        power_on_hours=poh,
        hours_until_replace_est=_estimate_replace_hours(data),
    )


def _estimate_replace_hours(data: dict) -> int | None:
    # Very rough: if the drive is an SSD with a percent-used attribute,
    # linearly extrapolate remaining life from power-on hours.
    attrs = data.get("ata_smart_attributes", {}).get("table", []) or []
    poh = data.get("power_on_time", {}).get("hours")
    if not poh:
        return None
    for attr in attrs:
        if attr.get("name") in ("Percent_Lifetime_Remain", "Wear_Leveling_Count"):
            raw = attr.get("value")
            if isinstance(raw, int) and 0 < raw <= 100:
                used_pct = 100 - raw
                if used_pct <= 0:
                    return None
                return int(poh * (100 - used_pct) / used_pct)
    return None
