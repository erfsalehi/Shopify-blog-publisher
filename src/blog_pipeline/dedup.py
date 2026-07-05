"""Topic dedup: exact keyword overlap + semantic similarity.

Two layers, matching the PRD risk mitigations:
  * cheap Jaccard token overlap catches near-identical phrasings,
  * fastembed (local ONNX embeddings, no torch, no API key) catches
    semantically similar topics phrased differently.

`is_duplicate` returns the matched existing topic + score when either signal
crosses its threshold, so the calendar agent can skip it and log why.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from blog_pipeline.utils import keyword_overlap

EXACT_THRESHOLD = 0.6
SEMANTIC_THRESHOLD = 0.82


@dataclass
class DedupHit:
    is_dup: bool
    matched: str | None = None
    score: float = 0.0
    method: str = ""


@lru_cache(maxsize=1)
def _embedder():
    """Lazily construct the fastembed model (downloads on first use)."""
    from fastembed import TextEmbedding

    return TextEmbedding(model_name="BAAI/bge-small-en-v1.5")


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _embed(texts: list[str]) -> list[list[float]]:
    return [list(v) for v in _embedder().embed(texts)]


def is_duplicate(
    candidate: str,
    existing: list[str],
    *,
    use_semantic: bool = True,
) -> DedupHit:
    if not existing:
        return DedupHit(is_dup=False)

    # Layer 1: exact-ish token overlap.
    best_overlap, best_match = 0.0, None
    for e in existing:
        ov = keyword_overlap(candidate, e)
        if ov > best_overlap:
            best_overlap, best_match = ov, e
    if best_overlap >= EXACT_THRESHOLD:
        return DedupHit(True, best_match, round(best_overlap, 3), "keyword_overlap")

    # Layer 2: semantic similarity via embeddings.
    if use_semantic:
        try:
            vectors = _embed([candidate, *existing])
            cand_vec = vectors[0]
            best_sim, sim_match = 0.0, None
            for e, vec in zip(existing, vectors[1:]):
                sim = _cosine(cand_vec, vec)
                if sim > best_sim:
                    best_sim, sim_match = sim, e
            if best_sim >= SEMANTIC_THRESHOLD:
                return DedupHit(True, sim_match, round(best_sim, 3), "semantic")
        except Exception:
            # Embedding unavailable (offline / model download blocked): fall
            # back to the keyword signal already computed.
            pass

    return DedupHit(False, best_match, round(best_overlap, 3), "keyword_overlap")


def filter_new_topics(
    candidates: list[str], existing: list[str], *, use_semantic: bool = True
) -> tuple[list[str], list[dict]]:
    """Split candidates into (kept, rejected-with-reason), deduping against
    existing AND against already-kept candidates in this batch."""
    kept: list[str] = []
    rejected: list[dict] = []
    pool = list(existing)
    for c in candidates:
        hit = is_duplicate(c, pool, use_semantic=use_semantic)
        if hit.is_dup:
            rejected.append(
                {"topic": c, "matched": hit.matched, "score": hit.score,
                 "method": hit.method}
            )
        else:
            kept.append(c)
            pool.append(c)
    return kept, rejected
