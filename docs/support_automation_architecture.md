# 🧭 Architecture Blueprint

# Multi‑Agent Customer Support Automation

``` mermaid
flowchart LR
    U[User] --> FE[Next.js Dashboard]
    FE --> API[FastAPI Backend]
    API --> Q[(Redis Queue)]
    Q --> W[Worker Agents]
    W --> DB[(PostgreSQL)]
    W --> LLM[OpenAI API]
```

Agents: 1. Triage Agent -- classification 2. Routing Agent -- rule-based
assignment 3. Draft Agent -- auto-response drafting

Data Flow: Ticket → API → Queue → Worker → LLM → Database → Dashboard
