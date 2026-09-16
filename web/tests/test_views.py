"""Template rendering, formatting filters, and redirect handling."""

import pytest

from conftest import make_sparsebundle, write_share


@pytest.mark.parametrize("path", ["/", "/users", "/disks", "/settings"])
def test_pages_render(auth_client, env, path):
    """Guards the deprecated TemplateResponse signature, which blew up with
    `TypeError: unhashable type: dict` on newer Starlette."""
    write_share(env.shares, "bob", 500)
    make_sparsebundle(env.backup, "bob")
    r = auth_client.get(path)
    assert r.status_code == 200
    assert "TimeNest" in r.text


def test_login_page_renders_error(client):
    r = client.post(
        "/login", data={"username": "admin", "password": "nope"}, follow_redirects=False
    )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text


def test_users_page_shows_unknown_quota(auth_client, env):
    write_share(env.shares, "bob", None)
    r = auth_client.get("/users")
    assert "unknown" in r.text


def test_create_user_error_is_urlencoded(auth_client):
    """An unencoded `&` in the message used to truncate the query string."""
    r = auth_client.post(
        "/users",
        data={"username": "BAD NAME", "password": "pw", "quota_gb": "500"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/users?error=")
    assert " " not in loc

    body = auth_client.get(loc).text
    assert "username must match" in body


def test_delete_failure_redirects_instead_of_raw_json(auth_client):
    """Used to raise HTTPException(400) and render a bare JSON error page."""
    r = auth_client.post("/users/bob/delete", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/users?")

    body = auth_client.get(r.headers["location"]).text
    assert "<!DOCTYPE html>" in body or "TimeNest" in body


def test_no_unused_cdn_scripts(auth_client):
    r = auth_client.get("/users")
    assert "htmx.org" not in r.text
    assert "alpinejs" not in r.text


def test_settings_page_does_not_claim_sessions_are_invalidated(auth_client):
    """The old copy promised a password change logged everyone out. It did not."""
    r = auth_client.get("/settings")
    assert "invalidated automatically" not in r.text
    assert ".session_secret" in r.text


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "-"),
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024**2, "1.0 MB"),
        (1024**3, "1.0 GB"),
        (1024**4, "1.0 TB"),
        (1024**5, "1.0 PB"),
        (1024**6, "1024.0 PB"),
    ],
)
def test_fmt_bytes(env, value, expected):
    from app.main import _fmt_bytes

    assert _fmt_bytes(value) == expected


def test_fmt_ts_and_quota(env):
    from app.main import _fmt_quota, _fmt_ts

    assert _fmt_ts(None) == "never"
    assert _fmt_ts(0) == "never"
    assert _fmt_ts(1_700_000_000).startswith("20")
    assert _fmt_quota(None) == "unknown"
    assert _fmt_quota(500) == "500 GB"
