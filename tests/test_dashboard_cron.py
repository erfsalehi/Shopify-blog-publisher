"""Vercel Cron endpoints.

The one property worth protecting above the others: an unauthenticated
`/api/cron/*` would let anyone on the internet trigger a paid DataForSEO call
or a Shopify write by requesting a URL. Everything else here is secondary to
that.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dashboard.jobs.registry import JobResult, JobSpec, register


@pytest.fixture
def client(dashboard_db, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "test-cron-secret")
    from dashboard.config import get_settings

    get_settings.cache_clear()
    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def registered_job(dashboard_db):
    from dashboard.jobs import registry

    name = "cron_test_job"
    if name not in {j.name for j in registry.all_jobs()}:
        register(JobSpec(
            name=name, title="Cron test job", description="",
            fn=lambda: JobResult(rows=3),
        ))
    return name


def test_no_bearer_token_is_refused(client, registered_job):
    resp = client.get(f"/api/cron/{registered_job}")
    assert resp.status_code == 401


def test_the_wrong_token_is_refused(client, registered_job):
    resp = client.get(
        f"/api/cron/{registered_job}",
        headers={"Authorization": "Bearer wrong-secret"},
    )
    assert resp.status_code == 401


def test_the_right_token_runs_the_job(client, registered_job):
    resp = client.get(
        f"/api/cron/{registered_job}",
        headers={"Authorization": "Bearer test-cron-secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["rows"] == 3


def test_an_unknown_job_name_is_404(client):
    resp = client.get(
        "/api/cron/not-a-real-job",
        headers={"Authorization": "Bearer test-cron-secret"},
    )
    assert resp.status_code == 404


def test_cron_endpoints_are_refused_when_no_secret_is_configured(
    dashboard_db, monkeypatch, registered_job
):
    """The equivalent local default — no password means no login required —
    is safe because the app is unreachable from outside. A cron endpoint is
    reachable from the whole internet the moment this is deployed, so the
    default here has to be the opposite: refuse, not allow."""
    monkeypatch.delenv("CRON_SECRET", raising=False)
    from dashboard.config import get_settings

    get_settings.cache_clear()
    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        resp = c.get(
            f"/api/cron/{registered_job}",
            headers={"Authorization": "Bearer anything"},
        )
    assert resp.status_code == 503
    get_settings.cache_clear()


def test_the_cron_path_is_not_gated_by_the_owner_password(
    dashboard_db, monkeypatch, registered_job
):
    """Vercel Cron has no browser and cannot carry a session cookie — if the
    password login guard also caught this path, cron could never run at all
    no matter what token it sent."""
    monkeypatch.setenv("CRON_SECRET", "test-cron-secret")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "owner-password")
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "b" * 32)
    from dashboard.config import get_settings

    get_settings.cache_clear()
    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        resp = c.get(
            f"/api/cron/{registered_job}",
            headers={"Authorization": "Bearer test-cron-secret"},
        )
    assert resp.status_code == 200
    get_settings.cache_clear()
