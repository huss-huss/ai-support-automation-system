"""
Deterministic, offline LLM stand-in.

Used by the test suite, the eval harness in CI, and local runs without an API
key. Triage is a transparent keyword heuristic, so its eval scores are a
baseline for the harness rather than a claim about model quality.
"""

import json
import re

from .base import LLMError, LLMRequest, LLMResponse, LLMUsage

MODEL_NAME = "fake-heuristic-v1"

_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("billing", ("charge", "refund", "invoice", "payment", "billing", "billed",
                 "subscription", "receipt", "credit card", "vat")),
    ("technical", ("error", "bug", "crash", "not working", "broken", "fails", "failing",
                   "timeout", "api", "outage", "down", "slow", "sync", "500")),
    ("account", ("password", "log in", "login", "sign in", "2fa", "two-factor",
                 "locked out", "delete my account", "email address", "username")),
    ("sales", ("pricing", "quote", "enterprise plan", "discount", "demo", "upgrade to",
               "purchase", "seats", "licen")),
]

_URGENT = ("urgent", "asap", "immediately", "outage", "is down", "data loss", "breach",
           "hacked", "security", "production")
_HIGH = ("cannot", "can't", "unable", "blocked", "charged twice", "double charged",
         "locked out", "not working", "error")
_LOW = ("question", "wondering", "feature request", "suggestion", "curious", "how do i",
        "just wanted", "feedback")

_NEGATIVE = ("angry", "frustrated", "unacceptable", "terrible", "worst", "disappointed",
             "annoyed", "ridiculous", "furious", "fed up")
_POSITIVE = ("thank", "love", "great", "awesome", "appreciate", "fantastic")

_ESCALATE = ("lawyer", "legal", "chargeback", "speak to a human", "manager", "cancel my",
             "breach", "hacked", "gdpr")


def _has(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def _extract(user: str, label: str) -> str:
    match = re.search(rf"{label}:\s*(.*?)(?:\n\n|$)", user, flags=re.S)
    return match.group(1).strip() if match else ""


def heuristic_triage(text: str) -> dict:
    t = text.lower()
    category = next((c for c, kws in _CATEGORY_KEYWORDS if _has(t, kws)), "general")

    if _has(t, _URGENT):
        priority = "urgent"
    elif _has(t, _HIGH):
        priority = "high"
    elif _has(t, _LOW):
        priority = "low"
    else:
        priority = "medium"

    if _has(t, _NEGATIVE):
        sentiment = "negative"
    elif _has(t, _POSITIVE):
        sentiment = "positive"
    else:
        sentiment = "neutral"

    needs_human = (
        priority == "urgent"
        or _has(t, _ESCALATE)
        or (sentiment == "negative" and priority == "high")
    )
    return {
        "priority": priority,
        "category": category,
        "sentiment": sentiment,
        "needs_human": needs_human,
    }


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class FakeLLMProvider:
    name = "fake"

    def complete(self, request: LLMRequest) -> LLMResponse:
        if request.json_schema is not None:
            if request.schema_name != "triage_result":
                raise LLMError(f"fake provider cannot produce schema {request.schema_name!r}")
            subject = _extract(request.user, "Subject")
            body = _extract(request.user, "Body")
            result = heuristic_triage(f"{subject}\n{body}")
            first_sentence = re.split(r"(?<=[.!?])\s", body or subject, maxsplit=1)[0]
            result["summary"] = first_sentence[:160]
            text = json.dumps(result)
        else:
            text = (
                "Hi,\n\nThanks for reaching out. We have received your message and a "
                "member of our support team is reviewing it. We will follow up as soon "
                "as we have an update.\n\nBest regards,\nSupport Team"
            )

        return LLMResponse(
            text=text,
            usage=LLMUsage(
                provider=self.name,
                model=MODEL_NAME,
                input_tokens=_approx_tokens(request.system + request.user),
                output_tokens=_approx_tokens(text),
                latency_ms=0,
            ),
        )
