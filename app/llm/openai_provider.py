import time

import openai

from .base import (
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    TransientLLMError,
    is_retryable_status,
)


class OpenAIProvider:
    """Chat Completions with strict JSON-schema structured output."""

    name = "openai"

    def __init__(self, client: openai.OpenAI, *, models: dict[str, str]) -> None:
        self._client = client
        self._models = models

    def complete(self, request: LLMRequest) -> LLMResponse:
        model = self._models[request.purpose]
        kwargs = {}
        if request.json_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": request.json_schema,
                    "strict": True,
                },
            }

        started = time.monotonic()
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                **kwargs,
            )
        except openai.APIConnectionError as exc:  # includes APITimeoutError
            raise TransientLLMError(f"openai connection error: {exc}") from exc
        except openai.APIStatusError as exc:
            error_cls = TransientLLMError if is_retryable_status(exc.status_code) else LLMError
            raise error_cls(f"openai HTTP {exc.status_code}: {exc.message}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        message = response.choices[0].message
        if getattr(message, "refusal", None):
            raise LLMError(f"openai refused the request: {message.refusal}")

        usage = response.usage
        return LLMResponse(
            text=message.content or "",
            usage=LLMUsage(
                provider=self.name,
                model=getattr(response, "model", None) or model,
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                latency_ms=latency_ms,
            ),
        )
