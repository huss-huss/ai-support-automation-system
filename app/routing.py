from .schemas import RoutingDecision, TriageResult


def route_ticket(*, triage: TriageResult | None) -> RoutingDecision:
    """
    Simple rules-based routing module.

    Extend this with organization-specific rules (e.g. keyword matching,
    customer tiers, business hours).
    """
    if triage is None:
        return RoutingDecision(assigned_team="general", sla_bucket="standard")

    category = (triage.category or "").lower()
    priority = triage.priority

    if "billing" in category or "payment" in category:
        team = "billing"
    elif "technical" in category or "bug" in category or "error" in category:
        team = "technical_support"
    elif "sales" in category or "pricing" in category:
        team = "sales"
    else:
        team = "general"

    if priority in ("urgent", "high"):
        sla = "rush"
    elif priority == "medium":
        sla = "standard"
    else:
        sla = "low"

    return RoutingDecision(assigned_team=team, sla_bucket=sla)

