import pytest

from app.routing import route_ticket
from app.schemas import TriageResult


def _triage(category: str = "general", priority: str = "medium") -> TriageResult:
    return TriageResult(
        priority=priority,
        category=category,
        sentiment="neutral",
        needs_human=False,
        summary="s",
    )


def test_no_triage_falls_back_to_general_standard():
    decision = route_ticket(triage=None)
    assert decision.assigned_team == "general"
    assert decision.sla_bucket == "standard"


@pytest.mark.parametrize(
    ("category", "team"),
    [
        ("billing", "billing"),
        ("Payment issue", "billing"),
        ("technical", "technical_support"),
        ("bug report", "technical_support"),
        ("login error", "technical_support"),
        ("sales", "sales"),
        ("pricing question", "sales"),
        ("feedback", "general"),
        ("", "general"),
    ],
)
def test_category_maps_to_team(category, team):
    assert route_ticket(triage=_triage(category=category)).assigned_team == team


@pytest.mark.parametrize(
    ("priority", "sla"),
    [("urgent", "rush"), ("high", "rush"), ("medium", "standard"), ("low", "low")],
)
def test_priority_maps_to_sla(priority, sla):
    assert route_ticket(triage=_triage(priority=priority)).sla_bucket == sla
