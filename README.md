# Multi-Agent Customer Support Automation System (MVP)

FastAPI + PostgreSQL + Redis + Celery backend for multi-agent customer support automation. Includes a web dashboard for creating tickets and viewing triage, routing, and draft replies.

## Components

- **API (`app.main`)**: FastAPI service exposing:
  - `GET /` – dashboard UI
  - `POST /tickets` – ticket ingestion; stores ticket and enqueues Celery pipeline
  - `GET /tickets` – list tickets (newest first)
  - `GET /tickets/{id}` – fetch ticket with triage/routing/draft fields
  - `GET /tickets/{id}/audit` – audit trail events
  - `GET /docs` – Swagger UI
- **Celery worker**: Runs the `process_ticket` task:
  - Triage agent → OpenAI, strict JSON validated by Pydantic
  - Routing module → rules-based assignment (team, SLA)
  - Drafting agent → reply drafts via OpenAI
- **Database**: `Ticket` and `AuditLog` tables (PostgreSQL).

## Secrets

- **Never commit `.env`.** It is in `.gitignore`. Put your real `OPENAI_API_KEY` and any overrides in `.env` only.
- For Docker, set `OPENAI_API_KEY` in the environment when you run `docker-compose up` (e.g. `export OPENAI_API_KEY=sk-...`).

## Environment variables

Create a `.env` file in the project root for local runs. The app loads it automatically (and overrides shell env for `OPENAI_API_KEY`).

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | **Required** for triage and draft agents. |
| `DATABASE_URL` | Postgres URL (defaults in code for Docker; use `localhost` for local run.) |
| `REDIS_URL` | Redis URL. |
| `CELERY_BROKER_URL` | Celery broker (Redis). |
| `CELERY_RESULT_BACKEND` | Celery results (Redis). |

## Local development (without Docker)

1. Create `.venv`, install deps, create `.env` with `OPENAI_API_KEY` and optional `DATABASE_URL`/`REDIS_URL` (use `localhost` if Postgres/Redis run in Docker).
2. Start Postgres and Redis (e.g. `docker-compose up -d postgres redis`).
3. Run API: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
4. In another terminal, run worker: `celery -A app.celery_app worker --loglevel=info`
5. Open **http://localhost:8000** for the dashboard.

## Local development (full Docker Compose)

```bash
export OPENAI_API_KEY=your_key_here
docker-compose up --build
```

- **API + dashboard**: http://localhost:8000
- **Postgres**: `localhost:5432` (user `support_user`, db `support_db`, password `support_pass`)
- **Redis**: `localhost:6379`

## Stop everything

- **Local API/worker**: Stop the terminals (Ctrl+C) where uvicorn and celery are running.
- **Docker**: From the project root run:
  ```bash
  docker-compose down
  ```

## Basic flow

1. Create a ticket (dashboard or `POST /tickets` with `subject`, `body`, optional `customer_id`, `channel`).
2. Ticket is stored and `process_ticket` is queued to Celery.
3. Worker: triage (OpenAI) → routing (rules) → draft (OpenAI); each step writes to the ticket and audit log.
4. Open the ticket in the dashboard and click **Refresh** to see updated triage, team, SLA, and draft reply.
