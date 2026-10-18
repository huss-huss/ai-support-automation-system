"""
Provider-neutral LLM interface used by the triage and drafting agents.

Agents build an ``LLMRequest`` and get back an ``LLMResponse`` with the raw text
plus usage metadata. Providers translate SDK-specific failures into two error
types so the orchestration layer can decide whether a retry is worthwhile.
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMRequest:
    purpose: str  # "triage" | "drafting": selects the model and labels telemetry
    system: str
    user: str
    # When set, the provider must constrain the output to JSON matching this schema.
    json_schema: dict[str, Any] | None = None
    schema_name: str = "result"
    max_output_tokens: int = 2048


@dataclass(frozen=True)
class LLMUsage:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: LLMUsage


class LLMError(Exception):
    """The call failed in a way a retry will not fix (bad request, refusal, auth)."""


class TransientLLMError(LLMError):
    """Rate limits, timeouts, 5xx and connection failures: safe to retry later."""


class LLMProvider(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...


# 408/409/429 and any 5xx are retryable for both OpenAI and Anthropic.
_RETRYABLE_STATUS = {408, 409, 429}


def is_retryable_status(status_code: int | None) -> bool:
    return status_code is not None and (status_code in _RETRYABLE_STATUS or status_code >= 500)
