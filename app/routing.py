from .schemas import CATEGORIES, RoutingDecision, TriageResult

TEAM_BY_CATEGORY = {
    "billing": "billing",
    "technical": "technical_support",
    "account": "technical_support",
    "sales": "sales",
    "general": "general",
}

SLA_BY_PRIORITY = {"urgent": "rush", "high": "rush", "medium": "standard", "low": "low"}

# Free-text categories written before the taxonomy existed.
_LEGACY_KEYWORDS = [
    ("billing", ("billing", "payment")),
    ("technical", ("technical", "bug", "error")),
    ("sales", ("sales", "pricing")),
]


def normalize_category(raw: str | None) -> str:
    """Map any stored category (including legacy free text) onto the taxonomy."""
    value = (raw or "").strip().lower()
    if value in CATEGORIES:
        return value
    for category, keywords in _LEGACY_KEYWORDS:
        if any(k in value for k in keywords):
            return category
    return "general"


def route_ticket(*, triage: TriageResult | None) -> RoutingDecision:
    """
    Simple rules-based routing module.

    Extend this with organization-specific rules (e.g. keyword matching,
    customer tiers, business hours).
    """
    if triage is None:
        return RoutingDecision(assigned_team="general", sla_bucket="standard")

    return RoutingDecision(
        assigned_team=TEAM_BY_CATEGORY[triage.category],
        sla_bucket=SLA_BY_PRIORITY[triage.priority],
    )
