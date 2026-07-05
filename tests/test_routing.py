"""Confidence-based routing after QA. Patches settings to exercise each path."""

import blog_pipeline.graphs.article_graph as ag


class _FakeSettings:
    def __init__(self, gate_mode="gated", threshold=0.75):
        self.gate_mode = gate_mode
        self.confidence_threshold = threshold


def _patch(monkeypatch, gate_mode, threshold=0.75):
    monkeypatch.setattr(ag, "get_settings", lambda: _FakeSettings(gate_mode, threshold))


def test_block_verdict_routes_to_blocked(monkeypatch):
    _patch(monkeypatch, "auto")
    state = {"qa_report": {"verdict": "block"}, "confidence": 0.99}
    assert ag.route_after_qa(state) == "blocked"


def test_auto_mode_high_confidence_publishes(monkeypatch):
    _patch(monkeypatch, "auto", threshold=0.75)
    state = {"qa_report": {"verdict": "pass"}, "confidence": 0.9}
    assert ag.route_after_qa(state) == "publish"


def test_auto_mode_low_confidence_gates(monkeypatch):
    _patch(monkeypatch, "auto", threshold=0.75)
    state = {"qa_report": {"verdict": "pass"}, "confidence": 0.5}
    assert ag.route_after_qa(state) == "gate"


def test_gated_mode_always_gates_on_pass(monkeypatch):
    _patch(monkeypatch, "gated")
    state = {"qa_report": {"verdict": "pass"}, "confidence": 0.99}
    assert ag.route_after_qa(state) == "gate"


def test_route_after_gate_reads_status():
    assert ag.route_after_gate({"status": "approved"}) == "publish"
    assert ag.route_after_gate({"status": "rejected"}) == "rejected"
