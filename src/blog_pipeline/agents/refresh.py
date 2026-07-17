"""Content refresh agent — brings an existing live post back up to date.

Distinct from agents/revise.py, which cannot do this job: revise is driven by
_diagnose(metrics) and returns the body untouched when the SEO rubric has no
complaint. A four-year-old article can score perfectly on that rubric while
being thoroughly stale — right keyword density, wrong decade. Staleness isn't
a rubric failure, so it needs its own prompt and its own reasons to edit.

The output overwrites a live, indexed page (Shopify has no draft revision for
a published post), which shapes the prompt heavily: preserve what already
earns rankings, never rename or restructure for its own sake, and prefer an
honest no-op over invented churn. The caller snapshots the previous body
first — see db.ArticleRevision.
"""

from __future__ import annotations

from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage

from blog_pipeline.config import get_settings
from blog_pipeline.llm import CostTracker, structured_invoke
from blog_pipeline.schemas import RefreshedArticle
from blog_pipeline.utils import word_count

SYSTEM = """You are a content editor refreshing an article that is already \
published and may already rank in Google. Your edits go live immediately.

That asymmetry governs everything: a page that ranks has earned something you \
cannot see from the text alone. Improving it is worth real money; breaking it \
costs real money. When a change is marginal, don't make it.

Rules:
- Output the COMPLETE refreshed article as clean semantic HTML: <h2>, <h3>, \
<p>, <ul>/<ol>/<li>, <blockquote>. No <html>/<head>/<body>/<h1>.
- PRESERVE every existing <a href> link and <figure>/<img> block exactly. They \
are internal links and hosted assets — dropping one is a real regression.
- Do NOT invent statistics, prices, studies, dates, or product claims. You do \
not know today's prices or this year's model numbers. If something reads as \
dated but you can't source the current fact, rewrite it to be durable \
("modern vinyl planks typically...") rather than swapping in a guess. A \
plausible invented number is the worst possible outcome here.
- Keep the article's angle and scope. This is a refresh, not a rewrite: the \
reader who searched for this should still land on what they wanted.
- Prefer keeping the title. A ranking page's title is load-bearing; only \
propose a new one if the current is genuinely poor.

What actually justifies an edit:
- Advice that has aged badly, or omits an option now standard in the field.
- Thin sections that don't answer the question they promise.
- Structure: sections that ramble past ~400 words or stop under ~150, where \
each <h2> should stand alone and open with a direct answer.
- Missing depth a reader would now expect — practical steps, comparisons, \
trade-offs, common mistakes.
- Readability: shorter sentences, plainer words, less throat-clearing.

DEPTH. You are given the article's word count and a target. An article far \
under target is usually losing to competitors who answer the question more \
completely, and "tidied the prose" will not win that back. Where you can add \
real substance the reader wants — the trade-off nobody mentions, the mistake \
people make, the step-by-step, the honest comparison — add it. This is the \
one place to be ambitious.

But depth means answering more, not saying the same thing at greater length. \
Never pad, never restate, never write a paragraph to hit a number. If you \
genuinely have nothing more of substance, a short honest article beats a long \
padded one — say so in change_summary rather than filling space.

CITATION LEVERS (key_takeaways, faq, pull_quote). These are separate fields, \
not body HTML — the caller renders them. They are how AI answer engines and \
featured snippets quote a page, and an older article almost certainly has \
none. Fill them from what the article actually says:
- key_takeaways: each must survive being quoted alone, with no context.
- faq: questions real readers ask, answered from the article's substance. Not \
keyword restatements dressed as questions.
- pull_quote: honest and first-party, or naming a real standards body. This is \
the biggest single lever, and also the easiest to fake. An empty pull_quote is \
infinitely better than an invented authority or a fabricated statistic. If \
nothing genuine fits, leave it empty.

IMAGE_SUGGESTIONS. A separate field, never body HTML. This page has no human \
review before going live, so under no circumstances write an [IMAGE - ...] \
marker or any placeholder text into body_html — that would be public the \
instant this is applied. If a section you genuinely expanded or added would \
be clearer with a photo or diagram, describe it here instead: role, where it \
goes, a prompt, alt text. Most refreshes touch nothing that needs a new \
image — leave this empty rather than inventing a reason for one. This is a \
suggestion a human acts on later, not part of the edit itself.

If none of that applies, set skipped=true and return the body unchanged. An \
honest no-op is a good answer; busywork on a ranking page is not."""


def _age_hint(published_at: datetime | None) -> str:
    if published_at is None:
        return "Published: unknown."
    now = datetime.now(timezone.utc)
    # Imported rows may be naive depending on the backend that stored them.
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    years = (now - published_at).days / 365.25
    return (
        f"Published: {published_at.date().isoformat()} "
        f"(~{years:.1f} years ago). Judge what has plausibly changed in the "
        "field since then — but do not invent specifics to fill the gap."
    )


def refresh_article(
    *,
    title: str,
    body_html: str,
    published_at: datetime | None = None,
    business_context: str = "",
    must_keep: list[str] | None = None,
    forbid_image_markers: bool = False,
    cost: CostTracker | None = None,
) -> RefreshedArticle:
    """Refresh one live post. Returns the article unchanged with skipped=True
    when the model judges it doesn't need the work.

    `must_keep` and `forbid_image_markers` are the retry path for the caller's
    publish guards: which specific rule the previous attempt broke despite the
    standing instruction. Naming the exact failure works where the general
    instruction didn't — see refresh_graph.lost_assets /
    has_stray_image_marker for what triggers each.
    """
    settings = get_settings()

    # Give the model the actual gap, not just the target. "546 words vs 1500"
    # is actionable; "aim for ~1500" against an unstated current length reads
    # as boilerplate and gets ignored — which is how a 546-word post came back
    # at 527 with the prose merely tidied.
    current_words = word_count(body_html)
    target = settings.word_count_target
    length_note = f"Current length: ~{current_words} words. Target: ~{target}."
    if current_words < 0.6 * target:
        length_note += (
            f" This is well short — roughly {target - current_words} words of "
            "genuine substance are missing. Thin content is a likely reason it "
            "lost ground. Add real depth where the topic supports it, but do "
            "not pad to reach the number."
        )

    human = [
        f"Title: {title}",
        _age_hint(published_at),
        length_note,
    ]
    if business_context:
        human.append(f"Publisher context: {business_context}")
    if must_keep:
        human.append(
            "HARD REQUIREMENT — a previous attempt at this refresh dropped the "
            "following asset URLs, which is a publishing-blocking regression. "
            "Every one of these must appear in your output, byte-for-byte, in "
            "its original <img src> or <a href>:\n"
            + "\n".join(f"- {u}" for u in must_keep)
        )
    if forbid_image_markers:
        human.append(
            "HARD REQUIREMENT — a previous attempt wrote a bracketed "
            "[IMAGE - ...] placeholder into body_html. This page is live with "
            "no human review before publishing, so that text would be public. "
            "Do not write any [IMAGE ...] marker or bracketed image "
            "instruction into body_html. If you have an idea for a new image, "
            "put it in image_suggestions instead — never in the body."
        )
    human.append("Current article HTML:\n" + body_html)

    result: RefreshedArticle = structured_invoke(
        model=settings.model_draft,
        schema=RefreshedArticle,
        messages=[
            SystemMessage(content=SYSTEM),
            HumanMessage(content="\n\n".join(human)),
        ],
        temperature=0.5,
        stage="refresh",
        cost=cost,
        max_tokens=16384,
    )
    # A model can set skipped and still return a mangled/empty body; trust the
    # flag, not the payload, and hand back exactly what was there before.
    if result.skipped or not result.body_html.strip():
        return RefreshedArticle(
            body_html=body_html, change_summary=[], skipped=True
        )
    return result
