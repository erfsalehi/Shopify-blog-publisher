"""The advisor and the keyword layer.

The advisor tests are weighted toward the grounding check and the memory,
because those are the two claims the feature makes that a plain "call an LLM"
would not. Everything else about it is a prompt.

The DataForSEO tests are weighted toward *not spending money*: the budget cap,
and never writing a ledger entry for a call that was rejected.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from dashboard import advisor, reporting, store
from dashboard.db import get_session
from dashboard.jobs.dataforseo_keywords import (
    COST_PER_REQUEST,
    budget_remaining,
    sync_dataforseo_keywords,
)
from dashboard.models import (
    AdvisorAction,
    AdvisorNote,
    ApiSpend,
    GscQueryDaily,
    KeywordMetric,
)

TODAY = date(2026, 8, 8)
SETTLED = TODAY - timedelta(days=3)

BRIEF = """ORGANIC SEARCH
- Clicks: 1,529 (was 1,467)
- Impressions: 90,677
- CTR: 1.69%
term | clicks | impressions | position
toucan flooring | 2 | 1029 | 7.0
"""


# ── The grounding check ────────────────────────────────────────────


def test_a_fabricated_absolute_is_flagged(dashboard_db):
    """The failure this whole module is designed against: a plausible number
    that came from nowhere, wearing the credibility of the real ones."""
    output = "Traffic is strong — you had 8,412 clicks from 44,000 impressions."
    flagged = advisor.unverified_figures(output, BRIEF)
    assert "8,412" in flagged
    assert "44,000" in flagged


def test_figures_present_in_the_brief_are_not_flagged(dashboard_db):
    output = "You had 1,529 clicks against 90,677 impressions at 1.69% CTR."
    assert advisor.unverified_figures(output, BRIEF) == []


def test_comma_formatting_does_not_cause_a_false_flag(dashboard_db):
    """The brief writes 1029, the model writes 1,029. Same number."""
    assert advisor.unverified_figures("1,029 impressions", BRIEF) == []


def test_small_counts_are_not_flagged(dashboard_db):
    """The model legitimately counts things — "3 pages", "top 5". Flagging
    those buries the real catches in noise."""
    output = "Rewrite the top 3 titles; 2 of them are underperforming."
    assert advisor.unverified_figures(output, BRIEF) == []


def test_a_dollar_figure_from_nowhere_is_flagged(dashboard_db):
    """Money is worth flagging even when small — a $12 claim in a brief with
    no dollar figures at all is invented."""
    assert "$12" in advisor.unverified_figures("It costs $12 per click.", BRIEF)


# ── Memory ─────────────────────────────────────────────────────────


def _action(scope: str, text: str, status: str = "open", days_ago: int = 3):
    with get_session() as session:
        session.add(AdvisorAction(
            scope=scope, text=text, status=status,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        ))


def test_memory_reports_what_was_done_and_dismissed(dashboard_db):
    """Feeding old notes back only says what the model once said. What makes
    memory useful is knowing which advice was acted on."""
    _action("overview", "Rewrite the underlayment title", status="done")
    _action("overview", "Start a podcast", status="dismissed")
    memory = advisor._memory("overview")
    assert "DONE" in memory and "Rewrite the underlayment title" in memory
    assert "DISMISSED" in memory and "Start a podcast" in memory


def test_memory_is_explicit_when_there_is_none(dashboard_db):
    """An empty section invites the model to interpret silence."""
    assert "first note" in advisor._memory("overview")


def test_a_new_note_supersedes_open_actions_from_the_old_one(
    dashboard_db, monkeypatch
):
    """A checklist that only grows is one nobody reads."""
    monkeypatch.setattr(
        advisor, "_call",
        lambda *a, **k: (
            '{"summary": "s", "reading": "r", "actions": ["new thing"]}',
            {"input": 10, "output": 5}, "fake-model",
        ),
    )
    monkeypatch.setattr(
        "dashboard.advisor_context.build_context", lambda scope, today=None: BRIEF
    )
    _action("overview", "old thing", status="open")
    advisor.generate("overview")
    with get_session() as session:
        rows = {a.text: a.status for a in session.query(AdvisorAction).all()}
    assert rows["old thing"] == "dismissed"
    assert rows["new thing"] == "open"


def test_resolving_an_action_removes_it_from_the_open_list(dashboard_db):
    _action("blog", "do the thing")
    action_id = advisor.actions_for("blog")[0].id
    advisor.resolve(action_id, "done")
    assert advisor.actions_for("blog") == []
    assert advisor.open_action_count("blog") == 0


# ── Generation ─────────────────────────────────────────────────────


def test_a_failed_generation_is_recorded_not_raised(dashboard_db, monkeypatch):
    """The tab and the jobs page both need to show that it tried and why it
    couldn't — a raise would leave no trace anywhere."""
    def boom(*a, **k):
        raise RuntimeError("429 quota exceeded")

    monkeypatch.setattr(advisor, "_call", boom)
    monkeypatch.setattr(
        "dashboard.advisor_context.build_context", lambda scope, today=None: BRIEF
    )
    note = advisor.generate("ads")
    assert note.error is not None
    assert "429" in note.error
    assert advisor.latest_note("ads").error is not None


def test_the_brief_is_stored_with_the_note(dashboard_db, monkeypatch):
    """A suggestion you can't trace back to the numbers behind it is an
    assertion."""
    monkeypatch.setattr(
        advisor, "_call",
        lambda *a, **k: ('{"summary":"s","reading":"r","actions":[]}',
                         {"input": 1, "output": 1}, "fake-model"),
    )
    monkeypatch.setattr(
        "dashboard.advisor_context.build_context", lambda scope, today=None: BRIEF
    )
    note = advisor.generate("overview")
    assert note.context_md == BRIEF


def test_a_fenced_json_reply_is_parsed(dashboard_db):
    parsed = advisor._clean_json('```json\n{"summary": "ok"}\n```')
    assert parsed["summary"] == "ok"


def test_the_model_that_actually_answered_is_recorded(dashboard_db, monkeypatch):
    """Both Pro models are limit:0 on this key, so a configured model can fall
    through to another. The note has to say which one replied."""
    monkeypatch.setattr(
        advisor, "_call",
        lambda *a, **k: ('{"summary":"s","reading":"r","actions":[]}',
                         {"input": 1, "output": 1}, "gemini-3-flash-preview"),
    )
    monkeypatch.setattr(
        "dashboard.advisor_context.build_context", lambda scope, today=None: BRIEF
    )
    store.set(store.ADVISOR_MODEL, "gemini-3-pro-preview")
    note = advisor.generate("overview")
    assert note.model == "gemini-3-flash-preview"


def test_an_unknown_scope_is_rejected(dashboard_db):
    with pytest.raises(ValueError, match="unknown advisor scope"):
        advisor.generate("nonsense")


# ── Keywords ───────────────────────────────────────────────────────


def _query(term: str, clicks: int, impressions: int, position: float):
    with get_session() as session:
        session.add(GscQueryDaily(
            date=SETTLED, query=term, clicks=clicks, impressions=impressions,
            ctr=clicks / impressions if impressions else 0.0, position=position,
        ))


def test_striking_distance_excludes_page_one_and_the_unreachable(dashboard_db):
    """A term ranked 2nd has little headroom; one ranked 80th isn't a near
    miss. The middle is where one better page changes the outcome."""
    _query("already winning", 50, 500, 2.0)
    _query("striking", 1, 400, 12.0)
    _query("hopeless", 0, 400, 78.0)
    data = reporting.keywords(window_days=7, striking_only=True, today=TODAY)
    assert [r["query"] for r in data["rows"]] == ["striking"]


def test_terms_without_market_data_still_rank(dashboard_db):
    """Opportunity is computed from the site's own impressions, not bought
    volume — otherwise the ranking would only reflect which terms happened to
    have been paid for."""
    _query("no volume yet", 1, 900, 9.0)
    data = reporting.keywords(window_days=7, today=TODAY)
    assert data["rows"][0]["volume"] is None
    assert data["rows"][0]["opportunity"] > 0


def test_market_data_joins_onto_the_term(dashboard_db):
    _query("vinyl plank flooring", 2, 500, 11.0)
    with get_session() as session:
        session.add(KeywordMetric(
            keyword="vinyl plank flooring", location_code=2124,
            search_volume=1300, cpc=2.45, competition=0.6,
        ))
    row = reporting.keywords(window_days=7, today=TODAY)["rows"][0]
    assert row["volume"] == 1300
    assert row["cpc"] == 2.45


def test_opportunity_favours_impressions_being_wasted(dashboard_db):
    """Two terms, same position. The one being shown far more with no clicks
    has more to recover."""
    _query("big waste", 0, 2000, 10.0)
    _query("small waste", 0, 100, 10.0)
    data = reporting.keywords(window_days=7, today=TODAY)
    assert data["rows"][0]["query"] == "big waste"


# ── Not spending money ─────────────────────────────────────────────


class FakeDFS:
    def __init__(self, rows=None, error=None):
        self.enabled = True
        self.last_error = error
        self._rows = rows or []
        self.calls = 0

    def keyword_data(self, keywords, location_code=2124, language_code="en"):
        self.calls += 1
        self.seen = keywords
        return self._rows


def test_the_budget_cap_blocks_the_call_entirely(dashboard_db):
    """A scheduling mistake must not be able to drain a prepaid balance."""
    store.set(store.DFS_BUDGET_USD, 0.05)  # below one request
    client = FakeDFS(rows=[{"keyword": "x", "search_volume": 10}])
    result = sync_dataforseo_keywords(client=client, today=TODAY)
    assert result.skipped is True
    assert "Spend cap" in result.skip_reason
    assert client.calls == 0


def test_a_rejected_call_records_no_spend(dashboard_db):
    """DataForSEO charges nothing for a rejected request, and inventing a
    charge would be worse than under-counting."""
    _query("something", 1, 300, 12.0)
    client = FakeDFS(rows=[], error="40104: Please verify your account")
    result = sync_dataforseo_keywords(client=client, today=TODAY)
    assert result.skipped is True
    assert "verify your account" in result.skip_reason
    with get_session() as session:
        assert session.query(ApiSpend).count() == 0


def test_a_successful_call_records_exactly_one_charge(dashboard_db):
    _query("vinyl", 1, 300, 12.0)
    client = FakeDFS(rows=[
        {"keyword": "vinyl", "search_volume": 900, "cpc": 1.2, "competition": 0.4}
    ])
    before = budget_remaining()
    sync_dataforseo_keywords(client=client, today=TODAY)
    with get_session() as session:
        spends = session.query(ApiSpend).all()
    assert len(spends) == 1
    assert spends[0].cost_usd == COST_PER_REQUEST
    assert budget_remaining() == pytest.approx(before - COST_PER_REQUEST)


def test_every_candidate_goes_in_one_request(dashboard_db):
    """Billing is per request, not per keyword — batching is the entire cost
    strategy. One call per term would be 300x the price."""
    for n in range(40):
        _query(f"term {n}", 1, 100 + n, 12.0)
    client = FakeDFS(rows=[])
    sync_dataforseo_keywords(client=client, today=TODAY)
    assert client.calls == 1
    assert len(client.seen) == 40


def test_terms_already_looked_up_are_not_bought_again(dashboard_db):
    """Volume is a 12-month rolling average. Re-asking weekly spends money to
    watch a number that barely moves."""
    _query("known", 1, 300, 12.0)
    _query("unknown", 1, 300, 12.0)
    with get_session() as session:
        session.add(KeywordMetric(
            keyword="known", location_code=2124, search_volume=100,
            fetched_at=datetime.now(timezone.utc),
        ))
    client = FakeDFS(rows=[])
    sync_dataforseo_keywords(client=client, today=TODAY)
    assert client.seen == ["unknown"]


# ── Keyword text validity ─────────────────────────────────────────


def test_a_question_mark_keyword_is_filtered_out(dashboard_db):
    """Confirmed live against DataForSEO: a keyword containing '?' fails with
    40501 and takes the ENTIRE batched task down with it — not just itself.
    Must never reach the request."""
    from dashboard.jobs.dataforseo_keywords import is_valid_keyword

    assert is_valid_keyword("what are eco-friendly flooring options?") is False
    assert is_valid_keyword("vinyl plank flooring langley") is True


def test_overlong_or_wordy_keywords_are_filtered(dashboard_db):
    from dashboard.jobs.dataforseo_keywords import is_valid_keyword

    assert is_valid_keyword("x" * 81) is False
    assert is_valid_keyword(" ".join(["word"] * 11)) is False


def test_an_invalid_keyword_never_reaches_the_candidate_list(dashboard_db):
    """The filter has to run before batching, not after — the whole point is
    that DataForSEO fails the entire task on one bad keyword."""
    _query("floor underlayment", 5, 300, 12.0)
    _query("what are eco-friendly flooring options?", 5, 300, 12.0)
    from dashboard.jobs.dataforseo_keywords import _candidate_keywords

    out = _candidate_keywords(50, today=TODAY)
    assert "floor underlayment" in out
    assert all("?" not in k for k in out)


def test_a_task_level_failure_is_surfaced_not_silently_zeroed(dashboard_db):
    """The envelope can report 20000 'Ok' while the task inside it failed —
    confirmed live. A caller checking only the envelope sees an empty result
    with no explanation, which is worse than an error."""
    from blog_pipeline.tools.dataforseo import DataForSEOClient

    class FakeResponse:
        status_code = 200
        content = b"{}"

        def json(self):
            return {
                "status_code": 20000, "status_message": "Ok.",
                "tasks": [{
                    "status_code": 40501,
                    "status_message": "Invalid Field: 'keywords'.",
                    "result": None,
                }],
            }

        def raise_for_status(self):
            pass

    client = DataForSEOClient(login="x", password="y")
    import blog_pipeline.tools.dataforseo as dfs_module
    real_post = dfs_module.httpx.post
    dfs_module.httpx.post = lambda *a, **k: FakeResponse()
    try:
        rows = client.keyword_data(["bad"])
    finally:
        dfs_module.httpx.post = real_post
    assert rows == []
    assert client.last_error is not None
    assert "40501" in client.last_error


def test_a_surfaced_task_failure_reaches_the_job_as_skipped(
    dashboard_db, monkeypatch
):
    """Before this fix the job reported status='ok' with rows=0 and no
    explanation — indistinguishable from 'nothing needed enriching'."""
    _query("floor underlayment", 5, 300, 12.0)

    class FailingClient:
        enabled = True
        last_error = None

        def keyword_data(self, keywords, location_code=2124, language_code="en"):
            self.last_error = "40501: Invalid Field: 'keywords'."
            return []

    result = sync_dataforseo_keywords(client=FailingClient(), today=TODAY)
    assert result.skipped is True
    assert "40501" in result.skip_reason
