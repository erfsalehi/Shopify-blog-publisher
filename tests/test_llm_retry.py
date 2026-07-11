"""structured_invoke: transient-error retry + cross-model fallback, offline."""

import pytest
from pydantic import BaseModel

import blog_pipeline.llm as llm_mod
from blog_pipeline.llm import _is_retryable, structured_invoke


class _Schema(BaseModel):
    value: str


class _FakeStructured:
    """Stands in for llm.with_structured_output(...); .invoke either raises a
    scripted error or returns an include_raw-style dict."""

    def __init__(self, behavior):
        self._behavior = behavior

    def invoke(self, messages):
        b = self._behavior
        if isinstance(b, Exception):
            raise b
        return {"parsed": _Schema(value=b), "raw": None}


class _FakeLLM:
    def __init__(self, behavior):
        self._behavior = behavior

    def with_structured_output(self, schema, include_raw=False):
        return _FakeStructured(self._behavior)


def _patch_make_llm(monkeypatch, by_model: dict):
    monkeypatch.setattr(
        llm_mod, "make_llm",
        lambda m, temperature=0.4, max_tokens=None: _FakeLLM(by_model[m]),
    )
    # No real sleeping during retry backoff.
    monkeypatch.setattr(llm_mod.time, "sleep", lambda *_: None)


def test_is_retryable_detects_transient():
    assert _is_retryable(Exception("503 UNAVAILABLE"))
    assert _is_retryable(Exception("RESOURCE_EXHAUSTED"))
    assert not _is_retryable(Exception("bad request: invalid field"))


def test_falls_back_to_next_model_on_persistent_503(monkeypatch):
    # primary always 503s; fallback succeeds.
    _patch_make_llm(
        monkeypatch,
        {"primary": Exception("503 model overloaded"), "backup": "ok"},
    )
    result = structured_invoke(
        model="primary", schema=_Schema, messages=[], stage="t",
        fallbacks=["backup"], max_attempts=2,
    )
    assert result.value == "ok"


def test_non_retryable_still_tries_fallback_then_raises(monkeypatch):
    _patch_make_llm(
        monkeypatch,
        {"primary": ValueError("hard fail"), "backup": ValueError("also fails")},
    )
    with pytest.raises(ValueError):
        structured_invoke(
            model="primary", schema=_Schema, messages=[], stage="t",
            fallbacks=["backup"], max_attempts=2,
        )


def test_primary_success_no_fallback_used(monkeypatch):
    _patch_make_llm(
        monkeypatch,
        {"primary": "first", "backup": Exception("should not be called")},
    )
    result = structured_invoke(
        model="primary", schema=_Schema, messages=[], stage="t", fallbacks=["backup"],
    )
    assert result.value == "first"
