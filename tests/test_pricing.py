import pytest

from app.db import AuditLog, Ticket
from app.llm import LLMUsage
from app.llm.pricing import estimate_cost_usd, usage_record
from app.tasks import process_ticket


def _usage(model, inp=1_000_000, out=1_000_000):
    return LLMUsage(provider="p", model=model, input_tokens=inp, output_tokens=out, latency_ms=5)


def test_known_model_cost():
    assert estimate_cost_usd(_usage("gpt-4o-mini")) == pytest.approx(0.75)
    assert estimate_cost_usd(_usage("claude-opus-5-5", 2_000, 500)) == pytest.approx(0.018)


def test_dated_snapshot_uses_longest_prefix():
    assert estimate_cost_usd(_usage("gpt-4o-mini-2024-07-18")) == pytest.approx(0.75)
    assert estimate_cost_usd(_usage("gpt-4o-2024-08-06")) == pytest.approx(12.5)


def test_unknown_model_has_no_cost():
    assert estimate_cost_usd(_usage("mystery-model")) is None


def test_overrides_win():
    assert estimate_cost_usd(_usage("mystery-model"), {"mystery-model": (1.0, 2.0)}) == 3.0


def test_usage_record_shape():
    record = usage_record(_usage("fake-heuristic-v1", 10, 5))
    assert record == {
        "provider": "p",
        "model": "fake-heuristic-v1",
        "input_tokens": 10,
        "output_tokens": 5,
        "latency_ms": 5,
        "cost_usd": 0.0,
    }


def test_audit_log_records_tokens_and_cost_per_llm_call(db, fake_openai, monkeypatch):
    monkeypatch.setattr(
        "app.tasks.settings.llm_price_overrides", {"m-triage": (1.0, 2.0), "m-draft": (1.0, 2.0)}
    )
    ticket = Ticket(subject="s", body="b")
    db.add(ticket)
    db.commit()

    process_ticket.delay(ticket.id).get()

    logs = {
        log.event_type: log.payload
        for log in db.query(AuditLog).filter_by(ticket_id=ticket.id)
    }
    triage_llm = logs["triage_completed"]["llm"]
    assert triage_llm["model"] == "m-triage"
    assert (triage_llm["input_tokens"], triage_llm["output_tokens"]) == (120, 40)
    assert triage_llm["cost_usd"] == pytest.approx((120 * 1.0 + 40 * 2.0) / 1e6)
    assert logs["draft_created"]["llm"]["model"] == "m-draft"
    assert "llm" not in logs["routing_completed"]
