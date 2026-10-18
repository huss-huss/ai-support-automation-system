import json

import pytest

from app.agents_drafting import run_drafting_agent
from app.agents_triage import run_triage_agent
from app.llm import LLMError
from app.llm.fake import FakeLLMProvider
from app.llm.openai_provider import OpenAIProvider
from app.schemas import TriageResult
from tests.fakes import DEFAULT_TRIAGE, FakeOpenAIClient

TICKET = {
    "subject": "Charged twice",
    "body": "I was billed two times this month.",
    "channel": "email",
    "customer_id": "c-1",
}


@pytest.fixture
def client():
    return FakeOpenAIClient()


@pytest.fixture
def provider(client):
    return OpenAIProvider(client, models={"triage": "m-triage", "drafting": "m-draft"})


def test_triage_parses_structured_output(provider):
    result, usage = run_triage_agent(TICKET, provider=provider)
    assert result == TriageResult(**DEFAULT_TRIAGE)
    assert (usage.provider, usage.model) == ("openai", "m-triage")
    assert (usage.input_tokens, usage.output_tokens) == (120, 40)


def test_triage_requests_strict_json_schema(client, provider):
    run_triage_agent(TICKET, provider=provider)
    (call,) = client.calls
    assert call["model"] == "m-triage"
    fmt = call["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    user_msg = call["messages"][1]["content"]
    assert "Charged twice" in user_msg and "billed two times" in user_msg


def test_triage_rejects_output_that_violates_schema(client, provider):
    client.triage_content = json.dumps({**DEFAULT_TRIAGE, "priority": "p0"})
    with pytest.raises(LLMError, match="validation"):
        run_triage_agent(TICKET, provider=provider)


def test_triage_rejects_non_json_output(client, provider):
    client.triage_content = "not json"
    with pytest.raises(LLMError, match="validation"):
        run_triage_agent(TICKET, provider=provider)


def test_drafting_strips_and_includes_triage_context(client, provider):
    triage = TriageResult(**DEFAULT_TRIAGE)
    draft, usage = run_drafting_agent(ticket_body=TICKET["body"], triage=triage, provider=provider)
    assert draft.draft_reply.startswith("Hi, sorry")
    assert not draft.draft_reply.endswith(" ")
    assert usage.model == "m-draft"
    call = client.calls[0]
    assert "response_format" not in call
    assert '"category": "billing"' in call["messages"][1]["content"]


def test_agents_work_end_to_end_with_fake_provider():
    provider = FakeLLMProvider()
    triage, _ = run_triage_agent(TICKET, provider=provider)
    assert triage.category == "billing"
    draft, _ = run_drafting_agent(ticket_body=TICKET["body"], triage=triage, provider=provider)
    assert draft.draft_reply
