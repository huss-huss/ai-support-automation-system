import json
from types import SimpleNamespace

import anthropic
import httpx
import httpx2
import openai
import pytest

from app.agents_triage import TRIAGE_SCHEMA, build_triage_request
from app.config import Settings
from app.llm import LLMError, LLMRequest, TransientLLMError
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.factory import build_provider, resolve_provider_name
from app.llm.fake import FakeLLMProvider
from app.llm.openai_provider import OpenAIProvider
from app.schemas import TriageResult
from tests.fakes import FakeOpenAIClient

TICKET = {"subject": "Site is down", "body": "Our production dashboard is down. Urgent!"}
TEXT_REQUEST = LLMRequest(purpose="drafting", system="s", user="u")


# --- OpenAI -----------------------------------------------------------------

_oa_request = httpx.Request("POST", "https://api.openai.test/v1/chat/completions")


def _openai_status_error(cls, status):
    return cls("err", response=httpx.Response(status, request=_oa_request), body=None)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_openai_status_error(openai.RateLimitError, 429), TransientLLMError),
        (_openai_status_error(openai.InternalServerError, 503), TransientLLMError),
        (openai.APITimeoutError(request=_oa_request), TransientLLMError),
        (openai.APIConnectionError(request=_oa_request), TransientLLMError),
        (_openai_status_error(openai.BadRequestError, 400), LLMError),
        (_openai_status_error(openai.AuthenticationError, 401), LLMError),
    ],
)
def test_openai_error_classification(error, expected):
    client = FakeOpenAIClient()
    client.error = error
    provider = OpenAIProvider(client, models={"drafting": "m"})
    with pytest.raises(expected) as info:
        provider.complete(TEXT_REQUEST)
    if expected is LLMError:
        assert not isinstance(info.value, TransientLLMError)


# --- Anthropic --------------------------------------------------------------


class FakeAnthropicClient:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response
        self.error = error
        self.messages = SimpleNamespace(create=self._create("stable"))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create("beta")))

    def _create(self, endpoint):
        def create(**kwargs):
            self.calls.append((endpoint, kwargs))
            if self.error is not None:
                raise self.error
            return self.response

        return create


def _anthropic_message(text, *, stop_reason="end_turn", model="claude-opus-5-5"):
    return SimpleNamespace(
        model=model,
        stop_reason=stop_reason,
        stop_details=None,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(
            input_tokens=100,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=20,
            output_tokens=30,
        ),
    )


def _anthropic_provider(client, **kwargs):
    models = {"triage": "claude-opus-5-5", "drafting": "claude-opus-5-5"}
    return AnthropicProvider(client, models=models, **kwargs)


def test_anthropic_structured_request_and_parsing():
    payload = {
        "priority": "urgent",
        "category": "technical",
        "sentiment": "negative",
        "needs_human": True,
        "summary": "Production dashboard down.",
    }
    client = FakeAnthropicClient(_anthropic_message(json.dumps(payload)))
    response = _anthropic_provider(client).complete(build_triage_request(TICKET))

    assert TriageResult.model_validate_json(response.text).priority == "urgent"
    assert response.usage.input_tokens == 120  # uncached + cache reads
    assert response.usage.output_tokens == 30

    endpoint, kwargs = client.calls[0]
    assert endpoint == "beta"
    assert kwargs["fallbacks"] == "default"
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["output_config"]["effort"] == "low"
    assert kwargs["output_config"]["format"] == {"type": "json_schema", "schema": TRIAGE_SCHEMA}
    assert "thinking" not in kwargs


def test_anthropic_without_fallback_uses_stable_endpoint():
    client = FakeAnthropicClient(_anthropic_message("hello"))
    response = _anthropic_provider(client, refusal_fallback=False).complete(TEXT_REQUEST)
    endpoint, kwargs = client.calls[0]
    assert endpoint == "stable"
    assert "fallbacks" not in kwargs and "format" not in kwargs["output_config"]
    assert response.text == "hello"


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_anthropic_unusable_stop_reasons_are_permanent(stop_reason):
    client = FakeAnthropicClient(_anthropic_message("", stop_reason=stop_reason))
    with pytest.raises(LLMError) as info:
        _anthropic_provider(client).complete(TEXT_REQUEST)
    assert not isinstance(info.value, TransientLLMError)


_an_request = httpx2.Request("POST", "https://api.anthropic.test/v1/messages")


def _anthropic_status_error(cls, status):
    return cls("err", response=httpx2.Response(status, request=_an_request), body=None)


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (_anthropic_status_error(anthropic.RateLimitError, 429), True),
        (_anthropic_status_error(anthropic.OverloadedError, 529), True),
        (_anthropic_status_error(anthropic.InternalServerError, 500), True),
        (anthropic.APITimeoutError(request=_an_request), True),
        (_anthropic_status_error(anthropic.BadRequestError, 400), False),
        (_anthropic_status_error(anthropic.NotFoundError, 404), False),
    ],
)
def test_anthropic_error_classification(error, transient):
    client = FakeAnthropicClient(error=error)
    with pytest.raises(LLMError) as info:
        _anthropic_provider(client).complete(TEXT_REQUEST)
    assert isinstance(info.value, TransientLLMError) is transient


# --- Fake provider ----------------------------------------------------------


def test_fake_provider_is_deterministic_and_schema_valid():
    provider = FakeLLMProvider()
    first = provider.complete(build_triage_request(TICKET))
    second = provider.complete(build_triage_request(TICKET))
    assert first == second
    result = TriageResult.model_validate_json(first.text)
    assert result.priority == "urgent"
    assert result.needs_human is True
    assert first.usage.provider == "fake"


def test_fake_provider_rejects_unknown_schema():
    request = LLMRequest(purpose="x", system="", user="", json_schema={}, schema_name="other")
    with pytest.raises(LLMError):
        FakeLLMProvider().complete(request)


# --- Factory ----------------------------------------------------------------


def _settings(**overrides) -> Settings:
    base = dict(llm_provider="auto", openai_api_key=None, anthropic_api_key=None)
    return Settings(**{**base, **overrides})


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "fake"),
        ({"openai_api_key": "k"}, "openai"),
        ({"anthropic_api_key": "k"}, "anthropic"),
        ({"openai_api_key": "k", "anthropic_api_key": "k"}, "openai"),
        ({"llm_provider": "anthropic", "openai_api_key": "k"}, "anthropic"),
        ({"llm_provider": "FAKE", "openai_api_key": "k"}, "fake"),
    ],
)
def test_provider_resolution(overrides, expected):
    assert resolve_provider_name(_settings(**overrides)) == expected


def test_auto_refuses_fake_in_production():
    with pytest.raises(RuntimeError, match="production"):
        resolve_provider_name(_settings(environment="production"))


def test_unknown_provider_name():
    with pytest.raises(ValueError):
        resolve_provider_name(_settings(llm_provider="llama"))


@pytest.mark.parametrize("name", ["openai", "anthropic"])
def test_explicit_provider_requires_key(name):
    with pytest.raises(RuntimeError, match="API_KEY"):
        build_provider(_settings(llm_provider=name))


def test_build_real_providers_with_keys():
    assert build_provider(_settings(llm_provider="openai", openai_api_key="k")).name == "openai"
    assert (
        build_provider(_settings(llm_provider="anthropic", anthropic_api_key="k")).name
        == "anthropic"
    )
