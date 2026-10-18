import time

import anthropic

from .base import (
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    TransientLLMError,
    is_retryable_status,
)

# Server-side refusal fallback: if a safety classifier declines the request, the
# API re-runs it on a fallback model chosen by refusal category within the
# same call. Requires the beta Messages endpoint.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    """Claude Messages API with JSON-schema structured output (output_config.format)."""

    name = "anthropic"

    def __init__(
        self,
        client: anthropic.Anthropic,
        *,
        models: dict[str, str],
        effort: str = "low",
        refusal_fallback: bool = True,
    ) -> None:
        self._client = client
        self._models = models
        self._effort = effort
        self._refusal_fallback = refusal_fallback

    def complete(self, request: LLMRequest) -> LLMResponse:
        model = self._models[request.purpose]
        output_config: dict = {"effort": self._effort}
        if request.json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": request.json_schema}

        params = dict(
            model=model,
            max_tokens=request.max_output_tokens,
            system=request.system,
            messages=[{"role": "user", "content": request.user}],
            output_config=output_config,
        )

        started = time.monotonic()
        try:
            if self._refusal_fallback:
                response = self._client.beta.messages.create(
                    **params, betas=[_FALLBACK_BETA], fallbacks="default"
                )
            else:
                response = self._client.messages.create(**params)
        except anthropic.APIConnectionError as exc:  # includes APITimeoutError
            raise TransientLLMError(f"anthropic connection error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            error_cls = TransientLLMError if is_retryable_status(exc.status_code) else LLMError
            raise error_cls(f"anthropic HTTP {exc.status_code}: {exc.message}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise LLMError(f"anthropic refused the request (category={category})")
        if response.stop_reason == "max_tokens":
            raise LLMError(
                f"anthropic output truncated at max_tokens={request.max_output_tokens}"
            )

        text = "".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        input_tokens = (
            (usage.input_tokens or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        )
        return LLMResponse(
            text=text,
            usage=LLMUsage(
                provider=self.name,
                # Reports the model that actually served the turn (may be a fallback).
                model=response.model or model,
                input_tokens=input_tokens,
                output_tokens=usage.output_tokens or 0,
                latency_ms=latency_ms,
            ),
        )
