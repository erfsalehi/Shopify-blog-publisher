"""Not judging an article on a copy we damaged ourselves.

Six consecutive articles sat unpublished at confidence 0.5-0.7 against a
0.75 gate, every one of them complete in the database. QA was handed the
first 12,000 characters of a 17,000-character article, and the system prompt
tells it to "be strict about truncated/incomplete drafts" — so it reported,
accurately, that what it had been given stopped mid-sentence.

Measured on the real corpus: articles under the old cap averaged 0.96
confidence, articles over it averaged 0.70.
"""

from __future__ import annotations

from blog_pipeline.agents.draft import _looks_truncated
from blog_pipeline.agents.qa import _MAX_REVIEW_CHARS, _for_review
from blog_pipeline.llm import TruncatedResponse, _hit_token_ceiling, _is_retryable


class _Raw:
    def __init__(self, **meta):
        self.response_metadata = meta


# ── What QA is shown ────────────────────────────────────────────────


def test_a_normal_article_reaches_qa_whole():
    """The bug in one assertion: a 17,000-character article — the size this
    pipeline now writes — must arrive intact."""
    article = "word " * 3400  # ~17,000 chars

    assert _for_review(article) == article
    assert len(article) > 12_000, "the size that used to be cut"


def test_the_cap_is_far_above_anything_written():
    """Room for roughly ten of the longest article the pipeline produces, so
    growth doesn't silently re-introduce this."""
    assert _MAX_REVIEW_CHARS >= 100_000


def test_an_abridged_article_says_so_in_the_text():
    """If a cut is ever unavoidable it must be labelled. The model cannot
    distinguish "the author stopped" from "the harness stopped sending", and
    when it can't, it reports the only thing it can see."""
    out = _for_review("x" * (_MAX_REVIEW_CHARS + 500))

    assert "abridged" in out
    assert "incomplete draft" in out


# ── The heuristic that never fired ──────────────────────────────────


def test_prose_ending_mid_sentence_is_caught_even_with_closed_tags():
    """The old check asked whether the HTML ended with '>'. A model that
    stops mid-sentence still closes its tags, so it answered "fine" for every
    draft ever written and the retry beside it never once ran."""
    html = "<p>The subfloor must be clean, dry, and </p>"

    assert html.rstrip().endswith(">")   # the old check passed this
    assert _looks_truncated(html) is True


def test_a_finished_article_is_not_flagged():
    assert _looks_truncated("<p>Level the subfloor before installing.</p>") is False


def test_the_trailing_json_ld_block_is_not_mistaken_for_prose():
    """Every body now ends with a FAQPage <script>. Its closing brace is
    punctuation, so leaving it in would satisfy the check on its own and
    re-create the always-passes bug in a new form."""
    truncated = (
        "<p>Acclimate the planks for at least 48 hours before you </p>"
        '<script type="application/ld+json">{"@type":"FAQPage"}</script>'
    )

    assert _looks_truncated(truncated) is True


def test_an_empty_body_counts_as_truncated():
    assert _looks_truncated("") is True
    assert _looks_truncated("<div></div>") is True


# ── The provider's own signal ───────────────────────────────────────


def test_the_token_ceiling_is_read_from_the_response():
    """Stated by the provider, not inferred from the text. Every text-based
    guess has to decide what a finished answer looks like, and the ones here
    were wrong in both directions."""
    assert _hit_token_ceiling(_Raw(finish_reason="length")) is True
    assert _hit_token_ceiling(_Raw(finish_reason="stop")) is False
    assert _hit_token_ceiling(None) is False
    assert _hit_token_ceiling(_Raw()) is False


def test_both_provider_spellings_are_recognised():
    """OpenAI-compatible endpoints say "length"; native Gemini says
    MAX_TOKENS. This routes through the former today and shouldn't break if
    that changes."""
    assert _hit_token_ceiling(_Raw(finish_reason="MAX_TOKENS")) is True
    assert _hit_token_ceiling(_Raw(stop_reason="max_tokens")) is True


def test_a_truncated_response_is_retried_rather_than_returned():
    """Sampling differs between attempts and the fallback chain may reach a
    model with more headroom. Returning the fragment is the one option that
    can't work."""
    assert _is_retryable(TruncatedResponse("ran out")) is True
