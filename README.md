# AI Support Automation System

A backend service that takes in customer support tickets, uses an LLM to classify them (priority, category, sentiment, whether a human is needed), routes each one to a team with an SLA, and drafts a reply for an agent to review. It's built with FastAPI, PostgreSQL, Celery/Redis, and pluggable LLM providers: OpenAI, Anthropic, or an offline fake.

The interesting part is what happens around the LLM calls: they fail, they're slow, they cost money, and you have to check what they return. So the pipeline is idempotent and safe to retry, every call records tokens and estimated cost, the output schema is strict and validated, and there's a labeled eval set so triage quality is measured instead of assumed.

## The problem

Support queues mix outages, billing errors, legal threats and "how do I change my email" in one stream. The first minutes of handling a ticket are spent on the same things every time: decide how urgent it is, what it's about, who should own it, whether a person must step in, and write a first reply. This service automates that first pass and leaves a full audit trail. A human still owns the final answer.

## Architecture

```
                 X-API-Key                         ┌──────────────────────────┐
  client ──POST /tickets──▶ FastAPI ──commit──────▶│ PostgreSQL               │
  dashboard ◀─GET /tickets── (app.main)            │  tickets (+ step status) │
                  │   ▲                            │  audit_logs (JSONB)      │
                  │   └── /healthz /readyz         └──────────▲───────────────┘
                  │ enqueue(ticket_id, request_id)            │ one transaction per step
                  ▼                                           │
               Redis ───────────▶ Celery worker: process_ticket(ticket_id)
               (broker)            │
                                   ├─ 1. triage   ──▶ LLMProvider ──▶ OpenAI | Anthropic | fake
                                   │     strict JSON schema -> TriageResult (pydantic)
                                   ├─ 2. routing  ──▶ rules: category -> team, priority -> SLA
                                   └─ 3. drafting ──▶ LLMProvider (free text)
                                   retries: transient LLM errors, exponential backoff + jitter
```

Code map:

| Path | Responsibility |
|---|---|
| `app/main.py` | HTTP API, request-ID middleware, enqueueing, dashboard |
| `app/tasks.py` | `process_ticket` orchestration, idempotency, retries, failure handling |
| `app/agents_triage.py`, `app/agents_drafting.py` | Prompt construction and output validation |
| `app/llm/` | Provider interface, OpenAI/Anthropic/fake implementations, pricing |
| `app/routing.py` | Deterministic routing rules |
| `app/security.py`, `app/limits.py` | API-key auth, request body size limit |
| `app/idempotency.py` | `Idempotency-Key` handling for ticket creation |
| `app/health.py`, `app/logging_config.py` | Readiness checks, structured logging |
| `migrations/` | Alembic schema migrations |
| `evals/` | Labeled dataset and triage eval harness |

## Design decisions and trade-offs

**Postgres is the source of truth, not Celery.** Ticket state, per-step status and the audit log live in the database. Celery results are turned off (`task_ignore_result`) because nothing reads them. Because of that, a broker outage can't lose a ticket: `POST /tickets` commits first, then tries to enqueue. If the enqueue fails it records `enqueue_failed`, still returns 201, and the ticket can be re-queued with `POST /tickets/{id}/reprocess`. Before task results were turned off, a Redis outage made `POST /tickets` hang for about 19 s while the result backend retried. With results off and publish retries bounded, it returned in 0.27 s in a local test with Redis down.

**Idempotent steps instead of exactly-once delivery.** Workers ack late and requeue tasks if the worker dies, so a task can run more than once. Each step (triage, routing, drafting) writes its output, its `*_status = completed`, and its audit event in one transaction. A rerun skips completed steps, so a retry after a drafting timeout doesn't pay for triage again. Before committing, a step re-reads the row with `SELECT ... FOR UPDATE`, so if two runs race, the second one discards its result instead of overwriting or duplicating. The cost: two simultaneous runs can still both pay for the same LLM call before one of them loses at commit. Preventing that would mean holding a lock or lease for the whole LLM call, which this doesn't do yet.

**Retry only what's worth retrying.** Providers map SDK errors to two types. `TransientLLMError` covers 408/409/429/5xx, connection errors and timeouts, and Celery retries it with exponential backoff and jitter (`TASK_MAX_RETRIES`, default 5). `LLMError` covers bad requests, auth failures, refusals, truncated output and schema violations, and fails fast. When the task finally gives up, `on_failure` marks the ticket `failed` and the step that was running `failed`, exactly once. SDK-level retries are kept low (`LLM_SDK_MAX_RETRIES=1`) so the two retry layers don't multiply. Each LLM request has a timeout, and each task has a soft and a hard time limit.

**Strict structured output, then validate anyway.** Triage asks the provider for JSON matching a schema generated from the `TriageResult` pydantic model, with `additionalProperties: false` and every field required. That's OpenAI `json_schema` strict mode and Anthropic `output_config.format`. The response is validated again with pydantic. Category comes from a closed list (`billing`, `technical`, `account`, `sales`, `general`). It used to be free text, routing matched it by substring, and quality couldn't be measured. The labeling rubric is written into the schema's field descriptions, so the model sees the same definitions the eval labels use.

**Provider abstraction is deliberately thin.** `LLMProvider.complete(LLMRequest) -> LLMResponse` (text plus usage) is all the agents need. Prompts stay in the agents, and SDK-specific details stay in the providers:

- OpenAI: Chat Completions.
- Anthropic: Messages API. Default model `claude-opus-5-5`, effort set explicitly (`low` by default, because these are classification and short-reply tasks), and server-side refusal fallback on by default.
- Fake: a deterministic keyword heuristic, so tests, CI and local development need no API key.

`LLM_PROVIDER=auto` picks a real provider when a key is present. It refuses to fall back to the fake in `ENVIRONMENT=production`.

**Cost is recorded per call, not inferred later.** Every `triage_completed` and `draft_created` audit event stores the provider, the model that actually served the request, input and output tokens, latency, and an estimated USD cost from a small price table. If a model's price isn't in the table, `cost_usd` is `null` rather than a guess. `LLM_PRICE_OVERRIDES` fixes or extends prices without a deploy. The estimate prices all input tokens at the uncached rate.

**Auth is simple on purpose.** Write endpoints need an `X-API-Key` that matches one of `API_KEYS`. The list is comma-separated, so keys can be rotated with an overlap. Comparison is constant-time. The audit log records a SHA-256 fingerprint of the key, never the key itself. With no keys configured, writes are refused (503) unless `AUTH_DISABLED=true`. Read endpoints are open. See Limitations.

**Ingestion is idempotent when the client asks for it.** `POST /tickets` accepts an optional `Idempotency-Key` header. The key is stored in an `idempotency_keys` table together with the caller's API-key fingerprint, a SHA-256 hash of the validated request body, the ticket it created and an expiry (`IDEMPOTENCY_KEY_TTL_SECONDS`, default 24 h). The ticket, its `ticket_created` audit event and the key record are committed in one transaction, so a key never points at a ticket that doesn't exist. A retry with the same key and body returns the original ticket with `200` and `Idempotent-Replayed: true`, and doesn't enqueue it again. The same key with a different body is a `422`. Keys are scoped per API key, so two clients can't collide or read each other's tickets through a shared key. Two first requests racing with the same key are settled by a unique constraint on `(api_key, key)`: the loser's insert fails, it rolls back and returns the winner's ticket (or `422` if its body differs). The body hash is computed after validation, so key order, an explicit `null` versus an omitted optional field, and whitespace that's stripped anyway count as the same request.

## Running it

### Docker Compose

```bash
export API_KEYS=$(openssl rand -hex 16)        # required: protects write endpoints
export OPENAI_API_KEY=...                       # or ANTHROPIC_API_KEY=..., or neither (fake provider)
docker compose up --build
```

The API runs `alembic upgrade head` before starting. The worker waits until the API is healthy. The dashboard is at http://localhost:8000 (paste the API key into the form), and Swagger is at http://localhost:8000/docs.

### Local (without Docker for the app)

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements-dev.txt
cp .env.example .env                            # then set API_KEYS and, optionally, an LLM key
docker compose up -d postgres redis             # or point DATABASE_URL / CELERY_BROKER_URL elsewhere
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --port 8000
.venv/bin/celery -A app.celery_app worker --loglevel=info   # add --pool=solo on macOS
```

On macOS, Celery's default prefork pool runs in spawn mode, and in a local run tasks failed with `ValueError: not enough values to unpack`. `--pool=solo` works. The Linux containers use fork and don't need this.

**Existing databases** created by the old `create_all` start-up code already have the initial schema. Mark them with `alembic stamp 0001`, then run `alembic upgrade head`. Migration `0002` backfills step status for tickets that were already processed.

### Configuration

Environment variables take precedence over `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | compose Postgres | SQLAlchemy URL (`postgresql+psycopg://...`) |
| `CELERY_BROKER_URL` | `redis://redis:6379/1` | Celery broker; `/readyz` pings it if it is Redis |
| `CELERY_RESULT_BACKEND` | `redis://redis:6379/2` | Configured for Celery, but results are not stored |
| `ENVIRONMENT` | `development` | `production` forbids the implicit fake provider |
| `API_KEYS` | empty | Comma-separated keys accepted in `X-API-Key` |
| `AUTH_DISABLED` | `false` | Allow writes without a key (local experiments only) |
| `CORS_ORIGINS` | empty | Comma-separated allowed origins; empty = same-origin only |
| `MAX_REQUEST_BYTES` | `65536` | Request body limit (413 above it) |
| `IDEMPOTENCY_KEY_TTL_SECONDS` | `86400` | How long an `Idempotency-Key` is remembered (24 h) |
| `LLM_PROVIDER` | `auto` | `auto`, `openai`, `anthropic`, `fake` |
| `OPENAI_API_KEY` | | OpenAI credentials |
| `OPENAI_MODEL_TRIAGE`, `OPENAI_MODEL_DRAFTING` | `gpt-4o-mini` | OpenAI models per step |
| `ANTHROPIC_API_KEY` | | Anthropic credentials |
| `ANTHROPIC_MODEL_TRIAGE`, `ANTHROPIC_MODEL_DRAFTING` | `claude-opus-5-5` | Anthropic models per step |
| `ANTHROPIC_EFFORT` | `low` | `output_config.effort` for both steps |
| `ANTHROPIC_REFUSAL_FALLBACK` | `true` | Server-side refusal fallback (beta endpoint) |
| `LLM_TIMEOUT_SECONDS` | `30` | Per-request timeout for LLM SDK clients |
| `LLM_SDK_MAX_RETRIES` | `1` | SDK-level retries (Celery handles longer backoff) |
| `LLM_PRICE_OVERRIDES` | `{}` | JSON `{"model": [input_usd_per_mtok, output_usd_per_mtok]}` |
| `TASK_MAX_RETRIES` | `5` | Celery retries for transient failures |
| `TASK_RETRY_BACKOFF_SECONDS` / `TASK_RETRY_BACKOFF_MAX_SECONDS` | `2` / `300` | Backoff factor and cap |
| `TASK_SOFT_TIME_LIMIT_SECONDS` / `TASK_TIME_LIMIT_SECONDS` | `120` / `150` | Per-task time limits |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `LOG_FORMAT=text` for terminals |

## API

```bash
API=http://localhost:8000
KEY=your-api-key

# Create a ticket (processing happens asynchronously)
curl -s -X POST $API/tickets \
  -H "Content-Type: application/json" -H "X-API-Key: $KEY" -H "X-Request-ID: demo-1" \
  -d '{"subject": "Charged twice", "body": "I was billed twice for March.", "channel": "email"}'

# Safe to retry: the same Idempotency-Key and body return the original ticket
# (201 the first time, then 200 with "Idempotent-Replayed: true"); the same key
# with a different body is a 422. Keys are per API key and expire after 24 h.
curl -s -i -X POST $API/tickets \
  -H "Content-Type: application/json" -H "X-API-Key: $KEY" -H "Idempotency-Key: order-1234-retry" \
  -d '{"subject": "Charged twice", "body": "I was billed twice for March."}'

# Read it back: triage, routing, draft, plus status / triage_status / routing_status /
# drafting_status / attempts / last_error
curl -s $API/tickets/<id>

# Audit trail, including per-call tokens and estimated cost
curl -s $API/tickets/<id>/audit

# List the 100 newest tickets
curl -s $API/tickets

# Re-queue a failed or never-enqueued ticket (completed steps are kept); 409 if already done
curl -s -X POST $API/tickets/<id>/reprocess -H "X-API-Key: $KEY"

# Probes
curl -s $API/healthz    # liveness, no dependencies
curl -s $API/readyz     # 200 when DB and Redis broker respond, otherwise 503 with per-check detail
```

Example audit event:

```json
{"event_type": "triage_completed",
 "payload": {"priority": "urgent", "category": "technical", "sentiment": "neutral",
             "needs_human": true, "summary": "Since 09:10 UTC every call returns HTTP 500.",
             "llm": {"provider": "fake", "model": "fake-heuristic-v1", "input_tokens": 110,
                     "output_tokens": 37, "latency_ms": 0, "cost_usd": 0.0}}}
```

Error responses: `401` missing or invalid key, `413` body too large, `422` validation (subject 1-300 chars, body 1-20,000 chars, unknown fields rejected, `Idempotency-Key` not 1-255 printable ASCII characters, or a key reused with a different body), `503` no API keys configured.

Logs are JSON lines with `request_id` and `ticket_id` on every record. The request ID is either the caller's `X-Request-ID` or a generated one, it's echoed in the response, and it's passed to the worker, so one ID ties together the API and worker logs for a ticket.

## Tests

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest                                    # SQLite, about 1 s
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/support_test \
  .venv/bin/python -m pytest                                  # same suite on Postgres
```

140 tests cover:

- routing rules and legacy-category normalization
- strict schema generation and validation
- both SDK providers against fake clients (request shape, error classification, refusal and truncation handling)
- provider selection
- the Celery task run eagerly: idempotent reruns, resuming after completed steps, transient retries, exhausted retries, fail-fast permanent errors, the concurrent-completion guard
- the HTTP API through TestClient: auth, size limits, reprocess, enqueue failure
- idempotency keys: replay, body mismatch, per-API-key scoping, expiry and its setting, malformed keys, and a lost insert race (both simulated and with two concurrent requests)
- logging and health probes
- pricing
- migrations: models match the migrations, downgrade is clean, the `0002` backfill
- the eval harness

The schema used in tests is built by running the Alembic migrations, not `create_all`.

CI (`.github/workflows/ci.yml`) runs ruff, the suite on SQLite, and the suite on a Postgres 16 service after `alembic upgrade` / `check` / `downgrade` / `upgrade`. It also runs the eval harness with the fake provider.

## Evaluation

`evals/tickets.jsonl` has 34 synthetic tickets I wrote by hand (no customer data), labeled for priority, category and needs_human using the rubric in the triage schema. Some are deliberately ambiguous (a nonprofit-discount question, a GDPR deletion request, a lawyer's notice).

```bash
.venv/bin/python -m evals.run_eval                                # offline fake provider
.venv/bin/python -m evals.run_eval --provider openai --json report.json
.venv/bin/python -m evals.run_eval --provider anthropic
```

The report shows:

- per-field accuracy and exact match
- needs_human precision and recall
- confusion matrices for priority and category
- every mismatch
- total tokens and estimated cost

Real providers make one paid call per ticket.

These are the results with the fake keyword heuristic. They're a baseline and a check that the harness works, not a measure of LLM quality:

| Field | Fake provider |
|---|---|
| priority | 58.8% (20/34) |
| category | 79.4% (27/34) |
| needs_human | 76.5% (26/34); precision 0.83, recall 0.42 |
| all three correct | 38.2% (13/34) |

I haven't published numbers for OpenAI or Anthropic because I haven't run those evals. Run the commands above with your own key to get them.

## Limitations

- **Read endpoints and the dashboard are unauthenticated**, so anyone who can reach the API can read ticket contents. Only writes are protected. Before real deployment, put reads behind auth or a private network.
- API keys are shared secrets from the environment. There are no per-tenant keys, no scopes, and no rate limiting.
- Two workers running the same ticket at once can both pay for an LLM call (correctness is protected; cost isn't).
- `Idempotency-Key` is opt-in: a client that retries `POST /tickets` without one still creates duplicates. Keys are scoped to the API key, so a client that rotates its key between a request and its retry gets a new ticket, and with `AUTH_DISABLED=true` every caller shares one scope. Expired rows are only replaced when the same key is reused; nothing purges them in the background yet (`expires_at` is indexed so a periodic `DELETE ... WHERE expires_at < now()` is cheap). A replay returns the ticket's current state, not a byte-for-byte copy of the first response.
- Triage is evaluated on only 34 synthetic tickets, labeled by one person. The drafting quality isn't evaluated at all.
- The cost estimate uses a hand-maintained price table and ignores cache discounts.
- No metrics endpoint (Prometheus/OpenTelemetry) yet. Observability is structured logs plus the audit trail.
- I couldn't run Docker Compose in the environment where this was built. The compose file was checked with `docker compose config`, and the app and worker were run end to end with a real Celery worker, using Postgres as both the database and (through kombu's SQLAlchemy transport) the broker. Redis-specific behaviour is covered by unit tests with a stubbed client.
- `docs/` contains the original planning notes (SRS, deployment sketch). They describe intentions, such as a Next.js frontend, that this repository doesn't implement.

## Roadmap

- Authenticate reads; per-tenant keys with scopes; rate limiting
- Periodic purge of expired idempotency keys
- Per-step leases so concurrent duplicates don't pay twice
- Prometheus metrics: step latency, retries, tokens, and cost by model
- Human approval workflow for drafts (approve/edit/send) with the audit trail
- Larger eval set with inter-annotator agreement, and a drafting rubric graded by people or an LLM judge
- Webhook/email ingestion

## License

MIT, see [LICENSE](LICENSE).
