import json
from typing import Any

from pydantic import ValidationError

from .llm import LLMError, LLMProvider, LLMRequest, LLMUsage, get_llm_provider
from .schemas import TriageResult

SYSTEM_PROMPT = (
    "You are a customer support triage system. "
    "Classify the ticket into priority, category, sentiment, "
    "whether it needs a human agent, and provide a short summary. "
    "You must respond ONLY with JSON matching the provided JSON schema. "
    "Do not include any extra keys or text."
)


def _strict_schema(schema: dict) -> dict:
    """
    Recursively add additionalProperties: false and required to every object
    in the schema, as required by strict structured output.
    """
    schema = dict(schema)
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        props = schema.get("properties", {})
        if "required" not in schema:
            schema["required"] = list(props.keys())
        schema["properties"] = {k: _strict_schema(v) for k, v in props.items()}
    if "$defs" in schema:
        schema["$defs"] = {k: _strict_schema(v) for k, v in schema["$defs"].items()}
    return schema


TRIAGE_SCHEMA = _strict_schema(TriageResult.model_json_schema())


def build_triage_request(ticket: dict[str, Any]) -> LLMRequest:
    user_content = (
        "Classify this ticket:\n\n"
        f"Subject: {ticket.get('subject')}\n\n"
        f"Body:\n{ticket.get('body')}\n\n"
        f"Channel: {ticket.get('channel')}\n"
        f"Customer ID: {ticket.get('customer_id')}"
    )
    return LLMRequest(
        purpose="triage",
        system=SYSTEM_PROMPT,
        user=user_content,
        json_schema=TRIAGE_SCHEMA,
        schema_name="triage_result",
        max_output_tokens=2048,
    )


def run_triage_agent(
    ticket: dict[str, Any], *, provider: LLMProvider | None = None
) -> tuple[TriageResult, LLMUsage]:
    """
    Classify a ticket and return a validated TriageResult plus LLM usage.

    Output that is not valid JSON or does not match the schema raises LLMError
    (not retryable: with strict structured output this indicates a real bug).
    """
    provider = provider or get_llm_provider()
    response = provider.complete(build_triage_request(ticket))
    try:
        result = TriageResult.model_validate(json.loads(response.text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise LLMError(f"triage output failed validation: {exc}") from exc
    return result, response.usage
