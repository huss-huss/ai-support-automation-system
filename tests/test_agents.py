import json

import pytest
from pydantic import ValidationError

from app.agents_drafting import run_drafting_agent
from app.agents_triage import run_triage_agent
from app.schemas import TriageResult
from tests.fakes import DEFAULT_TRIAGE, FakeOpenAIClient

TICKET = {
    "subject": "Charged twice",
    "body": "I was billed two times this month.",
    "channel": "email",
    "customer_id": "c-1",
}


def test_triage_parses_structured_output():
    client = FakeOpenAIClient()
    result = run_triage_agent(TICKET, client=client)
    assert result == TriageResult(**DEFAULT_TRIAGE)


def test_triage_requests_strict_json_schema():
    client = FakeOpenAIClient()
    run_triage_agent(TICKET, client=client)
    (call,) = client.calls
    fmt = call["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    user_msg = call["messages"][1]["content"]
    assert "Charged twice" in user_msg and "billed two times" in user_msg


def test_triage_rejects_output_that_violates_schema():
    client = FakeOpenAIClient()
    client.triage_content = json.dumps({**DEFAULT_TRIAGE, "priority": "p0"})
    with pytest.raises(ValidationError):
        run_triage_agent(TICKET, client=client)


def test_triage_rejects_non_json_output():
    client = FakeOpenAIClient()
    client.triage_content = "not json"
    with pytest.raises(json.JSONDecodeError):
        run_triage_agent(TICKET, client=client)


def test_drafting_strips_and_includes_triage_context():
    client = FakeOpenAIClient()
    triage = TriageResult(**DEFAULT_TRIAGE)
    draft = run_drafting_agent(ticket_body=TICKET["body"], triage=triage, client=client)
    assert draft.draft_reply.startswith("Hi, sorry")
    assert not draft.draft_reply.endswith(" ")
    user_msg = client.calls[0]["messages"][1]["content"]
    assert '"category": "billing"' in user_msg


def test_missing_api_key_raises_clear_error():
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        run_triage_agent(TICKET)
