import json
from types import SimpleNamespace
from typing import Any

DEFAULT_TRIAGE = {
    "priority": "high",
    "category": "billing",
    "sentiment": "negative",
    "needs_human": False,
    "summary": "Customer was charged twice.",
}


class _Completions:
    def __init__(self, parent: "FakeOpenAIClient") -> None:
        self._parent = parent

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self._parent.calls.append(kwargs)
        if self._parent.error is not None:
            raise self._parent.error
        if "response_format" in kwargs:
            content = self._parent.triage_content
        else:
            content = self._parent.draft_content
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=40),
        )


class FakeOpenAIClient:
    """Minimal stand-in for ``openai.OpenAI`` covering chat.completions.create."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.triage_content = json.dumps(DEFAULT_TRIAGE)
        self.draft_content = "  Hi, sorry about the double charge. We are looking into it.  "
        self.chat = SimpleNamespace(completions=_Completions(self))


class ScriptedProvider:
    """
    Wraps the deterministic fake provider and raises queued errors first.

    ``failures`` maps a purpose ("triage"/"drafting") to a list of exceptions
    raised on successive calls before falling through to real fake output.
    ``always_fail`` raises the given error on every call for that purpose.
    """

    name = "scripted"

    def __init__(self, failures=None, always_fail=None):
        from app.llm.fake import FakeLLMProvider

        self._inner = FakeLLMProvider()
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.always_fail = always_fail or {}
        self.calls: list[str] = []

    def complete(self, request):
        self.calls.append(request.purpose)
        if request.purpose in self.always_fail:
            raise self.always_fail[request.purpose]
        queued = self.failures.get(request.purpose)
        if queued:
            raise queued.pop(0)
        return self._inner.complete(request)
