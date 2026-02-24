from openai import OpenAI

from .config import get_settings
from .schemas import DraftResult, TriageResult


settings = get_settings()


def _get_client() -> OpenAI:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=settings.openai_api_key)


def run_drafting_agent(
    *, ticket_body: str, triage: TriageResult | None, previous_thread: str | None = None
) -> DraftResult:
    """
    Generate a draft reply for a ticket, grounding on triage metadata.
    """
    client = _get_client()

    system_prompt = (
        "You are an expert customer support assistant. "
        "Draft a clear, empathetic reply to the customer message. "
        "Do not promise actions that the system cannot perform. "
        "Return only the reply text, no extra commentary."
    )

    user_parts = [
        "Customer message:\n",
        ticket_body,
        "\n\nTriage metadata (may be empty):\n",
        triage.model_dump_json(indent=2) if triage else "{}",
    ]
    if previous_thread:
        user_parts.append("\n\nConversation history:\n")
        user_parts.append(previous_thread)

    response = client.chat.completions.create(
        model=settings.openai_model_drafting,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "".join(user_parts)},
        ],
    )

    content = response.choices[0].message.content or ""
    return DraftResult(draft_reply=content.strip())

