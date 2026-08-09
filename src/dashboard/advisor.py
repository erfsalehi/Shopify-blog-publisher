"""Per-tab LLM advice, grounded in this database and aware of what was done.

The failure mode this module is designed against is not "the advice is bland".
It is **a confident paragraph containing a number that does not exist.** A
dashboard whose numbers are checkable sitting next to a model that invents one
is worse than no advisor at all, because the invented figure inherits the
credibility of the real ones.

Four things follow from that:

**The model only ever sees a brief built from real rows.** Every figure in
`context.py` comes from a query. The model is given no tools and no ability to
fetch, so it cannot introduce a number from anywhere else.

**The brief is stored with the note.** A suggestion you can't trace back to
the numbers behind it is an assertion. `AdvisorNote.context_md` keeps them
together permanently.

**Output is checked against the brief.** `unverified_figures` pulls the
substantial numbers out of the response and flags any that don't appear in the
context. It is deliberately not a blocker — the model legitimately computes
derived values like percentage changes — but anything it flags is shown to the
owner as unverified rather than displayed as fact. See that function for what
it can and cannot catch.

**Memory is outcomes, not transcripts.** Feeding old notes back only tells the
model what it once said. Feeding back which suggestions were marked *done* and
which were *dismissed* lets it reason about consequences — "titles changed
three weeks ago; CTR since" — and stop repeating advice already rejected.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dashboard import store
from dashboard.db import get_session
from dashboard.models import AdvisorAction, AdvisorNote

log = logging.getLogger(__name__)

SCOPES = (
    "overview", "products", "blog", "keywords", "ads", "experiments",
    "competitors",
)

SCOPE_TITLES = {
    "overview": "Organic search overview",
    "products": "Product catalogue in search",
    "blog": "Blog performance and decay",
    "keywords": "Search terms and market opportunity",
    "ads": "Google Ads against organic and calls",
    "experiments": "Running experiments",
    "competitors": "Competitors' catalogues, prices and blogs",
}

_SYSTEM = """You advise the owner of D&R Flooring, a flooring retailer and \
installer in Langley, British Columbia, on their website's performance.

Critical facts about this business:
- Almost nothing is bought online. 94% of the catalogue hides its price behind \
a "Call for price" button. The conversion is a PHONE CALL, tracked in GA4 as \
call_click and whatsapp_click. Never treat sessions or add-to-carts as the goal.
- The owner is one person who also runs the shop. Advice must be something a \
non-specialist can act on in an hour, not a quarter-long programme.

Rules you must follow:
1. Use ONLY the figures in the CONTEXT below. Never state a metric that is not \
there. If you need a number you do not have, say what you'd need and why.
2. Do not repeat a suggestion listed as DISMISSED. The owner considered it and \
said no.
3. When a suggestion was marked DONE, comment on what happened after it, using \
the figures given.
4. Be specific. "Improve your meta descriptions" is useless. "The three pages \
below have >1000 impressions and under 1% CTR — rewrite their titles to lead \
with the price" is useful.
5. Say plainly when the data is too thin to conclude anything. That is a valid \
and valuable answer.

Reply as JSON with exactly this shape:
{"summary": "one or two sentences on how this area is doing",
 "reading": "a short paragraph of analysis, markdown allowed",
 "actions": ["specific thing to do", "another"]}
No other keys."""

# Appended rather than interpolated. This prompt contains literal percent
# signs ("94% of the catalogue") and literal braces (the JSON example), so
# both %-formatting and str.format() misread it — %-formatting actually did,
# reading "94% o" as an octal conversion.
_ACTION_LIMIT = "\nReturn at most {n} actions."


def _system_prompt(max_actions: int) -> str:
    return _SYSTEM + _ACTION_LIMIT.replace("{n}", str(max_actions))


@dataclass(frozen=True)
class Advice:
    summary: str
    reading: str
    actions: list[str]


def _clean_json(text: str) -> dict:
    """Parse the model's reply, tolerating a ```json fence."""
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    return json.loads(body)


# Figures worth verifying: 3+ digits, or anything with a currency symbol,
# comma grouping, or a decimal point. Bare small integers ("3 pages", "top 5")
# are excluded because the model legitimately counts things, and flagging
# those would produce so much noise that the real catches get ignored.
_FIGURE = re.compile(r"\$?\d[\d,]*\.?\d*%?")


def _significant(token: str) -> bool:
    digits = re.sub(r"[^\d]", "", token)
    if len(digits) >= 3:
        return True
    return "$" in token or "," in token


def unverified_figures(output: str, context: str) -> list[str]:
    """Substantial numbers in the output that don't appear in the brief.

    Necessarily imperfect, and honest about it: the model may correctly derive
    "down 31%" from two numbers that are both present, and that will be
    flagged. So this annotates rather than blocks — the point is to draw the
    owner's eye to a figure worth checking, not to claim the note is wrong.

    What it reliably catches is the dangerous case: a plausible-looking
    absolute — a click count, a dollar figure — that came from nowhere.
    """
    context_digits = set(_FIGURE.findall(context))
    # Compare on digits alone so "$1,529" in the output matches "1529" in a
    # table, and 1529.0 matches 1529.
    def norm(token: str) -> str:
        return re.sub(r"[^\d]", "", token).lstrip("0") or "0"

    known = {norm(t) for t in context_digits}
    out: list[str] = []
    for token in _FIGURE.findall(output):
        if not _significant(token):
            continue
        if norm(token) in known:
            continue
        if token not in out:
            out.append(token)
    return out


def _memory(scope: str, limit: int = 12) -> str:
    """Prior suggestions and what became of them."""
    with get_session() as session:
        actions = (
            session.query(AdvisorAction)
            .filter(AdvisorAction.scope == scope)
            .order_by(AdvisorAction.created_at.desc())
            .limit(limit)
            .all()
        )
        for a in actions:
            session.expunge(a)
        previous = (
            session.query(AdvisorNote)
            .filter(AdvisorNote.scope == scope, AdvisorNote.error.is_(None))
            .order_by(AdvisorNote.created_at.desc())
            .first()
        )
        if previous is not None:
            session.expunge(previous)

    lines: list[str] = []
    if previous is not None and previous.summary:
        age = (datetime.now(timezone.utc) - _aware(previous.created_at)).days
        lines.append(f"YOUR PREVIOUS NOTE ({age} days ago): {previous.summary}")
    if actions:
        lines.append("")
        lines.append("PREVIOUS SUGGESTIONS AND WHAT HAPPENED:")
        for a in actions:
            age = (datetime.now(timezone.utc) - _aware(a.created_at)).days
            label = a.status.upper()
            extra = f" — owner's note: {a.note}" if a.note else ""
            lines.append(f"- [{label}, suggested {age}d ago] {a.text}{extra}")
    if not lines:
        lines.append("No previous advice for this area — this is the first note.")
    return "\n".join(lines)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def generate(scope: str, *, model: str | None = None) -> AdvisorNote:
    """Build the brief, call the model, store the note and its actions."""
    from dashboard.advisor_context import build_context

    if scope not in SCOPES:
        raise ValueError(f"unknown advisor scope {scope!r}")

    model = model or store.get(store.ADVISOR_MODEL)
    max_actions = store.get(store.ADVISOR_MAX_ACTIONS)
    context = build_context(scope)
    memory = _memory(scope)
    prompt = (
        f"AREA: {SCOPE_TITLES[scope]}\n\n"
        f"CONTEXT (the only figures that exist):\n{context}\n\n"
        f"{memory}\n"
    )

    note = AdvisorNote(scope=scope, model=model, context_md=context)
    try:
        text, usage, used_model = _call(model, _system_prompt(max_actions), prompt)
        note.model = used_model
        parsed = _clean_json(text)
        advice = Advice(
            summary=str(parsed.get("summary") or "").strip(),
            reading=str(parsed.get("reading") or "").strip(),
            actions=[
                str(a).strip() for a in (parsed.get("actions") or [])
                if str(a).strip()
            ][:max_actions],
        )
        note.summary = advice.summary
        note.body_md = advice.reading
        note.input_tokens = usage.get("input", 0)
        note.output_tokens = usage.get("output", 0)
        checked = f"{advice.summary}\n{advice.reading}\n" + "\n".join(advice.actions)
        note.unverified_json = json.dumps(unverified_figures(checked, context))
    except Exception as exc:  # noqa: BLE001
        # A failed generation is recorded, not raised: the jobs page and the
        # tab both need to show that the advisor tried and why it couldn't.
        log.exception("advisor generation failed for %s", scope)
        note.error = f"{type(exc).__name__}: {exc}"
        advice = None

    with get_session() as session:
        session.add(note)
        session.flush()
        note_id = note.id
        if advice is not None:
            # Superseded open actions are closed rather than accumulating:
            # a checklist that only grows is one nobody reads.
            session.query(AdvisorAction).filter(
                AdvisorAction.scope == scope,
                AdvisorAction.status == "open",
            ).update({"status": "dismissed",
                      "note": "superseded by a newer note",
                      "resolved_at": datetime.now(timezone.utc)},
                     synchronize_session=False)
            for text_ in advice.actions:
                session.add(AdvisorAction(
                    note_id=note_id, scope=scope, text=text_, status="open",
                ))
        session.expunge(note)
    return note


def _call(model: str, system: str, prompt: str) -> tuple[str, dict, str]:
    """One Gemini call, falling back through models that actually have quota.

    Being listed in `GET /v1beta/models` is not entitlement. Measured against
    this key on 2026-08-08, both `gemini-3-pro-preview` and
    `gemini-3.1-pro-preview` return 429 with `limit: 0` for free-tier requests
    *and* input tokens — they appear in the catalog and cannot be called at
    all. The Flash models work.

    So a configured model that turns out to be unusable falls through to one
    that isn't, and the note records which model actually answered. Silently
    producing nothing because someone picked an unavailable model from a
    dropdown would be the worst of both.
    """
    from blog_pipeline.config import get_settings
    from blog_pipeline.llm import make_llm

    candidates = [model] + [
        m for m in get_settings().llm_fallback_models_list if m != model
    ]
    messages = [
        {"role": "system", "content": system}, {"role": "user", "content": prompt}
    ]
    last: Exception | None = None
    for candidate in candidates:
        try:
            response = make_llm(candidate, temperature=0.4).invoke(messages)
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("advisor model %s unavailable (%s); trying next",
                        candidate, str(exc)[:120])
            continue
        usage = getattr(response, "usage_metadata", None) or {}
        return str(response.content), {
            "input": int(usage.get("input_tokens", 0)),
            "output": int(usage.get("output_tokens", 0)),
        }, candidate
    raise last or RuntimeError("no advisor model available")


# ── Reading side ────────────────────────────────────────────────────


def latest_note(scope: str) -> AdvisorNote | None:
    with get_session() as session:
        row = (
            session.query(AdvisorNote)
            .filter(AdvisorNote.scope == scope)
            .order_by(AdvisorNote.created_at.desc(), AdvisorNote.id.desc())
            .first()
        )
        if row is not None:
            session.expunge(row)
    return row


def actions_for(scope: str, *, include_resolved: bool = False) -> list[AdvisorAction]:
    with get_session() as session:
        query = session.query(AdvisorAction).filter(AdvisorAction.scope == scope)
        if not include_resolved:
            query = query.filter(AdvisorAction.status == "open")
        rows = query.order_by(AdvisorAction.created_at.desc()).limit(40).all()
        for row in rows:
            session.expunge(row)
    return rows


def resolve(action_id: int, status: str, note: str | None = None) -> None:
    if status not in ("open", "done", "dismissed"):
        raise ValueError(f"bad status {status!r}")
    with get_session() as session:
        row = session.get(AdvisorAction, action_id)
        if row is None:
            return
        row.status = status
        row.note = note or None
        row.resolved_at = None if status == "open" else datetime.now(timezone.utc)


def open_action_count(scope: str) -> int:
    with get_session() as session:
        return (
            session.query(AdvisorAction)
            .filter(AdvisorAction.scope == scope, AdvisorAction.status == "open")
            .count()
        )


def stale_scopes(older_than_days: int = 7) -> list[str]:
    """Scopes whose note is missing or older than the interval."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    out = []
    for scope in SCOPES:
        note = latest_note(scope)
        if note is None or note.error or _aware(note.created_at) < cutoff:
            out.append(scope)
    return out
