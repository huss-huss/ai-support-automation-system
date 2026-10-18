import json

import pytest

from app.llm import LLMError, LLMResponse, LLMUsage
from app.llm.fake import FakeLLMProvider
from app.schemas import CATEGORIES
from evals.run_eval import format_report, load_dataset, main, run_eval

DATASET = load_dataset()


class OracleProvider:
    """Answers with the gold labels, to check the harness scores perfection as 1.0."""

    name = "oracle"

    def __init__(self, dataset):
        self._by_subject = {item["subject"]: item["expected"] for item in dataset}

    def complete(self, request):
        subject = request.user.split("Subject: ", 1)[1].split("\n", 1)[0]
        labels = self._by_subject[subject]
        text = json.dumps({**labels, "sentiment": "neutral", "summary": "s"})
        return LLMResponse(text=text, usage=LLMUsage("oracle", "unknown-model", 10, 5, 1))


class BrokenProvider:
    name = "broken"

    def complete(self, request):
        raise LLMError("refused")


def test_dataset_is_well_formed():
    assert len(DATASET) >= 25
    assert len({item["id"] for item in DATASET}) == len(DATASET)
    for item in DATASET:
        expected = item["expected"]
        assert set(expected) == {"priority", "category", "needs_human"}
        assert expected["priority"] in ("low", "medium", "high", "urgent")
        assert expected["category"] in CATEGORIES
        assert isinstance(expected["needs_human"], bool)
        assert item["subject"] and item["body"]
    # every category and priority is represented
    assert {i["expected"]["category"] for i in DATASET} == set(CATEGORIES)
    assert {i["expected"]["priority"] for i in DATASET} == {"low", "medium", "high", "urgent"}


def test_oracle_scores_perfectly():
    report = run_eval(OracleProvider(DATASET), DATASET)
    assert report["accuracy"] == {"priority": 1.0, "category": 1.0, "needs_human": 1.0}
    assert report["exact_match"] == 1.0
    assert report["needs_human"]["precision"] == report["needs_human"]["recall"] == 1.0
    assert report["mismatches"] == []
    # unknown model: tokens are counted but cost is reported as unknown, not zero
    assert report["usage"]["estimated_cost_usd"] is None
    assert report["usage"]["unpriced_calls"] == len(DATASET)


def test_provider_errors_count_as_wrong():
    report = run_eval(BrokenProvider(), DATASET[:3])
    assert len(report["errors"]) == 3
    assert report["accuracy"]["priority"] == 0.0
    assert sum(row["error"] for row in report["confusion"]["priority"].values()) == 3


def test_fake_provider_baseline():
    """
    The keyword heuristic is a baseline that keeps the harness honest in CI.
    If you change the heuristic or the dataset, update these numbers and the
    README together.
    """
    report = run_eval(FakeLLMProvider(), DATASET)
    n = report["n"]
    for matrix in report["confusion"].values():
        assert sum(sum(row.values()) for row in matrix.values()) == n

    assert report["accuracy"]["priority"] == pytest.approx(20 / 34)
    assert report["accuracy"]["category"] == pytest.approx(27 / 34)
    assert report["accuracy"]["needs_human"] == pytest.approx(26 / 34)
    assert report["exact_match"] == pytest.approx(13 / 34)
    assert report["usage"]["estimated_cost_usd"] == 0.0

    text = format_report(report)
    assert "Confusion: priority" in text and "Mismatches" in text


def test_cli_writes_json_report(tmp_path, capsys):
    out = tmp_path / "report.json"
    assert main(["--provider", "fake", "--json", str(out)]) == 0
    assert "Triage eval: provider=fake" in capsys.readouterr().out
    assert json.loads(out.read_text())["n"] == len(DATASET)
