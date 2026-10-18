import pytest
from pydantic import ValidationError

from app.agents_triage import _strict_schema
from app.schemas import TriageResult

VALID = {
    "priority": "low",
    "category": "billing",
    "sentiment": "neutral",
    "needs_human": False,
    "summary": "ok",
}


def _walk_objects(schema: dict):
    if schema.get("type") == "object":
        yield schema
        for prop in schema.get("properties", {}).values():
            yield from _walk_objects(prop)
    for sub in schema.get("$defs", {}).values():
        yield from _walk_objects(sub)


def test_strict_schema_closes_every_object_and_requires_all_keys():
    schema = _strict_schema(TriageResult.model_json_schema())
    objects = list(_walk_objects(schema))
    assert objects, "expected at least the root object"
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


def test_strict_schema_handles_nested_defs():
    schema = {
        "type": "object",
        "properties": {"inner": {"$ref": "#/$defs/Inner"}},
        "$defs": {"Inner": {"type": "object", "properties": {"x": {"type": "string"}}}},
    }
    out = _strict_schema(schema)
    assert out["$defs"]["Inner"]["additionalProperties"] is False
    assert out["$defs"]["Inner"]["required"] == ["x"]
    # input is not mutated
    assert "additionalProperties" not in schema["$defs"]["Inner"]


def test_triage_result_accepts_valid_payload():
    assert TriageResult.model_validate(VALID).priority == "low"


@pytest.mark.parametrize(
    "patch",
    [
        {"priority": "critical"},
        {"sentiment": "angry"},
        {"needs_human": "maybe"},
        {"unexpected": "field"},
    ],
)
def test_triage_result_rejects_invalid_payloads(patch):
    with pytest.raises(ValidationError):
        TriageResult.model_validate({**VALID, **patch})


def test_triage_result_rejects_missing_fields():
    payload = dict(VALID)
    payload.pop("summary")
    with pytest.raises(ValidationError):
        TriageResult.model_validate(payload)
