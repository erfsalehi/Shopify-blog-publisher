"""A list of collections, worked through one at a time and unattended.

The queue exists because importing was attended: paste a URL, watch it
finish, paste the next. A supplier with thirty ranges is thirty of those.

What the tests here are really pinning down is the *one at a time* rule.
Two runs going together compete for the same bounded passes, so both crawl,
and the second range's collection and cross-linking stages interleave with
the first's — so "one per day" has to mean "one per day, and only when the
one in front has finished", which is a different promise and an honest one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dashboard import import_queue, product_import

# The fake manufacturer site and the fake Shopify already exist, built for
# the importer's own tests. Imported rather than rebuilt: a second fake
# store would be a second thing to keep in step with the real one, and the
# queue's job is to start runs against exactly the store those model.
from tests.test_product_import import (  # noqa: F401
    fake_shopify,
    fake_site,
    no_llm,
)
from dashboard.db import get_session
from dashboard.jobs import get_job
from dashboard.models import (
    ImportQueueEntry,
    ImportQueueStatus,
    ImportRun,
    ImportStage,
)

TWO = (
    "https://maker.test/collections/advantage, Ames Tile & Stone\n"
    "https://maker.test/collections/anthology, Ames Tile & Stone, Anthology\n"
)


@pytest.fixture
def client(dashboard_db):
    from dashboard.web import create_app

    with TestClient(create_app()) as c:
        yield c


# ── Reading a pasted list ────────────────────────────────────────────


def test_a_pasted_list_becomes_a_queue(dashboard_db):
    added, problems = import_queue.add(TWO)
    assert (added, problems) == (2, [])

    queue = import_queue.entries()
    assert [e["source_url"] for e in queue] == [
        "https://maker.test/collections/advantage",
        "https://maker.test/collections/anthology",
    ]
    assert queue[1]["collection_title"] == "Anthology"
    assert all(e["vendor"] == "Ames Tile & Stone" for e in queue)
    assert all(e["status"] == ImportQueueStatus.queued.value for e in queue)


def test_the_readable_lines_are_added_and_the_rest_reported(dashboard_db):
    """Losing twenty-nine good lines to a typo in the thirtieth is what stops
    people pasting lists."""
    added, problems = import_queue.add(
        "https://maker.test/collections/advantage, Ames Tile\n"
        "not-a-url, Ames Tile\n"
        "https://maker.test/collections/anthology\n"          # no brand
        "\n"
        "# a comment, ignored\n"
    )
    assert added == 1
    assert len(problems) == 2
    assert any("not a URL" in p for p in problems)
    assert any("no brand" in p for p in problems)


def test_a_line_without_a_brand_is_refused_rather_than_guessed(dashboard_db):
    """The brand is the first word of every product name and most suppliers
    publish it nowhere a scraper can read. A queue entry without one would
    import a whole range unnamed, hours after anyone could have noticed."""
    added, problems = import_queue.add("https://maker.test/collections/advantage")
    assert added == 0
    assert "no brand" in problems[0]


def test_tabs_and_pipes_read_the_same_as_commas(dashboard_db):
    """Someone pasting from a spreadsheet gets tabs, and a brand can contain
    a comma."""
    added, _ = import_queue.add(
        "https://maker.test/collections/a\tAmes Tile, Inc.\n"
        "https://maker.test/collections/b | Ames Tile\n"
    )
    assert added == 2
    assert import_queue.entries()[0]["vendor"] == "Ames Tile, Inc."


def test_a_list_pasted_twice_says_so(dashboard_db):
    """Silently skipping it would look like it worked."""
    import_queue.add(TWO)
    added, problems = import_queue.add(TWO)
    assert added == 0
    assert len(problems) == 2
    assert all("already in the queue" in p for p in problems)


def test_an_entry_can_be_taken_off_before_it_starts(dashboard_db):
    import_queue.add(TWO)
    first = import_queue.entries()[0]
    assert import_queue.remove(first["id"]) is True
    assert import_queue.waiting() == 1
    # Nothing is deleted — what was queued and dropped is still readable.
    assert any(
        e["status"] == ImportQueueStatus.cancelled.value
        for e in import_queue.entries()
    )


# ── One at a time ────────────────────────────────────────────────────


def test_the_next_collection_starts_when_nothing_is_importing(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    import_queue.add(TWO)
    started = import_queue.start_next()

    assert started is not None
    assert started["source_url"] == "https://maker.test/collections/advantage"
    with get_session() as session:
        run = session.get(ImportRun, started["run"])
    assert run.vendor == "Ames Tile & Stone"
    assert run.is_active
    assert import_queue.waiting() == 1


def test_nothing_starts_while_an_import_is_still_running(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    """The rule the whole queue turns on. Two runs at once would each get
    half the passes and take twice as long, and the second range's
    collection stage would interleave with the first's."""
    import_queue.add(TWO)
    import_queue.start_next()

    assert import_queue.start_next() is None
    with get_session() as session:
        assert session.query(ImportRun).count() == 1


def test_the_one_after_it_starts_once_the_first_has_finished(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    import_queue.add(TWO)
    first = import_queue.start_next()
    for _ in range(20):
        if product_import.advance(first["run"]).done:
            break

    second = import_queue.start_next()
    assert second is not None
    assert second["source_url"] == "https://maker.test/collections/anthology"

    queue = import_queue.entries()
    by_url = {e["source_url"]: e for e in queue}
    assert by_url[first["source_url"]]["status"] == ImportQueueStatus.done.value
    assert by_url[second["source_url"]]["status"] == ImportQueueStatus.running.value


def test_a_run_that_failed_marks_its_entry_failed_with_the_reason(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    """And is not retried. A supplier page that broke once usually breaks the
    same way twice, and a queue that re-runs it never advances past it."""
    import_queue.add("https://maker.test/collections/missing, Ames Tile\n")
    started = import_queue.start_next()
    product_import.advance(started["run"])          # discovery 404s

    assert import_queue.start_next() is None        # nothing left, not a retry
    entry = import_queue.entries()[0]
    assert entry["status"] == ImportQueueStatus.failed.value
    assert "404" in entry["note"]


def test_an_entry_that_cannot_start_at_all_does_not_stall_the_queue(
    dashboard_db, fake_site, fake_shopify, no_llm, monkeypatch
):
    def refuse(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(product_import, "start_run", refuse)
    import_queue.add(TWO)
    assert import_queue.start_next() is None

    by_url = {e["source_url"]: e for e in import_queue.entries()}
    entry = by_url["https://maker.test/collections/advantage"]
    assert entry["status"] == ImportQueueStatus.failed.value
    assert "the database went away" in entry["note"]
    # The one behind it is still waiting rather than lost with it.
    assert import_queue.waiting() == 1


def test_a_stopped_run_is_a_failed_entry_not_a_finished_one(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    import_queue.add(TWO)
    started = import_queue.start_next()
    product_import.stop_run(started["run"])

    import_queue.reconcile()
    by_url = {e["source_url"]: e for e in import_queue.entries()}
    entry = by_url["https://maker.test/collections/advantage"]
    assert entry["status"] == ImportQueueStatus.failed.value
    assert entry["run_id"] == started["run"]


# ── The job ──────────────────────────────────────────────────────────


def test_the_job_starts_one_and_says_how_many_are_waiting(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    import_queue.add(TWO)
    result = get_job("import_queue").fn()

    assert result.rows == 1
    assert result.skipped is False
    assert result.detail["waiting"] == 1
    assert "advantage" in result.detail["started"]


def test_the_job_holds_off_while_something_is_importing(
    dashboard_db, fake_site, fake_shopify, no_llm
):
    import_queue.add(TWO)
    import_queue.start_next()

    result = get_job("import_queue").fn()
    assert result.rows == 0
    assert result.skipped is True
    assert "still running" in result.skip_reason


def test_an_empty_queue_is_a_skip_not_a_failure(dashboard_db):
    result = get_job("import_queue").fn()
    assert result.skipped is True
    assert result.skip_reason == "Nothing is queued."


# ── The page ─────────────────────────────────────────────────────────


def test_the_page_queues_what_was_pasted(client, dashboard_db):
    response = client.post(
        "/import/queue",
        data={"collections": TWO},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "notice=" in response.headers["location"]
    assert import_queue.waiting() == 2

    page = client.get("/import").text
    assert "collections/advantage" in page
    assert "Ames Tile &amp; Stone" in page


def test_the_page_reports_the_lines_it_could_not_read(client, dashboard_db):
    response = client.post(
        "/import/queue",
        data={"collections": "nonsense\n"},
        follow_redirects=False,
    )
    assert "error=" in response.headers["location"]
    assert import_queue.waiting() == 0


def test_a_running_entry_cannot_be_removed_from_the_page(
    client, dashboard_db, fake_site, fake_shopify, no_llm
):
    import_queue.add(TWO)
    import_queue.start_next()
    entry = import_queue.entries()[0]

    response = client.post(
        f"/import/queue/{entry['id']}/remove", follow_redirects=False
    )
    assert "error=" in response.headers["location"]
    assert import_queue.entries()[0]["status"] == ImportQueueStatus.running.value
