"""
Triage evaluation harness.

Runs the triage agent over a labeled set of synthetic tickets and reports
per-field accuracy (priority, category, needs_human), confusion matrices,
needs_human precision/recall, and token/cost totals.

    python -m evals.run_eval                      # offline fake provider
    python -m evals.run_eval --provider openai    # needs OPENAI_API_KEY
    python -m evals.run_eval --provider anthropic --json report.json

Real providers make one LLM call per ticket and cost real money (the report
prints the estimated total).
"""

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from app.agents_triage import run_triage_agent
from app.config import get_settings
from app.llm import LLMError, LLMProvider, build_provider
from app.llm.pricing import estimate_cost_usd
from app.schemas import CATEGORIES

DEFAULT_DATASET = Path(__file__).with_name("tickets.jsonl")
FIELDS = ("priority", "category", "needs_human")
PRIORITIES = ("low", "medium", "high", "urgent")
LABELS = {"priority": PRIORITIES, "category": CATEGORIES, "needs_human": (False, True)}


def load_dataset(path: Path = DEFAULT_DATASET) -> list[dict[str, Any]]:
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def _confusion(pairs: list[tuple[Any, Any]], labels) -> dict[str, dict[str, int]]:
    """matrix[expected][predicted] = count; predictions outside labels go to 'error'."""
    columns = [str(label) for label in labels] + ["error"]
    matrix = {str(e): {c: 0 for c in columns} for e in labels}
    for expected, predicted in pairs:
        column = str(predicted) if predicted in labels else "error"
        matrix[str(expected)][column] += 1
    return matrix


def run_eval(provider: LLMProvider, dataset: list[dict[str, Any]]) -> dict[str, Any]:
    pairs: dict[str, list[tuple[Any, Any]]] = {f: [] for f in FIELDS}
    mismatches: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    input_tokens = output_tokens = 0
    costs: list[float | None] = []
    latencies: list[int] = []
    overrides = get_settings().llm_price_overrides

    for item in dataset:
        expected = item["expected"]
        try:
            result, usage = run_triage_agent(item, provider=provider)
        except LLMError as exc:
            errors.append({"id": item["id"], "error": f"{type(exc).__name__}: {exc}"})
            for field in FIELDS:
                pairs[field].append((expected[field], None))
            continue

        input_tokens += usage.input_tokens
        output_tokens += usage.output_tokens
        costs.append(estimate_cost_usd(usage, overrides))
        latencies.append(usage.latency_ms)

        predicted = result.model_dump()
        wrong = {f: (expected[f], predicted[f]) for f in FIELDS if expected[f] != predicted[f]}
        for field in FIELDS:
            pairs[field].append((expected[field], predicted[field]))
        if wrong:
            mismatches.append({"id": item["id"], "subject": item["subject"], "wrong": wrong})

    n = len(dataset)
    accuracy = {f: sum(e == p for e, p in pairs[f]) / n for f in FIELDS}
    exact = sum(
        all(pairs[f][i][0] == pairs[f][i][1] for f in FIELDS) for i in range(n)
    ) / n

    nh = Counter((e, p) for e, p in pairs["needs_human"])
    tp, fp, fn = nh[(True, True)], nh[(False, True)], nh[(True, None)] + nh[(True, False)]
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None

    known_costs = [c for c in costs if c is not None]
    return {
        "provider": provider.name,
        "n": n,
        "errors": errors,
        "accuracy": accuracy,
        "exact_match": exact,
        "needs_human": {"precision": precision, "recall": recall, "tp": tp, "fp": fp, "fn": fn},
        "confusion": {f: _confusion(pairs[f], LABELS[f]) for f in ("priority", "category")},
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": round(sum(known_costs), 6) if known_costs else None,
            "unpriced_calls": len(costs) - len(known_costs),
            "latency_ms_p50": statistics.median(latencies) if latencies else None,
            "latency_ms_max": max(latencies) if latencies else None,
        },
        "mismatches": mismatches,
    }


def _fmt_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def format_report(report: dict[str, Any]) -> str:
    lines = [f"Triage eval: provider={report['provider']} tickets={report['n']}", ""]
    lines.append("Accuracy")
    for field, value in report["accuracy"].items():
        lines.append(f"  {field:<12} {value:6.1%}")
    lines.append(f"  {'all fields':<12} {report['exact_match']:6.1%}")
    nh = report["needs_human"]
    lines.append(
        f"  needs_human precision={_fmt_ratio(nh['precision'])} "
        f"recall={_fmt_ratio(nh['recall'])} (tp={nh['tp']} fp={nh['fp']} fn={nh['fn']})"
    )

    for field, matrix in report["confusion"].items():
        columns = list(next(iter(matrix.values())).keys())
        width = max(len(c) for c in [*columns, *matrix]) + 1
        lines += ["", f"Confusion: {field} (rows = expected, columns = predicted)"]
        lines.append(" " * width + "".join(c.rjust(width) for c in columns))
        for expected, row in matrix.items():
            lines.append(expected.ljust(width) + "".join(str(row[c]).rjust(width) for c in columns))

    usage = report["usage"]
    cost = usage["estimated_cost_usd"]
    lines += [
        "",
        f"Usage: input_tokens={usage['input_tokens']} output_tokens={usage['output_tokens']} "
        f"estimated_cost_usd={'n/a' if cost is None else f'{cost:.6f}'} "
        f"latency_ms p50={usage['latency_ms_p50']} max={usage['latency_ms_max']}",
    ]
    if report["errors"]:
        lines += ["", f"Errors ({len(report['errors'])})"]
        lines += [f"  {e['id']}: {e['error']}" for e in report["errors"]]
    if report["mismatches"]:
        lines += ["", f"Mismatches ({len(report['mismatches'])})"]
        for m in report["mismatches"]:
            wrong = ", ".join(f"{f} {e}->{p}" for f, (e, p) in m["wrong"].items())
            lines.append(f"  {m['id']} {m['subject'][:40]:<40} {wrong}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--provider", default="fake", choices=["fake", "openai", "anthropic"])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--json", type=Path, help="also write the full report as JSON")
    args = parser.parse_args(argv)

    settings = get_settings().model_copy(update={"llm_provider": args.provider})
    report = run_eval(build_provider(settings), load_dataset(args.dataset))
    print(format_report(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
