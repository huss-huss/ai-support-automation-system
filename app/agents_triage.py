import json
from typing import Any

from openai import OpenAI

from .config import get_settings
from .schemas import TriageResult


def _get_client() -> OpenAI:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=settings.openai_api_key)


def _strict_schema(schema: dict) -> dict:
    """
    Recursively add additionalProperties: false and required to every object
    in the schema, as required by OpenAI strict structured output.
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


def run_triage_agent(ticket: dict[str, Any], *, client: OpenAI | None = None) -> TriageResult:
    """
    Call OpenAI to classify a ticket and return a validated TriageResult.

    ``client`` can be injected (tests pass a fake); by default one is built
    from settings.
    """
    client = client or _get_client()

    schema = _strict_schema(TriageResult.model_json_schema())

    system_prompt = (
        "You are a customer support triage system. "
        "Classify the ticket into priority, category, sentiment, "
        "whether it needs a human agent, and provide a short summary. "
        "You must respond ONLY with JSON matching the provided JSON schema. "
        "Do not include any extra keys or text."
    )

    user_content = (
        "Classify this ticket:\n\n"
        f"Subject: {ticket.get('subject')}\n\n"
        f"Body:\n{ticket.get('body')}\n\n"
        f"Channel: {ticket.get('channel')}\n"
        f"Customer ID: {ticket.get('customer_id')}"
    )

    response = client.chat.completions.create(
        model=get_settings().openai_model_triage,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "triage_result",
                "schema": schema,
                "strict": True,
            },
        },
    )

    content = response.choices[0].message.content
    data = json.loads(content)
    return TriageResult.model_validate(data)

