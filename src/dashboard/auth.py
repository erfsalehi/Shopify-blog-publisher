"""Single shared password, one owner, one session.

Not a users table, deliberately. This app has exactly one person who is
allowed to see it — the same design decision as everything else here
(loopback-only locally, no multi-tenancy) — so a login form checking one
password against one env var is the correct amount of machinery, not a
shortcut around a "real" auth system that this app has no use for.

**Local, unauthenticated use still works.** Leave `DASHBOARD_PASSWORD` unset
and every request passes through — that was always true, and it stays true
after this module exists, so `python -m dashboard` on loopback needs no
change to anyone's `.env`.

**Once a password is set, everything is gated except three doors**: the login
page itself (or nothing could ever authenticate), `/static/*` (CSS has no
secrets in it and gating it just adds a redirect loop to every page's
network tab), and `/api/cron/*` (Vercel Cron has no browser and cannot carry
a session cookie — it proves itself with a bearer token instead, checked
separately in `cron.py`, never with the owner's password).

The session cookie is **signed, not encrypted** (`itsdangerous`, via
Starlette's `SessionMiddleware`). That is the right tool for the one bit of
state involved — "this browser passed the password check" — and the wrong
tool would be storing anything an attacker reading the cookie shouldn't see,
which this deliberately never does.
"""

from __future__ import annotations

import hmac
import time
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from dashboard.config import get_settings

SESSION_COOKIE = "dr_session"

# Paths reachable with no session at all. Prefixes, checked with startswith.
_PUBLIC_PATHS = ("/login", "/static/", "/api/cron/", "/favicon.ico")


def install_session_middleware(app) -> None:
    """Wire the signed-cookie session. Called once, at app creation.

    A missing `session_secret` while a password is set is refused outright
    rather than falling back to a random one: a secret regenerated on every
    cold start would silently log the owner out on every deploy, and on
    Vercel — many short-lived instances — effectively every few requests.
    That failure is worse than refusing to start, because it looks like the
    login form is broken rather than like a missing setting.
    """
    settings = get_settings()
    if settings.auth_required and not settings.session_secret.strip():
        raise RuntimeError(
            "DASHBOARD_PASSWORD is set but DASHBOARD_SESSION_SECRET is not. "
            "Generate one with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\" "
            "and set it before the login page can work."
        )
    # A random per-run secret is fine when auth is off — the cookie signs
    # nothing anyone relies on across a restart if there's no login gating it.
    secret = settings.session_secret.strip() or _ephemeral_secret()
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie=SESSION_COOKIE,
        max_age=settings.session_max_age_days * 24 * 3600,
        same_site="lax",
        https_only=settings.host not in ("127.0.0.1", "localhost", "::1"),
    )


def _ephemeral_secret() -> str:
    import secrets

    return secrets.token_hex(32)


def is_public(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in _PUBLIC_PATHS)


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


async def auth_guard(request: Request, call_next):
    """Redirect any unauthenticated page request to the login form.

    Registered as HTTP middleware rather than a per-route dependency because
    the alternative — remembering to add `Depends(require_login)` to every
    route in `web.py` — is exactly the kind of thing a new route added six
    months from now forgets. A default-deny gate that new code must be
    explicitly excluded from (via `_PUBLIC_PATHS`) fails safe instead.
    """
    settings = get_settings()
    if not settings.auth_required or is_public(request.url.path):
        return await call_next(request)
    if not is_authenticated(request):
        next_url = request.url.path
        if request.url.query:
            next_url += f"?{request.url.query}"
        return RedirectResponse(
            f"/login?{urlencode({'next': next_url})}", status_code=303
        )
    return await call_next(request)


def check_password(candidate: str) -> bool:
    """Constant-time comparison — a login form is exactly where timing leaks
    matter, since it's the one endpoint an attacker gets to call repeatedly."""
    expected = get_settings().password
    if not expected:
        return False
    return hmac.compare_digest(candidate.strip(), expected)


# A crude, in-memory throttle: N failed attempts per IP locks it out for a
# while. Not a substitute for a real rate limiter behind a WAF, but this is a
# one-password login with no account-lockout story otherwise, and Vercel
# functions are stateless between cold starts — so "in memory" only helps
# within one warm instance, which is still better than nothing and costs
# nothing to add.
_FAILURES: dict[str, list[float]] = {}
_MAX_ATTEMPTS = 8
_WINDOW_SECONDS = 300


def is_locked_out(client_ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _FAILURES.get(client_ip, []) if now - t < _WINDOW_SECONDS]
    _FAILURES[client_ip] = attempts
    return len(attempts) >= _MAX_ATTEMPTS


def record_failure(client_ip: str) -> None:
    _FAILURES.setdefault(client_ip, []).append(time.time())
