"""The settings store, the chart renderer, and that every page renders.

The web tests are deliberately shallow — they assert the pages come back 200
with the right numbers in them, not how they look. What they do pin down is
the architectural rule: a page load must not make an outbound call. That is
easy to break by accident (one convenience import of a live client) and
impossible to notice locally until the proxy has a bad morning.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from dashboard import charts, store
from dashboard.db import get_session
from dashboard.models import AppSetting, Competitor, GscSiteDaily

TODAY = date.today()


@pytest.fixture
def client(dashboard_db):
    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        yield c


# ── Settings store ─────────────────────────────────────────────────


def test_an_unset_setting_reads_its_declared_default(dashboard_db):
    assert store.get(store.GSC_RECENT_DAYS) == 10
    with get_session() as session:
        assert session.query(AppSetting).count() == 0  # nothing written


def test_values_outside_the_declared_bounds_are_rejected(dashboard_db):
    with pytest.raises(ValueError):
        store.set(store.GSC_RECENT_DAYS, 1)
    with pytest.raises(ValueError):
        store.set(store.GSC_BACKFILL_DAYS, 9999)


def test_an_unknown_key_is_a_hard_error(dashboard_db):
    """A typo'd key must not silently become a new setting nobody reads."""
    with pytest.raises(KeyError):
        store.get("gsc.recent_dayz")


def test_a_bad_value_in_a_batch_saves_none_of_the_batch(dashboard_db):
    """The settings form posts every field at once. A partial save would leave
    the page showing a mix of old and new with no way to tell which is which."""
    with pytest.raises(ValueError):
        store.set_many([
            (store.GSC_RECENT_DAYS, 20),
            (store.GSC_BACKFILL_DAYS, -5),
        ])
    assert store.get(store.GSC_RECENT_DAYS) == 10  # still the default


def test_a_stored_value_that_no_longer_validates_falls_back_to_the_default(
    dashboard_db,
):
    with get_session() as session:
        session.add(AppSetting(key=store.GSC_RECENT_DAYS, value_json="999999"))
    assert store.get(store.GSC_RECENT_DAYS) == 10


# ── Charts ─────────────────────────────────────────────────────────


def test_the_provisional_tail_is_drawn_separately(dashboard_db):
    points = [
        charts.Point("2026-08-01", 10),
        charts.Point("2026-08-02", 12),
        charts.Point("2026-08-03", 4, provisional=True),
    ]
    svg = str(charts.line_chart(points))
    assert "chart-line-provisional" in svg
    assert "chart-boundary" in svg
    # The tooltip has to say so too — the dash is invisible to a screen reader.
    assert "(provisional)" in svg


def test_a_chart_with_no_points_says_so_instead_of_drawing_an_empty_axis(
    dashboard_db,
):
    out = str(charts.line_chart([], empty_note="No data yet"))
    assert "<svg" not in out
    assert "No data yet" in out


def test_chart_labels_are_escaped(dashboard_db):
    svg = str(charts.line_chart([charts.Point("<script>", 1)]))
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


# ── Pages ──────────────────────────────────────────────────────────


def test_the_overview_renders_on_an_empty_database(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Nothing synced yet" in resp.text


def test_the_overview_shows_synced_numbers(client):
    end = TODAY - timedelta(days=3)
    with get_session() as session:
        for n in range(7):
            session.add(
                GscSiteDaily(date=end - timedelta(days=n), clicks=111,
                             impressions=2000, ctr=0.055, position=7.5)
            )
    resp = client.get("/?window=7")
    assert resp.status_code == 200
    assert "777" in resp.text  # 7 days x 111 clicks, summed not averaged


def test_the_jobs_page_lists_the_registered_jobs(client):
    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert "Search Console daily sync" in resp.text


def test_the_settings_page_renders_every_declared_setting(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    for spec in store.SPECS:
        assert spec.key in resp.text


def test_saving_settings_persists_and_redirects(client):
    resp = client.post(
        "/settings",
        data={
            store.GSC_BACKFILL_DAYS: "90",
            store.GSC_RECENT_DAYS: "7",
            store.GSC_PAGE_CHUNK_DAYS: "7",
            store.GSC_PAGE_ROW_LIMIT: "60000",
            store.JOB_GSC_HOUR: "4",
            store.JOB_HISTORY_KEEP: "100",
            store.JOB_GSC_ENABLED: "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert store.get(store.GSC_BACKFILL_DAYS) == 90
    assert store.get(store.JOB_GSC_HOUR) == 4


def test_an_unchecked_box_saves_as_false(client):
    """An unchecked checkbox posts nothing at all, so a naive handler would
    read 'absent' as 'unchanged' and the toggle could never be turned off."""
    store.set(store.JOB_GSC_ENABLED, True)
    client.post(
        "/settings",
        data={store.GSC_BACKFILL_DAYS: "90", store.GSC_RECENT_DAYS: "7",
              store.GSC_PAGE_CHUNK_DAYS: "7", store.GSC_PAGE_ROW_LIMIT: "60000",
              store.JOB_GSC_HOUR: "4", store.JOB_HISTORY_KEEP: "100"},
        follow_redirects=False,
    )
    assert store.get(store.JOB_GSC_ENABLED) is False


def test_competitors_can_be_added_and_removed(client):
    client.post(
        "/settings/competitors",
        data={"name": "BC Floors", "base_url": "https://example.ca",
              "price_selector": "", "notes": ""},
        follow_redirects=False,
    )
    with get_session() as session:
        rows = session.query(Competitor).all()
        assert [r.name for r in rows] == ["BC Floors"]
        # Left null rather than stored as "" so the extractor can tell
        # "use the default order" from "the owner set a selector".
        assert rows[0].price_selector is None
        competitor_id = rows[0].id

    client.post(f"/settings/competitors/{competitor_id}/delete",
                follow_redirects=False)
    with get_session() as session:
        assert session.query(Competitor).count() == 0


def test_running_an_unknown_job_is_a_404(client):
    assert client.post("/jobs/nope/run").status_code == 404


def test_no_page_makes_an_outbound_request(client, monkeypatch):
    """The architectural rule, asserted. If a page ever needs live data, that
    is a sync job that hasn't been written yet."""
    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("a page handler made a live HTTP call")

    for name in ("get", "post", "request"):
        monkeypatch.setattr(httpx, name, forbidden)

    for path in ("/", "/jobs", "/settings", "/jobs/status", "/import"):
        assert client.get(path).status_code == 200


# ── Database URL resolution ─────────────────────────────────────────


def test_the_pipelines_database_url_is_never_silently_adopted(monkeypatch):
    """DASHBOARD_DATABASE_URL falls back to the bare DATABASE_URL / POSTGRES_URL
    so Vercel's Neon integration works with no manual wiring — but this repo's
    own DATABASE_URL is the PIPELINE's database, a different schema entirely.
    An explicit DASHBOARD_DATABASE_URL must always win over that fallback, or
    the dashboard would try to create its own tables inside the pipeline's
    database file the moment the fallback existed."""
    from dashboard.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/pipeline.db")
    monkeypatch.setenv("DASHBOARD_DATABASE_URL", "sqlite:///data/dashboard.db")
    monkeypatch.setitem(
        __import__("dashboard.config", fromlist=["DashboardSettings"])
        .DashboardSettings.model_config, "env_file", None,
    )
    get_settings.cache_clear()
    try:
        assert get_settings().database_url == "sqlite:///data/dashboard.db"
    finally:
        get_settings.cache_clear()


def test_database_url_falls_back_to_the_bare_env_var_when_unset(monkeypatch):
    """The other half: Vercel's Neon integration sets DATABASE_URL with no
    idea this app expects a DASHBOARD_ prefix. Without the fallback, connecting
    Neon through Vercel's UI would silently leave the app pointed at a SQLite
    path with no disk under it."""
    from dashboard.config import get_settings

    monkeypatch.delenv("DASHBOARD_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://neon-test/db")
    monkeypatch.setitem(
        __import__("dashboard.config", fromlist=["DashboardSettings"])
        .DashboardSettings.model_config, "env_file", None,
    )
    get_settings.cache_clear()
    try:
        assert get_settings().database_url == "postgresql://neon-test/db"
    finally:
        get_settings.cache_clear()


# ── Sharing one database with the pipeline (Vercel only) ────────────


def test_the_two_schemas_can_share_one_database():
    """On Vercel there is one Neon database and both packages read
    DATABASE_URL, so the schemas coexist whether or not that was ever the
    plan. They may not share a table name: create_all would silently skip
    the second definition of it, and one package would then be reading the
    other's columns. Nothing else enforces this — the two Bases are declared
    in different packages that never import each other."""
    from blog_pipeline.db.models import Base as pipeline_base

    from dashboard.models import Base as dashboard_base

    overlap = set(pipeline_base.metadata.tables) & set(
        dashboard_base.metadata.tables
    )
    assert overlap == set(), f"table name collision: {sorted(overlap)}"


def test_separate_databases_are_left_alone(dashboard_db, monkeypatch):
    """The local shape: the pipeline has its own database and its own
    `blog-pipeline init-db`. The dashboard must not reach in and create the
    pipeline's tables inside its own file just because it can."""
    from dashboard import db as dash_db

    # dashboard_db points DASHBOARD_DATABASE_URL at a temp file; conftest
    # points the pipeline's DATABASE_URL at a different one.
    assert dash_db.shares_database_with_pipeline() is False
    assert dash_db.init_pipeline_db() is False


def test_a_shared_database_gets_the_pipelines_tables_too(monkeypatch, tmp_path):
    """The Vercel shape. The Blog page reads the pipeline's `article` table
    directly, and on Vercel nothing has run `blog-pipeline init-db` against
    Neon — the page 500s with `relation "article" does not exist` until the
    dashboard's own startup creates it."""
    import blog_pipeline.config as pipeline_config
    import blog_pipeline.db.session as pipeline_session
    from sqlalchemy import inspect

    import dashboard.config as dash_config
    from dashboard import db as dash_db

    shared = tmp_path / "shared.db"
    url = f"sqlite:///{shared.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DASHBOARD_DATABASE_URL", url)
    monkeypatch.setitem(dash_config.DashboardSettings.model_config, "env_file", None)
    monkeypatch.setitem(pipeline_config.Settings.model_config, "env_file", None)
    pipeline_config.get_settings.cache_clear()
    pipeline_session._engine = None
    pipeline_session._SessionLocal = None
    dash_db.reset_engine()
    try:
        assert dash_db.shares_database_with_pipeline() is True
        dash_db.init_db()
        assert dash_db.init_pipeline_db() is True

        tables = set(inspect(dash_db.get_engine()).get_table_names())
        assert "article" in tables       # the pipeline's, the one that broke
        assert "job_run" in tables       # the dashboard's, still there
    finally:
        dash_db.reset_engine()
        pipeline_config.get_settings.cache_clear()
        pipeline_session._engine = None
        pipeline_session._SessionLocal = None
