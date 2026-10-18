import pytest
from pydantic import ValidationError

from app.routing import normalize_category, route_ticket
from app.schemas import CATEGORIES, TriageResult


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
        ("technical", "technical_support"),
        ("account", "technical_support"),
        ("sales", "sales"),
        ("general", "general"),
    ],
)
def test_category_maps_to_team(category, team):
    assert route_ticket(triage=_triage(category=category)).assigned_team == team


def test_every_category_has_a_team():
    for category in CATEGORIES:
        route_ticket(triage=_triage(category=category))


@pytest.mark.parametrize(
    ("priority", "sla"),
    [("urgent", "rush"), ("high", "rush"), ("medium", "standard"), ("low", "low")],
)
def test_priority_maps_to_sla(priority, sla):
    assert route_ticket(triage=_triage(priority=priority)).sla_bucket == sla


def test_free_text_category_is_rejected_by_schema():
    with pytest.raises(ValidationError):
        _triage(category="Payment issue")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("billing", "billing"),
        (" Account ", "account"),
        ("Payment issue", "billing"),
        ("bug report", "technical"),
        ("login error", "technical"),
        ("pricing question", "sales"),
        ("feedback", "general"),
        ("", "general"),
        (None, "general"),
    ],
)
def test_normalize_legacy_categories(raw, expected):
    assert normalize_category(raw) == expected
