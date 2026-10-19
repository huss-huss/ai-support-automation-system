"""Prometheus metrics: values move with real work, labels stay bounded, /metrics is protected."""

import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from prometheus_client import REGISTRY
from prometheus_client.parser import text_string_to_metric_families

import app.metrics as m
import app.tasks as tasks
from app.config import get_settings
from app.db import SessionLocal, StepStatus, Ticket
from app.llm import LLMError, TransientLLMError
from app.llm.openai_provider import OpenAIProvider
from app.security import validate_metrics_settings
from tests.conftest import READ_ONLY_KEY, TEST_API_KEY
from tests.fakes import FakeOpenAIClient, ScriptedProvider

TICKET = {"subject": "Charged twice", "body": "I was billed twice for March."}
TOKEN = "metrics-token-0123456789"
NO_KEY = {"X-API-Key": ""}
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(api_client):
    return api_client


@pytest.fixture
def priced_openai(use_provider):
    """OpenAI provider on a fake client, using a model that is in the price table."""
    client = FakeOpenAIClient()
    use_provider(
        OpenAIProvider(client, models={"triage": "gpt-4o-mini", "drafting": "gpt-4o-mini"})
    )
    return client


@pytest.fixture
def scripted(use_provider):
    return lambda **kwargs: use_provider(ScriptedProvider(**kwargs))


class Snapshot:
    """Reads sample values now and reports how much they changed since."""

    def __init__(self):
        self._before = {}

    def __call__(self, name: str | tuple, **labels) -> float:
        if isinstance(name, tuple):  # a (name, labels) pair from _step/_llm
            name, labels = name
        key = (name, tuple(sorted(labels.items())))
        if key not in self._before:
            raise KeyError(f"{name}{labels} was not captured before the action")
        return _value(name, **labels) - self._before[key]

    def capture(self, name: str, **labels) -> None:
        self._before[(name, tuple(sorted(labels.items())))] = _value(name, **labels)


def _value(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _delta(*series: tuple) -> Snapshot:
    snap = Snapshot()
    for name, labels in series:
        snap.capture(name, **labels)
    return snap


def _step(step, outcome="completed"):
    return ("support_pipeline_step_duration_seconds_count", {"step": step, "outcome": outcome})


def _llm(name, purpose, model="gpt-4o-mini", provider="openai", **extra):
    return (name, {"provider": provider, "model": model, "purpose": purpose, **extra})


def _scrape(client, **headers) -> dict[str, list]:
    resp = client.get("/metrics", headers=headers or {"X-API-Key": READ_ONLY_KEY})
    assert resp.status_code == 200
    return {f.name: f.samples for f in text_string_to_metric_families(resp.text)}


# --- metrics move when a ticket is processed (eager Celery) ------------------


def test_processing_a_ticket_moves_pipeline_llm_and_ingest_metrics(client, priced_openai):
    snap = _delta(
        ("support_tickets_ingested_total", {"result": "created"}),
        _step("triage"), _step("routing"), _step("drafting"),
        ("support_tickets_processed_total", {"outcome": "completed"}),
        _llm("support_llm_calls_total", "triage"),
        _llm("support_llm_calls_total", "drafting"),
        _llm("support_llm_tokens_total", "triage", kind="input"),
        _llm("support_llm_tokens_total", "triage", kind="output"),
        _llm("support_llm_tokens_total", "drafting", kind="input"),
        _llm("support_llm_cost_usd_total", "triage"),
        _llm("support_llm_cost_usd_total", "drafting"),
        _llm("support_llm_call_duration_seconds_count", "triage"),
        ("support_http_requests_total", {"method": "POST", "route": "/tickets", "status": "201"}),
    )

    resp = client.post("/tickets", json=TICKET)
    assert resp.status_code == 201
    assert resp.json()["status"] == "completed"

    assert snap("support_tickets_ingested_total", result="created") == 1
    for step in ("triage", "routing", "drafting"):
        assert snap(_step(step)) == 1
    assert snap("support_tickets_processed_total", outcome="completed") == 1
    for purpose in ("triage", "drafting"):
        assert snap(_llm("support_llm_calls_total", purpose)) == 1
    # FakeOpenAIClient reports 120 input and 40 output tokens per call.
    assert snap(_llm("support_llm_tokens_total", "triage", kind="input")) == 120
    assert snap(_llm("support_llm_tokens_total", "triage", kind="output")) == 40
    assert snap(_llm("support_llm_tokens_total", "drafting", kind="input")) == 120
    # gpt-4o-mini: $0.15 / $0.60 per million tokens
    expected_cost = (120 * 0.15 + 40 * 0.60) / 1_000_000
    for purpose in ("triage", "drafting"):
        assert snap(_llm("support_llm_cost_usd_total", purpose)) == pytest.approx(expected_cost)
    assert snap(_llm("support_llm_call_duration_seconds_count", "triage")) == 1
    assert snap(
        "support_http_requests_total", method="POST", route="/tickets", status="201"
    ) == 1

    # /metrics serves the same series.
    families = _scrape(client)
    assert any(
        s.labels == {"step": "drafting", "outcome": "completed"}
        for s in families["support_pipeline_step_duration_seconds"]
    )
    assert "support_llm_cost_usd" in families


def test_unpriced_model_counts_the_call_but_not_the_cost(client, fake_openai):
    # fake_openai serves models "m-triage" / "m-draft", which have no price.
    unpriced = ("support_llm_unpriced_calls_total", {"provider": "openai", "model": "m-triage"})
    cost = _llm("support_llm_cost_usd_total", "triage", model="m-triage")
    calls = _llm("support_llm_calls_total", "triage", model="m-triage")
    snap = _delta(unpriced, cost, calls)

    client.post("/tickets", json=TICKET)

    assert snap(unpriced) == 1
    assert snap(calls) == 1
    assert snap(cost) == 0


def test_transient_failure_counts_a_retry_and_the_failed_step_run(db, scripted):
    scripted(failures={"drafting": [TransientLLMError("429")]})
    snap = _delta(
        ("support_pipeline_retries_total", {"step": "drafting"}),
        _step("drafting", "transient_error"),
        _step("drafting"),
        _step("triage"),
        ("support_tickets_processed_total", {"outcome": "completed"}),
    )
    ticket = Ticket(**TICKET)
    db.add(ticket)
    db.commit()

    tasks.process_ticket.delay(ticket.id).get()

    assert snap("support_pipeline_retries_total", step="drafting") == 1
    assert snap(_step("drafting", "transient_error")) == 1
    assert snap(_step("drafting")) == 1
    # Triage completed once; the retry resumed at drafting.
    assert snap(_step("triage")) == 1
    assert snap("support_tickets_processed_total", outcome="completed") == 1


def test_exhausted_retries_count_every_retry_and_one_failure(db, scripted, monkeypatch):
    monkeypatch.setattr(tasks.process_ticket, "max_retries", 2)
    scripted(always_fail={"triage": TransientLLMError("rate limited")})
    snap = _delta(
        ("support_pipeline_retries_total", {"step": "triage"}),
        _step("triage", "transient_error"),
        ("support_tickets_processed_total", {"outcome": "failed"}),
    )
    ticket = Ticket(**TICKET)
    db.add(ticket)
    db.commit()

    with pytest.raises(TransientLLMError):
        tasks.process_ticket.delay(ticket.id).get()

    assert snap("support_pipeline_retries_total", step="triage") == 2
    assert snap(_step("triage", "transient_error")) == 3
    assert snap("support_tickets_processed_total", outcome="failed") == 1


def test_permanent_error_is_an_error_outcome_without_retry(db, scripted):
    scripted(always_fail={"triage": LLMError("refused")})
    snap = _delta(
        ("support_pipeline_retries_total", {"step": "triage"}),
        _step("triage", "error"),
        ("support_tickets_processed_total", {"outcome": "failed"}),
    )
    ticket = Ticket(**TICKET)
    db.add(ticket)
    db.commit()

    with pytest.raises(LLMError):
        tasks.process_ticket.delay(ticket.id).get()

    assert snap("support_pipeline_retries_total", step="triage") == 0
    assert snap(_step("triage", "error")) == 1
    assert snap("support_tickets_processed_total", outcome="failed") == 1


def test_step_lost_to_a_concurrent_run_is_discarded_but_its_cost_counts(
    db, scripted, monkeypatch
):
    scripted()
    real_commit = tasks._commit_step

    def concurrent_run_wins(session, ticket_id, step, *args):
        if step == "triage":
            other = SessionLocal()
            try:
                t = other.get(Ticket, ticket_id)
                t.priority, t.category, t.sentiment = "low", "general", "neutral"
                t.needs_human, t.summary = False, "done elsewhere"
                t.triage_status = StepStatus.COMPLETED
                other.commit()
            finally:
                other.close()
        return real_commit(session, ticket_id, step, *args)

    monkeypatch.setattr(tasks, "_commit_step", concurrent_run_wins)
    calls = _llm("support_llm_calls_total", "triage", provider="fake", model="fake-heuristic-v1")
    snap = _delta(_step("triage", "discarded"), _step("triage"), calls)
    ticket = Ticket(**TICKET)
    db.add(ticket)
    db.commit()

    tasks.process_ticket.delay(ticket.id).get()

    assert snap(_step("triage", "discarded")) == 1
    assert snap(_step("triage", "completed")) == 0
    assert snap(calls) == 1  # the call was paid for either way


def test_rerun_of_a_completed_ticket_counts_as_already_completed(db, scripted):
    scripted()
    ticket = Ticket(**TICKET)
    db.add(ticket)
    db.commit()
    tasks.process_ticket.delay(ticket.id).get()
    snap = _delta(
        ("support_tickets_processed_total", {"outcome": "already_completed"}),
        _step("triage"),
    )

    tasks.process_ticket.delay(ticket.id).get()

    assert snap("support_tickets_processed_total", outcome="already_completed") == 1
    assert snap(_step("triage")) == 0


def test_idempotent_replays_and_conflicts_are_counted(client):
    snap = _delta(
        *[("support_tickets_ingested_total", {"result": r})
          for r in ("created", "replayed", "conflict")]
    )
    headers = {"Idempotency-Key": "retry-1"}

    assert client.post("/tickets", json=TICKET, headers=headers).status_code == 201
    assert client.post("/tickets", json=TICKET, headers=headers).status_code == 200
    other = {**TICKET, "body": "something else"}
    assert client.post("/tickets", json=other, headers=headers).status_code == 422

    assert snap("support_tickets_ingested_total", result="created") == 1
    assert snap("support_tickets_ingested_total", result="replayed") == 1
    assert snap("support_tickets_ingested_total", result="conflict") == 1


def test_enqueue_failures_are_counted(client, monkeypatch):
    import app.main as main

    def broker_down(*_args, **_kwargs):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(main.process_ticket, "delay", broker_down)
    before = _value("support_ticket_enqueue_failures_total")

    assert client.post("/tickets", json=TICKET).status_code == 201

    assert _value("support_ticket_enqueue_failures_total") == before + 1


# --- label cardinality --------------------------------------------------------

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def test_routes_are_labelled_by_template_never_by_raw_path(client):
    ids = [client.post("/tickets", json=TICKET).json()["id"] for _ in range(3)]
    for ticket_id in ids:
        client.get(f"/tickets/{ticket_id}")
        client.get(f"/tickets/{ticket_id}/audit")
    client.get("/tickets/not-a-real-id")
    client.get("/no/such/path/12345")
    client.request("PROPFIND", "/tickets")

    families = _scrape(client)
    routes = {s.labels["route"] for s in families["support_http_requests"]}
    methods = {s.labels["method"] for s in families["support_http_requests"]}

    assert {"/tickets", "/tickets/{ticket_id}", "/tickets/{ticket_id}/audit"} <= routes
    assert "unmatched" in routes
    assert not any("not-a-real-id" in r or "12345" in r for r in routes)
    assert methods <= {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "other"}
    assert "other" in methods


def test_no_metric_carries_ticket_ids_and_series_stay_bounded(client):
    ids = [client.post("/tickets", json={**TICKET, "subject": f"s{i}"}).json()["id"]
           for i in range(5)]
    for ticket_id in ids:
        client.get(f"/tickets/{ticket_id}")

    resp = client.get("/metrics", headers={"X-API-Key": READ_ONLY_KEY})
    support = [line for line in resp.text.splitlines() if line.startswith("support_")]
    assert support
    assert not any(_UUID_RE.search(line) for line in support)
    for ticket_id in ids:
        assert ticket_id not in resp.text

    allowed = {"method", "route", "status", "result", "step", "outcome", "provider",
               "model", "purpose", "kind", "le"}
    for family in text_string_to_metric_families(resp.text):
        if not family.name.startswith("support_"):
            continue
        for sample in family.samples:
            assert set(sample.labels) <= allowed, (family.name, sample.labels)
        # Five tickets and their reads must not have added per-ticket series.
        series = {tuple(sorted((k, v) for k, v in s.labels.items() if k != "le"))
                  for s in family.samples}
        assert len(series) < 60, family.name


def test_step_and_outcome_labels_come_from_closed_sets(client, scripted):
    scripted(failures={"triage": [TransientLLMError("503")]})
    client.post("/tickets", json=TICKET)

    families = _scrape(client)
    steps = families["support_pipeline_step_duration_seconds"]
    assert {s.labels["step"] for s in steps} <= {"triage", "routing", "drafting"}
    assert {s.labels["outcome"] for s in steps} <= {
        "completed", "discarded", "transient_error", "error"
    }
    assert {s.labels["step"] for s in families["support_pipeline_retries"]} <= {
        "triage", "routing", "drafting", "none"
    }


def test_provider_and_model_labels_are_validated_and_capped(monkeypatch):
    monkeypatch.setattr(m, "_seen", {})
    assert m.bounded_label("model", "gpt-4o-mini-2024-07-18") == "gpt-4o-mini-2024-07-18"
    assert m.bounded_label("model", "") == m.OTHER
    assert m.bounded_label("model", None) == m.OTHER
    assert m.bounded_label("model", "has spaces") == m.OTHER
    assert m.bounded_label("model", "x" * 81) == m.OTHER
    assert m.bounded_label("model", 'quote"in') == m.OTHER

    for i in range(m.MAX_DISTINCT_VALUES * 2):
        m.bounded_label("model", f"model-{i}")
    seen = m._seen["model"]
    assert len(seen) == m.MAX_DISTINCT_VALUES
    assert m.bounded_label("model", "one-more-model") == m.OTHER
    # Values already admitted keep their own series.
    assert m.bounded_label("model", "gpt-4o-mini-2024-07-18") == "gpt-4o-mini-2024-07-18"
    # Each label is capped separately.
    assert m.bounded_label("provider", "openai") == "openai"


# --- access control on /metrics -----------------------------------------------


def test_metrics_need_a_key_by_default(client):
    resp = client.get("/metrics", headers=NO_KEY)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "ApiKey"
    assert "support_" not in resp.text
    assert client.get("/metrics", headers={"X-API-Key": "not-a-key"}).status_code == 401


@pytest.mark.parametrize("key", [READ_ONLY_KEY, TEST_API_KEY])
def test_read_and_write_keys_can_scrape(client, key):
    resp = client.get("/metrics", headers={"X-API-Key": key})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# TYPE support_http_requests_total counter" in resp.text


def test_metrics_fail_closed_without_configured_keys(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_keys", "")
    assert client.get("/metrics").status_code == 503


def test_metrics_token_replaces_api_keys(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "metrics_token", TOKEN)

    no_token = client.get("/metrics", headers={"X-API-Key": READ_ONLY_KEY})
    assert no_token.status_code == 401
    assert no_token.headers["WWW-Authenticate"] == "Bearer"
    wrong = client.get("/metrics", headers={"Authorization": "Bearer wrong-token-0000000"})
    assert wrong.status_code == 401
    assert client.get("/metrics", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    ok = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}", **NO_KEY})
    assert ok.status_code == 200
    # The metrics token is not an API key: it can't read tickets.
    assert client.get("/tickets", headers={"X-API-Key": TOKEN}).status_code == 401


def test_metrics_public_on_a_private_network(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "metrics_public", True)
    assert client.get("/metrics", headers=NO_KEY).status_code == 200
    # Only /metrics opens up.
    assert client.get("/tickets", headers=NO_KEY).status_code == 401


def test_metrics_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "metrics_enabled", False)
    assert client.get("/metrics").status_code == 404


def test_metrics_are_not_in_the_openapi_schema(client):
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize(
    ("token", "public", "message"),
    [(TOKEN, True, "not both"), ("short", False, "at least 16")],
)
def test_ambiguous_or_weak_metrics_config_fails_at_startup(monkeypatch, token, public, message):
    monkeypatch.setattr(get_settings(), "metrics_token", token)
    monkeypatch.setattr(get_settings(), "metrics_public", public)
    with pytest.raises(ValueError, match=message):
        validate_metrics_settings()


# --- worker metrics server ----------------------------------------------------


@pytest.fixture
def worker_server():
    server = m.start_worker_metrics_server("127.0.0.1", 0)
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


def test_worker_server_applies_the_same_access_rules(worker_server, monkeypatch):
    status, headers, _ = _get(f"{worker_server}/metrics")
    assert status == 401 and headers["WWW-Authenticate"] == "ApiKey"
    status, _, body = _get(f"{worker_server}/metrics", {"X-API-Key": READ_ONLY_KEY})
    assert status == 200
    assert "# TYPE support_pipeline_step_duration_seconds histogram" in body

    monkeypatch.setattr(get_settings(), "metrics_token", TOKEN)
    assert _get(f"{worker_server}/metrics", {"X-API-Key": READ_ONLY_KEY})[0] == 401
    assert _get(f"{worker_server}/metrics", {"Authorization": f"Bearer {TOKEN}"})[0] == 200

    assert _get(f"{worker_server}/other", {"Authorization": f"Bearer {TOKEN}"})[0] == 404


def test_worker_server_merges_metrics_from_pool_processes(tmp_path):
    """
    With PROMETHEUS_MULTIPROC_DIR set, samples recorded in forked pool children
    (as Celery's prefork pool does) are served by the parent's exposition.
    Runs in a fresh interpreter because the mode is fixed at import time.
    """
    script = textwrap.dedent(
        """
        import json, multiprocessing

        import app.metrics as m

        def task():
            m.TICKETS_PROCESSED.labels(outcome="completed").inc()
            m.STEP_DURATION.labels(step="triage", outcome="completed").observe(0.2)
            m.record_llm_usage(
                {"provider": "openai", "model": "gpt-4o-mini", "input_tokens": 100,
                 "output_tokens": 10, "latency_ms": 250, "cost_usd": 0.5},
                "triage",
            )

        ctx = multiprocessing.get_context("fork")
        children = [ctx.Process(target=task) for _ in range(2)]
        for p in children:
            p.start()
        for p in children:
            p.join()
            assert p.exitcode == 0
        print(json.dumps(m.render()[0].decode()))
        """
    )
    env = {
        **os.environ,
        "PROMETHEUS_MULTIPROC_DIR": str(tmp_path),
        "PYTHONPATH": str(REPO_ROOT),
    }
    out = subprocess.run(
        [sys.executable, "-c", script], env=env, cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=60, check=True,
    )
    text = json.loads(out.stdout.strip().splitlines()[-1])

    assert 'support_tickets_processed_total{outcome="completed"} 2.0' in text
    assert (
        'support_pipeline_step_duration_seconds_count{outcome="completed",step="triage"} 2.0'
        in text
    )
    assert re.search(
        r'support_llm_tokens_total\{kind="input",model="gpt-4o-mini",provider="openai",'
        r'purpose="triage"\} 200\.0', text
    )
    assert (
        'support_llm_cost_usd_total{model="gpt-4o-mini",provider="openai",purpose="triage"} 1.0'
        in text
    )
