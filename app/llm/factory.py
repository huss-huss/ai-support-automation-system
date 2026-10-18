import logging
from functools import lru_cache

from ..config import Settings, get_settings
from .base import LLMProvider
from .fake import FakeLLMProvider

logger = logging.getLogger(__name__)

PROVIDERS = ("auto", "openai", "anthropic", "fake")


def resolve_provider_name(settings: Settings) -> str:
    name = settings.llm_provider.lower()
    if name not in PROVIDERS:
        raise ValueError(f"LLM_PROVIDER must be one of {PROVIDERS}, got {name!r}")
    if name != "auto":
        return name
    if settings.openai_api_key:
        return "openai"
    if settings.anthropic_api_key:
        return "anthropic"
    if settings.environment == "production":
        raise RuntimeError(
            "No LLM API key configured in production; set LLM_PROVIDER explicitly "
            "(use LLM_PROVIDER=fake only if you really want canned output)"
        )
    return "fake"


def build_provider(settings: Settings) -> LLMProvider:
    name = resolve_provider_name(settings)

    if name == "openai":
        import openai

        from .openai_provider import OpenAIProvider

        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        client = openai.OpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_sdk_max_retries,
        )
        return OpenAIProvider(
            client,
            models={
                "triage": settings.openai_model_triage,
                "drafting": settings.openai_model_drafting,
            },
        )

    if name == "anthropic":
        import anthropic

        from .anthropic_provider import AnthropicProvider

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_sdk_max_retries,
        )
        return AnthropicProvider(
            client,
            models={
                "triage": settings.anthropic_model_triage,
                "drafting": settings.anthropic_model_drafting,
            },
            effort=settings.anthropic_effort,
            refusal_fallback=settings.anthropic_refusal_fallback,
        )

    logger.warning("Using the deterministic fake LLM provider; outputs are canned")
    return FakeLLMProvider()


@lru_cache
def get_llm_provider() -> LLMProvider:
    """Process-wide provider built from settings (clients keep connection pools)."""
    return build_provider(get_settings())
