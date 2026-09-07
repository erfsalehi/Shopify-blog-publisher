"""A list of collections to import, worked through one at a time.

Importing a range is minutes of fetching, and until now it was attended: the
owner pasted a URL, watched it finish, and pasted the next one. A supplier
with thirty ranges is thirty of those. This is the list they can fill in one
sitting and leave.

**One at a time, and never a new one while the last is unfinished.** Not
politeness — correctness. Two runs going at once compete for the same bounded
passes, so both crawl; and the second range's collection and cross-linking
stages would interleave with the first's. `start_next` therefore does nothing
at all while any import is still active, which makes the cadence "one per
day" on a good day and "when the one in front finishes" otherwise. Those are
the same thing for a range that fits in a day and honestly different for one
that doesn't.

**Nothing is retried.** A queue that re-runs a failed entry is a queue that
never advances past it — a supplier page that broke once usually breaks the
same way twice. A failed entry keeps its reason and its run, and the owner
decides.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from dashboard import product_import
from dashboard.db import get_session
from dashboard.models import ImportQueueEntry, ImportQueueStatus, ImportRun

log = logging.getLogger(__name__)

#: What a pasted line may separate its fields with. A URL never contains any
#: of these, and someone pasting from a spreadsheet gets a tab.
_SEPARATORS = ("\t", "|", ",")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class QueueError(RuntimeError):
    """Something about the entry itself is wrong."""


def parse_lines(text: str) -> tuple[list[dict], list[str]]:
    """Read pasted lines into entries, and say which lines couldn't be.

    One collection per line: `url, brand` or `url, brand, collection title`.
    Both are returned rather than raising on the first bad line, because a
    list of thirty pasted at once should add the twenty-nine that are fine
    and tell you about the one that isn't — losing the lot to a typo in the
    middle is the behaviour that makes people stop pasting lists.
    """
    entries: list[dict] = []
    problems: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        separator = next((s for s in _SEPARATORS if s in line), None)
        if separator is None:
            problems.append(f"{line} — no brand given (write “url, brand”)")
            continue
        parts = [p.strip() for p in line.split(separator)]
        url, vendor = parts[0], (parts[1] if len(parts) > 1 else "")
        title = parts[2] if len(parts) > 2 else ""

        if not url.lower().startswith(("http://", "https://")):
            problems.append(f"{url} — not a URL")
            continue
        if not vendor:
            # The brand is the first word of every product name and most
            # suppliers publish it nowhere a scraper can read. Refused here
            # rather than discovered hours later in an unnamed range.
            problems.append(f"{url} — no brand given")
            continue
        entries.append(
            {"source_url": url, "vendor": vendor, "collection_title": title or None}
        )
    return entries, problems


def add(text: str, *, dry_run: bool = False) -> tuple[int, list[str]]:
    """Append pasted collections to the end of the queue."""
    entries, problems = parse_lines(text)
    if not entries:
        return 0, problems

    with get_session() as session:
        last = (
            session.query(ImportQueueEntry.position)
            .order_by(ImportQueueEntry.position.desc())
            .first()
        )
        position = (last.position if last else 0) + 1
        existing = {
            row.source_url
            for row in session.query(ImportQueueEntry.source_url)
            .filter(
                ImportQueueEntry.status.in_([
                    ImportQueueStatus.queued.value,
                    ImportQueueStatus.running.value,
                ])
            )
            .all()
        }
        added = 0
        for entry in entries:
            if entry["source_url"] in existing:
                # Already waiting. Silently skipping it is wrong — a list
                # pasted twice should say so rather than look like it worked.
                problems.append(f"{entry['source_url']} — already in the queue")
                continue
            session.add(
                ImportQueueEntry(position=position, dry_run=bool(dry_run), **entry)
            )
            existing.add(entry["source_url"])
            position += 1
            added += 1
    return added, problems


def remove(entry_id: int) -> bool:
    """Take an entry off the queue. Only one that hasn't started."""
    with get_session() as session:
        entry = session.get(ImportQueueEntry, entry_id)
        if entry is None or entry.status != ImportQueueStatus.queued.value:
            return False
        entry.status = ImportQueueStatus.cancelled.value
        entry.finished_at = _now()
        return True


def reconcile() -> int:
    """Settle entries whose run has finished. Returns how many changed.

    The queue records that it *started* a run; what became of it is the run's
    own business, and asking is cheaper and more truthful than trying to keep
    a second copy of the answer up to date.
    """
    changed = 0
    with get_session() as session:
        running = (
            session.query(ImportQueueEntry)
            .filter(ImportQueueEntry.status == ImportQueueStatus.running.value)
            .all()
        )
        for entry in running:
            run = session.get(ImportRun, entry.run_id) if entry.run_id else None
            if run is None:
                entry.status = ImportQueueStatus.failed.value
                entry.note = "Its import run no longer exists."
            elif run.is_active:
                continue
            elif run.stage == "done":
                entry.status = ImportQueueStatus.done.value
                entry.note = None
            else:
                entry.status = ImportQueueStatus.failed.value
                entry.note = (run.error or f"The run {run.stage}.")[:500]
            entry.finished_at = _now()
            changed += 1
    return changed


def start_next() -> dict | None:
    """Start the next queued collection, if nothing else is importing.

    Returns what it started, or None with a reason it didn't. Deliberately
    starts exactly one: the point of the queue is that a catalogue arrives
    over days without anyone watching, not that thirty ranges fetch at once.
    """
    reconcile()

    if product_import.active_run_ids(limit=1):
        return None

    with get_session() as session:
        entry = (
            session.query(ImportQueueEntry)
            .filter(ImportQueueEntry.status == ImportQueueStatus.queued.value)
            .order_by(ImportQueueEntry.position, ImportQueueEntry.id)
            .first()
        )
        if entry is None:
            return None
        entry_id = entry.id
        source_url, vendor = entry.source_url, entry.vendor
        title, dry_run = entry.collection_title, entry.dry_run

    try:
        run_id = product_import.start_run(
            source_url,
            dry_run=dry_run,
            collection_title=title,
            vendor=vendor,
            make_collection=True,
            link_products=True,
            collection_mode="new",
            build_page=True,
        )
    except Exception as exc:  # noqa: BLE001 - a bad entry is not a dead queue
        log.warning("queued import %s could not start: %s", entry_id, exc)
        with get_session() as session:
            entry = session.get(ImportQueueEntry, entry_id)
            if entry is not None:
                entry.status = ImportQueueStatus.failed.value
                entry.note = f"Could not start: {exc}"[:500]
                entry.finished_at = _now()
        return None

    with get_session() as session:
        entry = session.get(ImportQueueEntry, entry_id)
        entry.status = ImportQueueStatus.running.value
        entry.run_id = run_id
        entry.started_at = _now()
    return {"entry": entry_id, "run": run_id, "source_url": source_url}


def waiting() -> int:
    with get_session() as session:
        return (
            session.query(ImportQueueEntry)
            .filter(ImportQueueEntry.status == ImportQueueStatus.queued.value)
            .count()
        )


def entries(limit: int = 100) -> list[dict]:
    """The queue as the page shows it: what's waiting, then what happened."""
    order = {
        ImportQueueStatus.running.value: 0,
        ImportQueueStatus.queued.value: 1,
    }
    with get_session() as session:
        rows = (
            session.query(ImportQueueEntry)
            .order_by(ImportQueueEntry.position, ImportQueueEntry.id)
            .limit(limit)
            .all()
        )
        return sorted(
            (
                {
                    "id": r.id,
                    "source_url": r.source_url,
                    "vendor": r.vendor,
                    "collection_title": r.collection_title,
                    "dry_run": r.dry_run,
                    "status": r.status,
                    "run_id": r.run_id,
                    "note": r.note,
                    "created_at": r.created_at,
                    "started_at": r.started_at,
                }
                for r in rows
            ),
            key=lambda e: (order.get(e["status"], 2), e["id"]),
        )
