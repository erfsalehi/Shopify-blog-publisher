"""Alert rules, deduplication, and the blog decay ranking.

The thing most worth protecting here isn't whether a rule fires — it's whether
it fires *repeatedly*. An alerting system that files a fresh row every night
for the same unchanged condition gets muted within a week, and it takes the
real alerts with it when it goes.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from dashboard import alerts, reporting
from dashboard.db import get_session
from dashboard.models import (
    AdsCampaignDaily,
    Alert,
    AlertRule,
    BlogArticle,
    Ga4EventDaily,
    GscPageDaily,
    GscSiteDaily,
    JobRun,
    JobStatus,
)

TODAY = date(2026, 8, 5)
SETTLED = TODAY - timedelta(days=3)


def _only_rule(kind: str, threshold: float) -> None:
    """Leave exactly one rule enabled so a test asserts on one evaluator."""
    with get_session() as session:
        session.query(AlertRule).delete()
        session.add(AlertRule(
            name=kind, kind=kind, threshold=threshold, enabled=True, notify=False
        ))


def _site_days(start: date, days: int, clicks: int, impressions=1000, position=10.0):
    with get_session() as session:
        for n in range(days):
            session.add(GscSiteDaily(
                date=start + timedelta(days=n), clicks=clicks,
                impressions=impressions,
                ctr=clicks / impressions if impressions else 0.0,
                position=position,
            ))


# ── Deduplication ──────────────────────────────────────────────────


def test_an_unchanged_condition_stays_one_alert(dashboard_db):
    """Three evaluations of the same unchanged problem must be one row with a
    rising seen-count, not three rows."""
    _only_rule("spend_without_conversions", 50)
    with get_session() as session:
        session.add(AdsCampaignDaily(
            date=SETTLED, campaign="phone camp", spend=254.0, conversions=0.0
        ))
    for _ in range(3):
        alerts.evaluate(today=TODAY, notify=False)
    with get_session() as session:
        rows = session.query(Alert).all()
    assert len(rows) == 1
    assert rows[0].times_seen == 3


def test_acknowledging_clears_it_from_the_inbox(dashboard_db):
    _only_rule("spend_without_conversions", 50)
    with get_session() as session:
        session.add(AdsCampaignDaily(
            date=SETTLED, campaign="phone camp", spend=254.0, conversions=0.0
        ))
    alerts.evaluate(today=TODAY, notify=False)
    alert_id = alerts.open_alerts()[0].id
    alerts.acknowledge(alert_id)
    assert alerts.open_alerts() == []
    assert alerts.open_count() == 0


def test_an_acknowledged_condition_that_persists_stays_hidden(dashboard_db):
    """"Acknowledged" has to mean "until it changes". Reopening every night
    while the condition simply continues makes the button useless, and the
    inbox gets ignored."""
    _only_rule("spend_without_conversions", 50)
    with get_session() as session:
        session.add(AdsCampaignDaily(
            date=SETTLED, campaign="phone camp", spend=254.0, conversions=0.0
        ))
    alerts.evaluate(today=TODAY, notify=False)
    alerts.acknowledge(alerts.open_alerts()[0].id)
    for _ in range(3):
        alerts.evaluate(today=TODAY, notify=False)
    assert alerts.open_count() == 0


def test_a_condition_that_goes_away_and_returns_reopens(dashboard_db):
    """The other half: a problem dealt with in July that comes back in August
    must not stay invisible because it was acknowledged once."""
    _only_rule("spend_without_conversions", 50)
    with get_session() as session:
        session.add(AdsCampaignDaily(
            date=SETTLED, campaign="phone camp", spend=254.0, conversions=0.0
        ))
    alerts.evaluate(today=TODAY, notify=False)
    alerts.acknowledge(alerts.open_alerts()[0].id)

    # Fixed: the campaign starts converting.
    with get_session() as session:
        session.query(AdsCampaignDaily).update({"conversions": 5.0})
    summary = alerts.evaluate(today=TODAY, notify=False)
    assert summary["resolved"] == 1

    # And it regresses.
    with get_session() as session:
        session.query(AdsCampaignDaily).update({"conversions": 0.0})
    summary = alerts.evaluate(today=TODAY, notify=False)
    assert summary["reopened"] == 1
    assert alerts.open_count() == 1


def test_a_problem_that_fixes_itself_clears_the_inbox(dashboard_db):
    """Most of them do. An inbox that only ever grows is one nobody opens."""
    _only_rule("spend_without_conversions", 50)
    with get_session() as session:
        session.add(AdsCampaignDaily(
            date=SETTLED, campaign="phone camp", spend=254.0, conversions=0.0
        ))
    alerts.evaluate(today=TODAY, notify=False)
    assert alerts.open_count() == 1
    with get_session() as session:
        session.query(AdsCampaignDaily).update({"conversions": 5.0})
    alerts.evaluate(today=TODAY, notify=False)
    assert alerts.open_count() == 0


def test_one_broken_rule_does_not_stop_the_others(dashboard_db, monkeypatch):
    with get_session() as session:
        session.query(AlertRule).delete()
        session.add(AlertRule(name="bad", kind="clicks_drop", threshold=10))
        session.add(AlertRule(
            name="good", kind="spend_without_conversions", threshold=50
        ))
        session.add(AdsCampaignDaily(
            date=SETTLED, campaign="phone camp", spend=254.0, conversions=0.0
        ))

    def explode(threshold, today):
        raise RuntimeError("rule is broken")

    monkeypatch.setitem(
        alerts._BY_KIND, "clicks_drop",
        alerts.RuleKind(key="clicks_drop", label="x", help="x", unit="%",
                        default_threshold=1, evaluate=explode),
    )
    summary = alerts.evaluate(today=TODAY, notify=False)
    assert summary["findings"] == 1  # the healthy rule still ran


# ── Individual evaluators ──────────────────────────────────────────


def test_clicks_drop_fires_only_past_the_threshold(dashboard_db):
    _only_rule("clicks_drop", 25)
    # Previous week 100/day, current week 50/day = a 50% fall.
    _site_days(SETTLED - timedelta(days=13), 7, clicks=100)
    _site_days(SETTLED - timedelta(days=6), 7, clicks=50)
    summary = alerts.evaluate(today=TODAY, notify=False)
    assert summary["findings"] == 1
    assert "50%" in alerts.open_alerts()[0].title


def test_clicks_drop_is_silent_without_a_baseline(dashboard_db):
    """A fall from nothing isn't a fall — it's a site that had no data."""
    _only_rule("clicks_drop", 25)
    _site_days(SETTLED - timedelta(days=6), 7, clicks=0, impressions=0)
    assert alerts.evaluate(today=TODAY, notify=False)["findings"] == 0


def test_position_drop_measures_the_number_going_up(dashboard_db):
    """Position is inverted: 4 → 9 is worse, and that's what must fire."""
    _only_rule("position_drop", 2.0)
    _site_days(SETTLED - timedelta(days=55), 28, clicks=10, position=4.0)
    _site_days(SETTLED - timedelta(days=27), 28, clicks=10, position=9.0)
    summary = alerts.evaluate(today=TODAY, notify=False)
    assert summary["findings"] == 1
    assert "slipped 5.0" in alerts.open_alerts()[0].title


def test_position_improving_does_not_fire(dashboard_db):
    _only_rule("position_drop", 2.0)
    _site_days(SETTLED - timedelta(days=55), 28, clicks=10, position=9.0)
    _site_days(SETTLED - timedelta(days=27), 28, clicks=10, position=4.0)
    assert alerts.evaluate(today=TODAY, notify=False)["findings"] == 0


def test_conversions_drop_stays_silent_while_the_baseline_predates_tracking(
    dashboard_db,
):
    """The GTM tags went live partway through July 2026. Firing on a window
    that predates them would report the install as a collapse."""
    _only_rule("conversions_drop", 40)
    # Events only exist in the current window.
    with get_session() as session:
        for n in range(7):
            session.add(Ga4EventDaily(
                date=SETTLED - timedelta(days=n), event_name="call_click",
                event_count=1,
            ))
    assert alerts.evaluate(today=TODAY, notify=False)["findings"] == 0


def test_cost_per_conversion_fires_on_the_expensive_campaign_only(dashboard_db):
    _only_rule("cost_per_conversion", 50)
    with get_session() as session:
        session.add(AdsCampaignDaily(
            date=SETTLED, campaign="phone camp", spend=254.16, conversions=4.0
        ))
        session.add(AdsCampaignDaily(
            date=SETTLED, campaign="Store Goal PMax", spend=143.82, conversions=46.0
        ))
    summary = alerts.evaluate(today=TODAY, notify=False)
    assert summary["findings"] == 1
    assert "phone camp" in alerts.open_alerts()[0].title


def test_a_zero_conversion_campaign_is_not_double_reported(dashboard_db):
    """It belongs to spend_without_conversions; cost_per_conversion must skip
    it rather than divide by zero or file a second alert about the same spend."""
    with get_session() as session:
        session.query(AlertRule).delete()
        session.add(AlertRule(
            name="a", kind="spend_without_conversions", threshold=50
        ))
        session.add(AlertRule(name="b", kind="cost_per_conversion", threshold=50))
        session.add(AdsCampaignDaily(
            date=SETTLED, campaign="phone camp", spend=254.0, conversions=0.0
        ))
    summary = alerts.evaluate(today=TODAY, notify=False)
    assert summary["findings"] == 1


def test_a_failed_job_raises_an_alert(dashboard_db):
    """Stale numbers with no error on screen is the failure this dashboard
    exists to prevent."""
    _only_rule("job_failure", 0)
    with get_session() as session:
        session.add(JobRun(
            job="gsc_daily", status=JobStatus.error.value,
            started_at=datetime.now(timezone.utc), error="403 from Search Console",
        ))
    summary = alerts.evaluate(today=TODAY, notify=False)
    assert summary["findings"] == 1
    assert "403" in alerts.open_alerts()[0].body


def test_a_job_that_recovered_does_not_alert(dashboard_db):
    _only_rule("job_failure", 0)
    now = datetime.now(timezone.utc)
    with get_session() as session:
        session.add(JobRun(job="gsc_daily", status=JobStatus.error.value,
                           started_at=now - timedelta(hours=2), error="boom"))
        session.add(JobRun(job="gsc_daily", status=JobStatus.ok.value,
                           started_at=now))
    assert alerts.evaluate(today=TODAY, notify=False)["findings"] == 0


def test_default_rules_are_seeded_once_and_deletions_stick(dashboard_db):
    """Re-creating a rule the owner deleted every night would be its own bug."""
    assert alerts.ensure_default_rules() == len(alerts.KINDS)
    assert alerts.ensure_default_rules() == 0
    with get_session() as session:
        session.query(AlertRule).filter(AlertRule.kind == "clicks_drop").delete()
    alerts.ensure_default_rules()
    with get_session() as session:
        kinds = {k for (k,) in session.query(AlertRule.kind).all()}
    assert "clicks_drop" not in kinds


# ── Blog decay ─────────────────────────────────────────────────────


def _article(pid: int, url: str, title: str = "Post"):
    return BlogArticle(pipeline_id=pid, title=title, shopify_url=url)


def _page(day: date, url: str, impressions: int, clicks: int = 1):
    return GscPageDaily(
        date=day, page=url, clicks=clicks, impressions=impressions,
        ctr=0.01, position=10.0,
    )


def test_decay_ranks_by_absolute_impressions_not_percent(dashboard_db):
    """Percentage flatters trivia: 25→1 is a 96% collapse worth 24
    impressions; 18,272→5,497 is 'only' −70% and worth 12,775. The second is
    the article to rewrite, and it must sort first."""
    big = "https://drflooring.ca/blogs/news/big"
    tiny = "https://drflooring.ca/blogs/news/tiny"
    with get_session() as session:
        session.add(_article(1, big, "Big"))
        session.add(_article(2, tiny, "Tiny"))
        session.add(_page(SETTLED - timedelta(days=7), big, 18272))
        session.add(_page(SETTLED, big, 5497))
        session.add(_page(SETTLED - timedelta(days=7), tiny, 25))
        session.add(_page(SETTLED, tiny, 1))

    data = reporting.blog_posts(window_days=7, order="decay", today=TODAY)
    assert [r["article"].title for r in data["rows"]] == ["Big", "Tiny"]
    assert data["rows"][0]["impressions_lost"] == 12775


def test_a_post_that_never_had_impressions_is_not_called_decaying(dashboard_db):
    """It's new, or it's invisible. Those want different responses from a
    rewrite, so lumping them in would send the refresh pass after the wrong
    articles."""
    url = "https://drflooring.ca/blogs/news/new"
    with get_session() as session:
        session.add(_article(1, url, "Brand new"))
    data = reporting.blog_posts(window_days=7, today=TODAY)
    assert data["rows"][0]["decaying"] is False
    assert data["decaying"] == 0


def test_a_small_fall_below_the_baseline_floor_is_not_decay(dashboard_db):
    url = "https://drflooring.ca/blogs/news/quiet"
    with get_session() as session:
        session.add(_article(1, url, "Quiet"))
        session.add(_page(SETTLED - timedelta(days=7), url, 20))
        session.add(_page(SETTLED, url, 2))
    data = reporting.blog_posts(window_days=7, today=TODAY)
    # Lost impressions, but never had 50 to fall from.
    assert data["rows"][0]["impressions_lost"] == 18
    assert data["rows"][0]["decaying"] is False


def test_blog_rows_join_search_data_despite_url_variation(dashboard_db):
    with get_session() as session:
        session.add(_article(1, "https://drflooring.ca/blogs/news/x"))
        session.add(_page(SETTLED, "https://www.drflooring.ca/blogs/news/x/", 500, 9))
    data = reporting.blog_posts(window_days=7, today=TODAY)
    assert data["rows"][0]["current"].clicks == 9
