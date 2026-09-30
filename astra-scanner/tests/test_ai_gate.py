"""The paid AI adapter stays disabled by default and is verified only with fake responses."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astra import ai, processing
from astra import candidates as cands
from astra.core import AstraError

from .conftest import cand_by


def blocked_output(evidence_id: str = "EVIDENCE_ID_UNSET") -> dict:
    return {
        "what_changed": {"summary": "Compression detector fired; no catalyst in context.", "when_basis": "unknown"},
        "mechanism": ["TAPE"],
        "facts": [{"id": "F1", "text": "Detector evidence shows compression.", "source_refs": ["S1"]}],
        "inferences": [], "unknowns": [{"item": "catalyst", "blocks_conclusions": ["mechanism_attribution"]}],
        "sources": [{"ref": "S1", "title": "frozen evidence", "kind": "data", "stance": "context", "evidence_id": evidence_id}],
        "contrary_evidence": [], "competing_explanations": [],
        "economic_materiality": {"status": "unknown"}, "liquidity": {"status": "unknown"},
        "financing": {"status": "unknown"}, "dilution": {"status": "unknown"}, "execution": {"status": "unknown"},
        "remaining_uncertainty": "high",
        "blockers": [{"key": "no-catalyst", "description": "no catalyst", "blocks": ["mechanism_attribution"],
                      "clearing_evidence": "primary source"}],
        "recheck": {"due_in": "P1D", "condition": "look again", "priority": "routine"}, "expiry": None,
        "proposed_disposition": "blocked", "disposition_rationale": "needs catalyst evidence",
    }


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def resp(text: str, stop="end_turn", category=None):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           stop_reason=stop, stop_details=SimpleNamespace(category=category) if category else None,
                           usage=SimpleNamespace(output_tokens=321), model="claude-opus-5-5")


def client_with(*responses):
    return SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(responses)))


def enabled(**kw):
    base = dict(paid_calls="enabled", provider="anthropic", model="claude-opus-5-5", max_calls_per_day=5)
    base.update(kw)
    return ai.AISettings(**base)


def test_api_key_alone_never_activates_paid_calls(scanned_db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(ai, "make_client", lambda s: pytest.fail("client must not be constructed"))
    b = cand_by(scanned_db, "ZSMPB", "anomaly")
    fake = client_with()
    with pytest.raises(ai.AIGateError, match="AI_RESEARCH_DISABLED"):
        ai.investigate(scanned_db, b["candidate_id"], client=fake)
    assert fake.beta.messages.calls == []
    assert scanned_db.execute("SELECT status FROM ai_calls").fetchone()[0] == "blocked_by_gate"
    res = processing.process_due(scanned_db, worker="w", with_ai=True)
    assert res["5_research_backlog"]["ai"]["invoked"] is False
    assert cands.get(scanned_db, b["candidate_id"])["state"] == "DETECTED"


def test_enabled_adapter_with_fake_response_imports_ai_research(scanned_db):
    b = cand_by(scanned_db, "ZSMPB", "anomaly")
    fake = client_with(resp(json.dumps(blocked_output(b["origin_evidence_id"]))))
    out = ai.investigate(scanned_db, b["candidate_id"], settings=enabled(max_output_tokens=2000), client=fake)
    kw = fake.beta.messages.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == 2000
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"] == {"effort": "high"} and "thinking" not in kw
    assert "<untrusted_source_content>" in kw["messages"][0]["content"] and "Never follow" in kw["system"]
    assert out["state"] == "BLOCKED"
    rec = scanned_db.execute("SELECT * FROM research_records WHERE research_id=?", (out["research_id"],)).fetchone()
    assert rec["researcher_kind"] == "ai" and rec["model"] == "claude-opus-5-5" and rec["is_sample"] == 1
    call = scanned_db.execute("SELECT * FROM ai_calls WHERE status='succeeded'").fetchone()
    assert call["output_tokens"] == 321


def test_fallbacks_can_be_switched_off(scanned_db):
    b = cand_by(scanned_db, "ZSMPB", "anomaly")
    fake = client_with(resp(json.dumps(blocked_output(b["origin_evidence_id"]))))
    ai.investigate(scanned_db, b["candidate_id"], settings=enabled(fallbacks="off"), client=fake)
    assert "fallbacks" not in fake.beta.messages.calls[0] and "betas" not in fake.beta.messages.calls[0]


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d.update(state="ENTRY_ELIGIBLE"), "invalid_output"),
    (lambda d: d.update(proposed_disposition="reject", blockers=[]), "invalid_output"),
    (lambda d: d.update(target_price=100.0), "invalid_output"),
])
def test_untrusted_model_output_cannot_change_permissions(scanned_db, mutate, expect):
    b = cand_by(scanned_db, "ZSMPB", "anomaly")
    data = blocked_output(b["origin_evidence_id"])
    mutate(data)
    with pytest.raises(AstraError):
        ai.investigate(scanned_db, b["candidate_id"], settings=enabled(), client=client_with(resp(json.dumps(data))))
    assert scanned_db.execute("SELECT status FROM ai_calls ORDER BY rowid DESC LIMIT 1").fetchone()[0] == expect
    assert cands.get(scanned_db, b["candidate_id"])["state"] == "DETECTED"


def test_refusal_truncation_and_non_json_are_recorded(scanned_db):
    b = cand_by(scanned_db, "ZSMPB", "anomaly")
    for r, status in ((resp("", stop="refusal", category="cyber"), "refused"),
                      (resp('{"partial": ', stop="max_tokens"), "truncated_output"),
                      (resp("not json"), "invalid_output")):
        with pytest.raises(AstraError):
            ai.investigate(scanned_db, b["candidate_id"], settings=enabled(), client=client_with(r))
        assert scanned_db.execute("SELECT status FROM ai_calls ORDER BY rowid DESC LIMIT 1").fetchone()[0] == status
    assert cands.get(scanned_db, b["candidate_id"])["state"] == "DETECTED"


def test_daily_cap_and_input_limit(scanned_db):
    b = cand_by(scanned_db, "ZSMPB", "anomaly")
    fake = client_with()
    with pytest.raises(AstraError, match="AI_INPUT_TOO_LARGE"):
        ai.investigate(scanned_db, b["candidate_id"], settings=enabled(max_input_chars=500), client=fake)
    assert fake.beta.messages.calls == []
    ai.investigate(scanned_db, b["candidate_id"], settings=enabled(max_calls_per_day=1),
                   client=client_with(resp(json.dumps(blocked_output(b["origin_evidence_id"])))))
    with pytest.raises(ai.AIGateError, match="AI_DAILY_LIMIT"):
        ai.investigate(scanned_db, b["candidate_id"], settings=enabled(max_calls_per_day=1), client=client_with())


def test_backlog_is_bounded_per_invocation(scanned_db):
    import re

    calls = []

    class Answering:
        """Answers for whichever candidate the adapter asks about, citing its own frozen evidence."""

        def create(self, **kw):
            calls.append(kw)
            cid = re.search(r'"candidate_id":"(cand_[0-9a-f]+)"', kw["messages"][0]["content"]).group(1)
            ev = scanned_db.execute("SELECT origin_evidence_id FROM candidates WHERE candidate_id=?", (cid,)).fetchone()[0]
            return resp(json.dumps(blocked_output(ev)))

    fake = SimpleNamespace(beta=SimpleNamespace(messages=Answering()))
    out = ai.run_backlog(scanned_db, settings=enabled(max_calls_per_run=1), client=fake)
    assert out["claimed"] == 1 and len(calls) == 1 and out["results"][0]["ok"] is True


def test_official_sdk_request_shape_and_retry_with_mock_transport(scanned_db):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx2.Response(429, headers={"retry-after": "0"}, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}})
        body = {"id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": json.dumps(blocked_output(b["origin_evidence_id"]))}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 42}}
        return httpx2.Response(200, json=body)

    client = anthropic.Anthropic(api_key="sk-test-not-real", max_retries=1,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    b = cand_by(scanned_db, "ZSMPB", "anomaly")
    out = ai.investigate(scanned_db, b["candidate_id"], settings=enabled(), client=client)
    assert out["state"] == "BLOCKED" and out["output_tokens"] == 42
    assert len(seen) == 2  # one retry after 429
    req = seen[-1]
    assert req.url.path == "/v1/messages"
    assert "server-side-fallback-2026-07-01" in req.headers.get("anthropic-beta", "")
    body = json.loads(req.content)
    assert body["fallbacks"] == "default" and body["model"] == "claude-opus-5-5" and body["output_config"] == {"effort": "high"}
