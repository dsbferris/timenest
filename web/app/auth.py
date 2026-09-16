"""Very small session-based auth for the admin UI.

We intentionally keep this single-user. TimeNest is meant to live on a
home LAN behind a VPN; multi-admin RBAC would be overkill and a larger
attack surface.
"""

from __future__ import annotations

import secrets
from typing import cast

from fastapi import Depends, HTTPException, Request, status

from .config import Settings, get_settings


def _eq(a: str, b: str) -> bool:
    """Constant-time string compare that tolerates non-ASCII.

    ``secrets.compare_digest`` raises TypeError on a str containing
    codepoints above U+00FF, so compare the UTF-8 bytes instead.
    """
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_login(username: str, password: str, settings: Settings) -> bool:
    """Compare credentials against the configured admin in constant time.

    ADMIN_PASSWORD arrives as plaintext in the environment, so running it
    through bcrypt only to verify it against itself bought no security --
    the plaintext is already in this process's memory either way. A
    constant-time compare gives the same guarantee without the passlib and
    bcrypt<4 version pins that used to be needed to make it work.
    """
    user_ok = _eq(username, settings.admin_user)
    pass_ok = _eq(password, settings.admin_password)
    # `&` not `and`: both compares always run, so a wrong username takes
    # the same time as a wrong password.
    return user_ok & pass_ok


def require_login(request: Request) -> str:
    """FastAPI dependency that redirects to /login if the session is missing."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return user


def require_metrics_access(request: Request) -> None:
    """Guard /metrics.

    A logged-in session is enough, so the endpoint stays clickable from the
    browser. Prometheus has no session, so it presents
    ``Authorization: Bearer <METRICS_TOKEN>`` instead. With no token
    configured and no session, the endpoint is closed -- the series carry
    every username, per-user backup size and the disk's free space, which
    is not something to hand out to whoever can reach the port.
    """
    if request.session.get("user"):
        return

    token = get_settings().metrics_token
    if token:
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and _eq(presented, token):
            return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="metrics require a session or a bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


# Runtime value is `fastapi.params.Depends`, but FastAPI substitutes the
# resolved string at call time. Cast so endpoints can declare `user: str = LoginDep`
# without mypy complaining about a Callable default for a str argument.
LoginDep: str = cast(str, Depends(require_login))
MetricsDep: None = cast(None, Depends(require_metrics_access))
