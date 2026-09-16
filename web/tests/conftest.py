import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ADMIN_PASSWORD = "correct-horse-battery"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A fully isolated Settings environment.

    Every test gets its own data/backup/shares directories so nothing
    touches a real deployment.
    """
    backup = tmp_path / "backup"
    shares = tmp_path / "shares.d"
    data = tmp_path / "data"
    for d in (backup, shares, data):
        d.mkdir()

    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("BACKUP_PATH", str(backup))
    monkeypatch.setenv("SHARES_PATH", str(shares))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("SAMBA_CONTAINER", "does-not-exist")
    monkeypatch.delenv("METRICS_TOKEN", raising=False)

    # get_settings is lru_cached and main.py caches a module-level copy.
    from app import config

    config.get_settings.cache_clear()
    for mod in ("app.main", "app.auth", "app.samba_mgr", "app.metrics", "app.disks"):
        sys.modules.pop(mod, None)

    yield type("Env", (), {"backup": backup, "shares": shares, "data": data})

    config.get_settings.cache_clear()


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_client(client):
    r = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return client


def write_share(shares: Path, username: str, quota_gb: int | None = 500) -> None:
    quota = f"    fruit:time machine max size = {quota_gb}G\n" if quota_gb else ""
    (shares / f"{username}.conf").write_text(
        f"[{username}]\n    path = /backup/{username}\n{quota}"
    )


def make_sparsebundle(backup: Path, username: str, bands: int = 3, size: int = 16):
    """A minimal stand-in for a Time Machine sparsebundle."""
    d = backup / username / "backup.sparsebundle" / "bands"
    d.mkdir(parents=True)
    for i in range(bands):
        (d / f"{i:x}").write_bytes(b"\0" * size)
    return backup / username
