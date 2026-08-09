"""The login gate.

Two states this app can be in, and both have to be tested against real HTTP
requests rather than the unit-level helpers alone — a middleware bug is
exactly the kind of thing that looks right in isolation and wrong wired up.

  * **No password set** — local dev, unchanged from before this module
    existed. Every page must still be reachable directly.
  * **Password set** — every page redirects to /login except the three doors
    that have to stay open for login itself to be possible.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dashboard import auth


@pytest.fixture
def open_client(dashboard_db):
    """No DASHBOARD_PASSWORD set — the original, unauthenticated behaviour."""
    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def gated_client(dashboard_db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct-horse")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "a" * 32)
    from dashboard.config import get_settings

    get_settings.cache_clear()
    from dashboard.web import create_app

    with TestClient(create_app(), follow_redirects=False) as c:
        yield c
    get_settings.cache_clear()


# ── Unauthenticated mode is unchanged ──────────────────────────────


def test_every_page_is_reachable_with_no_password_set(open_client):
    for path in ("/", "/products", "/keywords", "/blog", "/ads", "/alerts",
                 "/experiments", "/jobs", "/settings"):
        assert open_client.get(path).status_code == 200


def test_login_redirects_home_when_auth_is_off(open_client):
    resp = open_client.get("/login", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


# ── Gated mode ──────────────────────────────────────────────────────


def test_every_page_redirects_to_login_when_a_password_is_set(gated_client):
    for path in ("/", "/products", "/jobs", "/settings"):
        resp = gated_client.get(path)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")


def test_static_and_login_stay_reachable_with_no_session(gated_client):
    """Or the login page itself could never load its own CSS, and nothing
    could ever authenticate in the first place."""
    assert gated_client.get("/login").status_code == 200
    assert gated_client.get("/static/app.css").status_code == 200


def test_the_wrong_password_is_rejected(gated_client):
    resp = gated_client.post("/login", data={"password": "wrong", "next": "/"})
    assert resp.status_code == 401
    assert "session" not in gated_client.cookies or not gated_client.get(
        "/", follow_redirects=False
    ).status_code == 200


def test_the_right_password_grants_access_to_every_page(gated_client):
    login = gated_client.post(
        "/login", data={"password": "correct-horse", "next": "/"}
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/"
    for path in ("/", "/products", "/keywords", "/jobs", "/settings"):
        assert gated_client.get(path).status_code == 200


def test_signing_out_revokes_access(gated_client):
    gated_client.post("/login", data={"password": "correct-horse", "next": "/"})
    assert gated_client.get("/").status_code == 200
    gated_client.post("/logout")
    resp = gated_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_login_redirects_back_to_where_the_owner_was_headed(gated_client):
    gated_client.get("/jobs")  # redirected, but records ?next=/jobs
    resp = gated_client.post(
        "/login", data={"password": "correct-horse", "next": "/jobs"}
    )
    assert resp.headers["location"] == "/jobs"


def test_an_off_site_next_url_is_never_honoured(gated_client):
    """The login form's own field is attacker-controlled input the moment
    this is public — an open redirect here turns the login page into a
    phishing launchpad wearing this app's own domain."""
    resp = gated_client.post(
        "/login",
        data={"password": "correct-horse", "next": "https://evil.example/steal"},
    )
    assert resp.headers["location"] == "/"

    resp2 = gated_client.post(
        "/login", data={"password": "correct-horse", "next": "//evil.example"}
    )
    assert resp2.headers["location"] == "/"


def test_repeated_failures_are_locked_out(gated_client):
    """A one-password login with no account-lockout story otherwise is a
    login worth throttling — it's the one endpoint an attacker can call
    unlimited times."""
    for _ in range(auth._MAX_ATTEMPTS):
        gated_client.post("/login", data={"password": "wrong", "next": "/"})
    resp = gated_client.post(
        "/login", data={"password": "correct-horse", "next": "/"}
    )
    assert resp.status_code == 429


# ── Unit-level checks ───────────────────────────────────────────────


def test_check_password_is_constant_time_safe(dashboard_db, monkeypatch):
    """Not asserting timing directly — asserting the comparison function used
    is the constant-time one, which is the part a future edit could silently
    swap for `==` without any test noticing behaviourally."""
    import inspect

    assert "compare_digest" in inspect.getsource(auth.check_password)


def test_an_empty_configured_password_never_matches(dashboard_db, monkeypatch):
    """An unset password must mean 'no login required', handled by
    auth_required — never 'log in with a blank password'."""
    from dashboard.config import get_settings

    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    get_settings.cache_clear()
    assert auth.check_password("") is False
    get_settings.cache_clear()


def test_session_middleware_refuses_to_start_without_a_secret(
    dashboard_db, monkeypatch
):
    """A secret regenerated on every cold start would silently log the owner
    out on every Vercel deploy — refusing to start is the louder, safer
    failure."""
    from fastapi import FastAPI

    from dashboard.config import get_settings

    monkeypatch.setenv("DASHBOARD_PASSWORD", "x")
    monkeypatch.delenv("DASHBOARD_SESSION_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="SESSION_SECRET"):
            auth.install_session_middleware(FastAPI())
    finally:
        get_settings.cache_clear()


def test_trailing_whitespace_on_secrets_is_stripped_at_the_boundary(monkeypatch):
    """`vercel env add` reads its value from stdin, and echo-piping a value in
    appends a trailing newline that becomes part of the stored secret — this
    happened for real while deploying. Vercel's own build refuses a CRON_SECRET
    with trailing whitespace loudly, but the same bug on DASHBOARD_PASSWORD
    would silently reject every correct login attempt instead."""
    from dashboard.config import DashboardSettings, get_settings

    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct-horse\n")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", " session-secret ")
    monkeypatch.setenv("CRON_SECRET", "cron-secret\n")
    monkeypatch.setitem(DashboardSettings.model_config, "env_file", None)
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.password == "correct-horse"
        assert s.session_secret == "session-secret"
        assert s.cron_secret == "cron-secret"
    finally:
        get_settings.cache_clear()


def test_trailing_whitespace_on_a_boolean_flag_does_not_kill_the_app(monkeypatch):
    """The same newline, but on DASHBOARD_ENABLE_SCHEDULER — and far worse than
    on a secret. Pydantic's bool parser doesn't strip before matching against
    "true"/"false", so `'false\\n'` isn't a misread flag, it's a ValidationError
    raised inside get_settings() at import time. That took the whole deployed
    app down with a 500 on every route, login page included, because create_app
    reaches settings before it can serve anything. Stripping the three secrets
    wasn't enough: every env-sourced field needs to survive the newline."""
    from dashboard.config import DashboardSettings, get_settings

    monkeypatch.setenv("DASHBOARD_ENABLE_SCHEDULER", "false\n")
    monkeypatch.setitem(DashboardSettings.model_config, "env_file", None)
    get_settings.cache_clear()
    try:
        assert get_settings().enable_scheduler is False
    finally:
        get_settings.cache_clear()
