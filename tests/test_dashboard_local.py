"""Local search: the city data, and the two traps in reading it.

Both traps produce a number that looks fine and is wrong, which is why they
are pinned here rather than left to the template:

  * **Langley is two cities in GA4.** Google reports the City of Langley and
    Langley Township separately. A home-market figure that counts one
    understates the actual home market by roughly a third.
  * **"Not in the results" is not a bad position.** Storing a placeholder
    rank for an absent listing would average into a position the site never
    held.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from dashboard.db import get_session
from dashboard.models import Ga4CityDaily, LocalSerpRank

TODAY = date(2026, 8, 11)


def _city(city, sessions, conversions=0, region="British Columbia", day=None):
    return Ga4CityDaily(
        date=day or (TODAY - timedelta(days=1)), city=city, region=region,
        sessions=sessions, users=sessions, engaged_sessions=sessions,
        conversions=conversions,
    )


def _rank(keyword, city, position, pack=None, tops=None, packs=None):
    return LocalSerpRank(
        date=TODAY, keyword=keyword, city=city, position=position,
        pack_position=pack,
        top_domains_json=json.dumps(tops or []),
        pack_names_json=json.dumps(packs or []),
        results_seen=20,
    )


# ── The two-Langleys trap ───────────────────────────────────────────


def test_both_langleys_count_as_the_home_market(dashboard_db):
    """GA4 splits the City of Langley from Langley Township. Counting one
    understates the home market by about a third — measured on the real
    property, 156 sessions vs 57 for the same period."""
    from dashboard import reporting

    with get_session() as s:
        s.add(_city("Langley Township", 156))
        s.add(_city("Langley", 57))
        s.add(_city("Vancouver", 787))

    data = reporting.local_seo(today=TODAY)

    assert data["home_sessions"] == 213
    assert data["home_share"] == 213 / 1000 * 100
    homes = {c["city"] for c in data["cities"] if c["home"]}
    assert homes == {"Langley", "Langley Township"}


def test_a_city_that_is_not_home_is_not_counted_as_home(dashboard_db):
    from dashboard import reporting

    with get_session() as s:
        s.add(_city("Surrey", 300))

    data = reporting.local_seo(today=TODAY)

    assert data["home_sessions"] == 0
    assert all(not c["home"] for c in data["cities"])


# ── Traffic that doesn't convert ────────────────────────────────────


def test_sessions_per_conversion_is_none_rather_than_infinite(dashboard_db):
    """A city with traffic and no calls is the finding this page exists for.
    Dividing by zero, or showing 0%, would both bury it."""
    from dashboard import reporting

    with get_session() as s:
        s.add(_city("Langley", 300, conversions=0))
        s.add(_city("Vancouver", 200, conversions=4))

    data = reporting.local_seo(today=TODAY)
    by_city = {c["city"]: c for c in data["cities"]}

    assert by_city["Langley"]["per_conversion"] is None
    assert by_city["Vancouver"]["per_conversion"] == 50


# ── Rank tracking ───────────────────────────────────────────────────


def test_absence_from_the_results_is_null_not_a_big_number(dashboard_db):
    """Storing 100 for "not found" would average into a position the site
    never held, and the average is the number anyone would quote."""
    from dashboard import reporting

    with get_session() as s:
        s.add(_city("Langley", 10))
        s.add(_rank("flooring langley", "Langley", None))

    data = reporting.local_seo(today=TODAY)
    row = data["ranks_by_keyword"]["flooring langley"][0]

    assert row.position is None


def test_absence_from_the_local_pack_is_counted(dashboard_db):
    """Measured live: 2nd organically in Langley and absent from the pack in
    every city. Those point at completely different work, so the page has to
    be able to say the second one out loud."""
    from dashboard import reporting

    with get_session() as s:
        s.add(_city("Langley", 10))
        s.add(_rank("flooring langley", "Langley", 2, pack=None,
                    packs=["Ludo's Floors", "Nufloors Langley"]))
        s.add(_rank("flooring langley", "Surrey", 2, pack=None,
                    packs=["Nufloors Langley"]))

    data = reporting.local_seo(today=TODAY)

    assert data["pack_measured"] == 2
    assert data["pack_absent_count"] == 2
    # Who holds it, counted across every measurement.
    holders = dict(data["pack_holders"])
    assert holders["Nufloors Langley"] == 2


def test_our_own_domain_is_never_listed_as_a_rival(dashboard_db):
    from dashboard import reporting

    with get_session() as s:
        s.add(_city("Langley", 10))
        s.add(_rank("flooring langley", "Langley", 2,
                    tops=["nufloors.ca", "drflooring.ca", "westpro.com"]))

    data = reporting.local_seo(today=TODAY)
    rivals = dict(data["top_rivals"])

    assert "drflooring.ca" not in rivals
    assert rivals["nufloors.ca"] == 1


def test_only_the_most_recent_measurement_day_is_shown(dashboard_db):
    """Rank tracking runs repeatedly, and mixing days would show the same
    keyword twice with different numbers and no way to tell which is now."""
    from dashboard import reporting

    with get_session() as s:
        s.add(_city("Langley", 10))
        old = _rank("flooring langley", "Langley", 9)
        old.date = TODAY - timedelta(days=7)
        s.add(old)
        s.add(_rank("flooring langley", "Langley", 2))

    data = reporting.local_seo(today=TODAY)

    assert data["rank_day"] == TODAY
    rows = data["ranks_by_keyword"]["flooring langley"]
    assert len(rows) == 1 and rows[0].position == 2


# ── Local-intent query filtering ────────────────────────────────────


def test_only_place_naming_queries_are_treated_as_local(dashboard_db):
    """A national term measured nationally tells you nothing about Langley,
    and padding the local list with them makes the page look healthier than
    the market position is."""
    from dashboard.models import GscQueryDaily
    from dashboard import reporting

    day = date(2026, 8, 5)
    with get_session() as s:
        s.add(_city("Langley", 10))
        for query, impressions in [
            ("flooring langley", 400),
            ("flooring store near me", 120),
            ("laminate flooring", 9000),      # national — must not appear
            ("best vinyl plank", 5000),        # national — must not appear
        ]:
            s.add(GscQueryDaily(date=day, query=query, clicks=1,
                                impressions=impressions, ctr=0.01, position=10.0))

    data = reporting.local_seo(today=TODAY)
    found = {q["query"] for q in data["local_queries"]}

    assert found == {"flooring langley", "flooring store near me"}


def test_no_city_data_says_so_instead_of_rendering_zeroes(dashboard_db):
    """An empty Local SEO page full of confident 0.0% would read as "we have
    no Langley traffic" rather than "this has never been synced"."""
    from dashboard import reporting

    data = reporting.local_seo(today=TODAY)

    assert data["has_city_data"] is False
    assert data["has_ranks"] is False
