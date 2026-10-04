
# Rasoi 

FastAPI + Neon Postgres backend, React/Vite/Tailwind dashboard.

## Run it
```bash
# 1. Neon: create a project, copy the POOLED connection string into backend/.env (see .env.example)
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # edit DATABASE_URL (+ ANTHROPIC_API_KEY optionally)
python -m db.init_db --reset    # creates tables + seeds the demo household
uvicorn main:app --reload --port 8000

# 2. dashboard
cd ../frontend && npm install && npm run dev      # http://localhost:5173
```
Smoke test: `curl localhost:8000/health`, then `curl -X POST localhost:8000/agent/run`.

## Build levels (maps to your roadmap)
| Level | Files |
|---|---|
| 1. Setup + Neon schema + seed | `db/schema.sql`, `db/database.py`, `db/seed.py`, `db/init_db.py` |
| 2. Tools (household, inventory, meals, shopping) | `tools/household.py`, `inventory.py`, `meals.py`, `shopping.py` |
| 3. Agent state machine (happy path) | `agent/state_machine.py`, `agent/rasoi.py`, `agent/llm.py` |
| 4. Pine Labs + Delhivery adapters | `integrations/pine_labs.py`, `delhivery.py`, `tools/payment.py`, `delivery.py` |
| 5. Failure handling | human states + `resolve()` in `agent/rasoi.py` |
| 6. Dashboard | `frontend/src/App.jsx` |
| 7. Gnani voice | `integrations/gnani.py`, `POST /voice` |
| 8. Crash recovery | `agent_runs` checkpoints, `h_payment`, `recover_orphans()` |
| 9. Failure simulator | `tools/world.py`, `POST /simulate/{kind}` |

## Things to verify before submitting
- Real Pine Labs / Delhivery / Gnani adapters use env-configurable paths; check them against your UAT docs/credentials. `USE_MOCKS=true` uses mocks whose state is kept in Neon (mock Gnani does not transcribe audio and is flagged `"mock": true`).
- For the Gnani "paste back exactly what Gnani returns" requirement, run with real credentials and copy the "Raw Gnani response" panel.
=======
# Rasoi

**A household meal agent that plans, buys, pays for and verifies tomorrow's meal on its own, and stops to ask a human when it shouldn't decide alone.**

Rasoi looks at a household's rules, pantry and budget, picks a meal, builds a shopping list, pays through Pine Labs, books delivery through Delhivery, and verifies each step before moving on. Voice notes in Indian languages (via Gnani) can change what it plans.

> Status: hackathon / ideathon MVP. Payments, delivery and voice run against mocks by default. See [Status and limitations](#status-and-limitations).

## How it works

```
React dashboard  ->  FastAPI  ->  Rasoi agent (state machine)  ->  Tools  ->  Neon Postgres
                                                  |
                                                  +-> Pine Labs (payments)
                                                  +-> Delhivery (delivery)
                                                  +-> Gnani (speech-to-text)
```

Three things are kept strictly separate:

| Concern | Who handles it |
|---|---|
| Judgement (which meal, how to explain it, what a voice note means) | LLM (Gemini), inside guard-rails |
| Hard rules (diet, allergies, budget, approved substitutes) | Deterministic code, never overridden by the LLM |
| Truth (did the payment or delivery really happen?) | Database and the external provider's response, never the LLM |

The agent walks an explicit state machine:

`START -> UNDERSTAND_HOUSEHOLD -> CHECK_INVENTORY -> SELECT_MEAL -> CHECK_MISSING_INGREDIENTS -> CHECK_BUDGET -> CREATE_ORDER -> PAYMENT -> VERIFY_PAYMENT -> DELIVERY -> VERIFY_DELIVERY -> UPDATE_INVENTORY -> LEARN -> COMPLETE`

When it can't safely continue it stops in a human state (`PAYMENT_FAILED`, `BUDGET_EXCEEDED`, `CONSTRAINT_CONFLICT`, `STALE_INVENTORY`, `DELIVERY_FAILED`, `UNRESOLVED`, `NEEDS_HUMAN`) and offers options on the dashboard.

## Features

- **Rule-aware meal selection:** hard constraints filter meals first, then the agent scores by expiring ingredients, nutrition goal, preferences, cost and recent repeats.
- **Substitutions and re-planning:** approved like-for-like substitutes first; if an item is out of stock with no substitute, it drops the meal and re-plans.
- **Verified actions only:** a payment becomes `SUCCESS` only when the provider confirms it. No automatic payment retries.
- **Crash recovery:** the run is checkpointed before every consequential call and payments use idempotency keys, so a restarted run verifies the existing payment instead of paying twice.
- **Voice input:** record a voice note in the dashboard (converted to 16 kHz mono WAV in the browser), send it to Gnani, and store both the raw response and the interpretation.
- **Decision log:** every step shows the action, why, result and next step.
- **Failure simulator:** buttons that change the world (stock, failures, rules) without ever telling the agent what to do.

## Quick start

Requirements: Python 3.12, Node 18+, a free [Neon](https://neon.tech) project.

```bash
# 1. Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # set DATABASE_URL to your Neon POOLED connection string
python -m db.init_db --reset      # creates tables and seeds a demo household
uvicorn main:app --reload --port 8000

# 2. Dashboard (new terminal)
cd frontend
npm install
npm run dev                       # http://localhost:5173
```

Smoke test: `curl localhost:8000/health`, then click **Run today's plan** in the dashboard.

## Configuration

All settings live in `backend/.env` (see `.env.example`).

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Neon pooled connection string (host contains `-pooler`) |
| `ANTHROPIC_API_KEY` | Holds your **Gemini** API key (the variable name is kept as-is). Optional; without it Rasoi uses deterministic ranking and regex voice parsing |
| `LLM_MODEL` | A Gemini model your key can use |
| `USE_MOCKS` | `true` uses built-in mocks for Pine Labs, Delhivery and Gnani, with state kept in Neon |
| `PINELABS_*`, `DELHIVERY_*`, `GNANI_*` | Credentials and paths for real mode |
| `PLAN_DAYS_AHEAD`, `STEP_DELAY`, `STALE_HOURS` | Agent behaviour |
| `DEBUG` | Show error details in API responses. Set to `false` in production |

## Demo scenarios

Each scenario reseeds the database, changes one thing in the world, then runs the agent.

| Scenario | What the agent should do |
|---|---|
| Happy path | Plan, buy, pay, deliver, update pantry |
| Out of stock (substitute exists) | Swap in an approved substitute |
| Out of stock (no substitute) | Drop the meal and re-plan |
| Payment failure | Stop, do not retry, ask a human |
| Delivery delay | Offer to cook from the pantry instead |
| Budget exceeded | Ask to approve, pick a cheaper meal, or cancel |
| Constraint conflict | Explain the conflict and offer rules to relax |
| Stale pantry | Ask for confirmation before planning |
| Crash and resume | Resume from the checkpoint and verify, never pay twice |

## Project structure

```
backend/
  main.py                FastAPI routes
  agent/                 state machine, Rasoi agent loop, LLM helper
  tools/                 household, inventory, meals, shopping, payment, delivery, learning, simulator
  integrations/          pine_labs.py, delhivery.py, gnani.py (mock + real adapters)
  db/                    schema.sql, seed.py, database.py, init_db.py
frontend/
  src/                   React dashboard (App.jsx, api.js, audio.js)
```

## Tech stack

| Layer | Tech |
|---|---|
| Frontend | React 18, Vite, Tailwind CSS v4 |
| Backend | Python, FastAPI, Uvicorn, Pydantic |
| Database | Neon Postgres (`psycopg` 3 with connection pooling) |
| LLM | Google Gemini (REST) |
| Voice | Gnani speech-to-text |
| Payments | Pine Labs |
| Delivery | Delhivery |

## Status and limitations

This is a demo, not a production service.

- **Mocks by default.** Real Pine Labs, Delhivery and Gnani adapters are written but their endpoint paths, auth and payloads are best guesses, configurable by env variable. Verify them against your own credentials and docs. Mock Gnani does not transcribe audio.
- **Real Pine Labs orders** normally need a customer to finish checkout, so in real mode Rasoi will correctly report `UNRESOLVED` rather than assume success.
- **No accounts.** The demo uses a single hard-coded household with no login or per-user access control.
- **The simulator reset drops all tables.** Do not expose `/simulate/*` outside development.
- **Background jobs run in threads** inside the web process, so run a single instance.
- **Pantry is manual.** There is no automatic inventory source yet.

Before real customers: add authentication and multi-household support, remove the simulator endpoints, replace drop-and-reseed with migrations, move runs to a job queue with locking, use provider webhooks, add monitoring and tests, and review payment-authorisation and data-protection requirements.
