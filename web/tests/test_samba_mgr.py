"""Username/quota validation, quota parsing, and the cached usage scan."""

import asyncio

import pytest

from conftest import make_sparsebundle, write_share


@pytest.fixture()
def mgr(env):
    from app.config import get_settings
    from app.samba_mgr import SambaManager, invalidate_usage

    invalidate_usage()
    return SambaManager(get_settings())


@pytest.mark.parametrize("name", ["moamen", "a", "_x", "tm-mac_1", "a" * 32])
def test_valid_usernames(mgr, name):
    mgr._validate_username(name)


@pytest.mark.parametrize(
    "name",
    ["", "1abc", "A", "with space", "semi;colon", "../escape", "a" * 33, "-lead"],
)
def test_invalid_usernames(mgr, name):
    with pytest.raises(ValueError):
        mgr._validate_username(name)


def test_quota_below_minimum_rejected(mgr):
    with pytest.raises(ValueError, match="at least 10 GB"):
        asyncio.run(mgr.create_user("bob", "pw", 9))


def test_quota_above_maximum_rejected(mgr):
    """Server-side bound; the form's max= attribute is only a hint."""
    with pytest.raises(ValueError, match="at most"):
        asyncio.run(mgr.create_user("bob", "pw", 10_000_000))


def test_parse_quota_reads_fruit_setting(mgr, env):
    write_share(env.shares, "bob", 750)
    assert mgr._parse_quota(env.shares / "bob.conf") == 750


def test_parse_quota_returns_none_when_absent(mgr, env):
    """None, not 0 -- 0 GB would look like a real (empty) quota."""
    write_share(env.shares, "bob", None)
    assert mgr._parse_quota(env.shares / "bob.conf") is None


def test_list_users_reports_size_and_quota(mgr, env):
    write_share(env.shares, "bob", 500)
    make_sparsebundle(env.backup, "bob", bands=4, size=16)

    users = mgr.list_users()
    assert [u.username for u in users] == ["bob"]
    assert users[0].quota_gb == 500
    assert users[0].used_bytes == 64


def test_list_users_sorted_and_empty_when_unmounted(mgr, env):
    for name in ("zoe", "amy"):
        write_share(env.shares, name)
    assert [u.username for u in mgr.list_users()] == ["amy", "zoe"]


def test_last_backup_uses_checkpoint_marker(mgr, env):
    from app.samba_mgr import _TM_MARKER, _walk_usage

    d = make_sparsebundle(env.backup, "bob")
    marker = d / "backup.sparsebundle" / _TM_MARKER
    marker.write_text("")
    import os

    os.utime(marker, (1_700_000_000, 1_700_000_000))

    assert _walk_usage(d).last_backup_ts == 1_700_000_000


def test_walk_usage_survives_missing_directory(env):
    from app.samba_mgr import _walk_usage

    usage = _walk_usage(env.backup / "nope")
    assert usage.used_bytes == 0
    assert usage.last_backup_ts is None


def test_usage_is_cached_and_invalidated(mgr, env):
    """The perf finding: one walk should not repeat on every request."""
    from app import samba_mgr

    write_share(env.shares, "bob")
    make_sparsebundle(env.backup, "bob", bands=2, size=8)

    calls = []
    real = samba_mgr._walk_usage

    def counting(path):
        calls.append(path)
        return real(path)

    samba_mgr._walk_usage = counting
    try:
        mgr.list_users()
        mgr.list_users()
        mgr.list_users()
        assert len(calls) == 1, "usage scan should be cached across requests"

        samba_mgr.invalidate_usage(env.backup / "bob")
        mgr.list_users()
        assert len(calls) == 2, "mutating a user should drop its cache entry"
    finally:
        samba_mgr._walk_usage = real


def test_exec_reports_missing_docker_as_runtime_error(mgr, monkeypatch):
    async def boom(*a, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    with pytest.raises(RuntimeError, match="not found in PATH"):
        asyncio.run(mgr._exec("true"))


def test_password_is_passed_on_stdin_not_argv(mgr, monkeypatch):
    """The finding: argv is world-readable via `ps` on the host."""
    seen = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, data=None):
            seen["stdin"] = data
            return b"", b""

    async def fake_exec(*cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(mgr.create_user("bob", "hunter2", 500))

    assert "hunter2" not in " ".join(seen["cmd"])
    assert seen["stdin"] == b"hunter2\n"
    assert seen["cmd"][-2:] == ("bob", "500")
