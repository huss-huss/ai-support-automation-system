from .llm import LLMProvider, LLMRequest, LLMUsage, get_llm_provider
from .schemas import DraftResult, TriageResult

SYSTEM_PROMPT = (
    "You are an expert customer support assistant. "
    "Draft a clear, empathetic reply to the customer message. "
    "Do not promise actions that the system cannot perform. "
    "Return only the reply text, no extra commentary."
)


def run_drafting_agent(
    *,
    ticket_body: str,
    triage: TriageResult | None,
    previous_thread: str | None = None,
    provider: LLMProvider | None = None,
) -> tuple[DraftResult, LLMUsage]:
    """
    Generate a draft reply for a ticket, grounding on triage metadata.
    """
    provider = provider or get_llm_provider()

    user_parts = [
        "Customer message:\n",
        ticket_body,
        "\n\nTriage metadata (may be empty):\n",
        triage.model_dump_json(indent=2) if triage else "{}",
    ]
    if previous_thread:
        user_parts.append("\n\nConversation history:\n")
        user_parts.append(previous_thread)

    response = provider.complete(
        LLMRequest(
            purpose="drafting",
            system=SYSTEM_PROMPT,
            user="".join(user_parts),
            max_output_tokens=4096,
        )
    )
    return DraftResult(draft_reply=response.text.strip()), response.usage
