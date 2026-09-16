"""Auth and the /metrics guard -- the two high-severity review findings."""

import pytest

from conftest import ADMIN_PASSWORD


def test_login_accepts_correct_credentials(client):
    r = client.post(
        "/login",
        data={"username": "admin", "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


@pytest.mark.parametrize(
    "username,password",
    [
        ("admin", "wrong"),
        ("wrong", ADMIN_PASSWORD),
        ("admin", ADMIN_PASSWORD + "x"),
    ],
)
def test_login_rejects_bad_credentials(client, username, password):
    r = client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert r.status_code == 401


def test_login_rejects_empty_password(client):
    # 401 from the credential check, or 422 if the form layer rejects the
    # empty field first -- which of the two depends on the starlette version.
    r = client.post(
        "/login", data={"username": "admin", "password": ""}, follow_redirects=False
    )
    assert r.status_code in (401, 422)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_login_handles_non_ascii_without_crashing(client):
    """compare_digest on str raises TypeError above U+00FF; we encode first."""
    r = client.post(
        "/login", data={"username": "админ", "password": "пароль"}, follow_redirects=False
    )
    assert r.status_code == 401


@pytest.mark.parametrize("path", ["/", "/users", "/disks", "/settings"])
def test_pages_require_login(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_health_is_public(client):
    assert client.get("/health").status_code == 200


def test_metrics_rejects_anonymous(client):
    """The finding: this used to return 200 with every username in the body."""
    r = client.get("/metrics")
    assert r.status_code == 401
    assert "timenest_users_total" not in r.text


def test_metrics_allows_session(auth_client):
    r = auth_client.get("/metrics")
    assert r.status_code == 200
    assert "timenest_users_total" in r.text


def test_metrics_allows_bearer_token(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "s3cret-token")
    from app import config

    config.get_settings.cache_clear()

    assert client.get("/metrics").status_code == 401
    assert (
        client.get(
            "/metrics", headers={"Authorization": "Bearer wrong-token"}
        ).status_code
        == 401
    )
    r = client.get("/metrics", headers={"Authorization": "Bearer s3cret-token"})
    assert r.status_code == 200


def test_logout_clears_session(auth_client):
    auth_client.get("/logout", follow_redirects=False)
    assert auth_client.get("/", follow_redirects=False).status_code == 303
