"""FastAPI entrypoint for the TimeNest web UI."""

from __future__ import annotations

import datetime as dt
import logging
import os
import secrets
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import disks, metrics, samba_mgr
from .auth import LoginDep, MetricsDep, verify_login
from .config import Settings, get_settings

# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------
app = FastAPI(title="TimeNest", docs_url=None, redoc_url=None)

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("timenest.web")

# Session secret persists across restarts so users don't get logged out on
# container updates. Generated once and stashed alongside the data volume.
session_secret = settings.session_secret
if not session_secret:
    secret_file = settings.data_dir / ".session_secret"
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if secret_file.exists():
            session_secret = secret_file.read_text().strip()
        else:
            session_secret = secrets.token_urlsafe(64)
            secret_file.write_text(session_secret)
            secret_file.chmod(0o600)
    except OSError:
        session_secret = secrets.token_urlsafe(64)

# No CSRF tokens: `same_site="lax"` means a browser will not attach this
# cookie to a cross-site form POST, which is the whole attack surface here
# (every mutating route is a form POST). Don't loosen this to "none"
# without adding tokens.
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    session_cookie="timenest_session",
    max_age=60 * 60 * 24 * 7,
    same_site="lax",
    https_only=settings.session_https_only,
)

_static_dir = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

_templates_dir = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=_templates_dir)
templates.env.globals["version"] = "0.1.0"


def _mgr() -> samba_mgr.SambaManager:
    return samba_mgr.SambaManager(settings)


def _redirect(path: str, **params: str) -> RedirectResponse:
    """Redirect with properly encoded query parameters.

    urlencode matters: an error message containing `&` or `#` would
    otherwise truncate or split the parameter.
    """
    query = urlencode({k: v for k, v in params.items() if v})
    url = f"{path}?{query}" if query else path
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "-"
    if n < 1024:
        return f"{n} B"
    size = float(n)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        size /= 1024
        if size < 1024 or unit == "PB":
            return f"{size:.1f} {unit}"
    raise AssertionError("unreachable")  # pragma: no cover


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "never"
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_quota(gb: int | None) -> str:
    return f"{gb} GB" if gb is not None else "unknown"


templates.env.filters["bytes"] = _fmt_bytes
templates.env.filters["ts"] = _fmt_ts
templates.env.filters["quota"] = _fmt_quota


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> Response:
    if request.session.get("user"):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    cfg: Settings = Depends(get_settings),
) -> Response:
    if not verify_login(username, password, cfg):
        log.warning("failed login for '%s' from %s", username, request.client.host if request.client else "?")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid credentials"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    request.session["user"] = username
    log.info("login ok: %s", username)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = LoginDep) -> Response:
    mgr = _mgr()
    users = await mgr.alist_users()
    sessions = await mgr.list_sessions()
    du = disks.usage(settings.backup_path)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "page": "dashboard",
            "users": users,
            "sessions": sessions,
            "disk": du,
            "backup_path": str(settings.backup_path),
        },
    )


@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, user: str = LoginDep) -> Response:
    # Sync endpoint: FastAPI runs it in a worker thread, so the blocking
    # backup-tree scan inside list_users() stays off the event loop.
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "page": "users",
            "users": _mgr().list_users(),
            "default_quota_gb": settings.default_quota_gb,
            "max_quota_gb": settings.max_quota_gb,
            "created": request.query_params.get("created"),
            "deleted": request.query_params.get("deleted"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/users")
async def users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    quota_gb: int = Form(...),
    user: str = LoginDep,
) -> Response:
    try:
        await _mgr().create_user(username, password, quota_gb)
    except (ValueError, RuntimeError) as exc:
        return _redirect("/users", error=str(exc))
    log.info("created user '%s' (%d GB quota)", username, quota_gb)
    return _redirect("/users", created=username)


@app.post("/users/{username}/delete")
async def users_delete(
    username: str,
    purge: str = Form(default=""),
    user: str = LoginDep,
) -> Response:
    try:
        await _mgr().delete_user(username, purge=bool(purge))
    except (ValueError, RuntimeError) as exc:
        return _redirect("/users", error=str(exc))
    log.info("deleted user '%s' (purge=%s)", username, bool(purge))
    return _redirect("/users", deleted=username)


@app.get("/disks", response_class=HTMLResponse)
async def disks_page(request: Request, user: str = LoginDep) -> Response:
    du = disks.usage(settings.backup_path)
    # Probe concurrently; missing devices simply show "unavailable".
    smart_results = await disks.smart_all(_probe_devices(), settings.smart_cache_ttl)
    return templates.TemplateResponse(
        request,
        "disks.html",
        {
            "page": "disks",
            "disk": du,
            "smart": smart_results,
            "backup_path": str(settings.backup_path),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: str = LoginDep) -> Response:
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "page": "settings",
            "settings": {
                "admin_user": settings.admin_user,
                "backup_path": str(settings.backup_path),
                "default_quota_gb": settings.default_quota_gb,
                "max_quota_gb": settings.max_quota_gb,
                "log_level": settings.log_level,
                "timezone": settings.timezone,
                "enable_metrics": settings.enable_metrics,
                "metrics_token": "set" if settings.metrics_token else "not set",
                "session_https_only": settings.session_https_only,
            },
        },
    )


@app.get("/metrics")
async def metrics_endpoint(_: None = MetricsDep) -> Response:
    if not settings.enable_metrics:
        return Response("metrics disabled\n", status_code=404, media_type="text/plain")
    body = await metrics.render(settings, _mgr())
    return Response(body, media_type=metrics.CONTENT_TYPE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _probe_devices() -> list[str]:
    """Enumerate likely disk device nodes in a sensible order.

    On Raspberry Pi the backup drive is typically /dev/sda; on NUCs and
    generic Linux boxes it is /dev/sdb; on Mac mini we skip because the
    container cannot read raw disk devices through Docker Desktop.
    """
    return [
        p
        for p in ("/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/nvme0n1", "/dev/nvme1n1")
        if os.path.exists(p)
    ]
