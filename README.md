# Sportspipeline

A knowledge base for the sports trading stack, built in Postgres (Supabase):

- **Semantic layer**: an ontology registry (`object_type`, `link_type`), typed
  tables for teams, players, games, markets, price snapshots and injuries, and a
  generic edge table that only accepts registered relations.
- **Pipeline**: ingest, normalize, signal, grade, ledger. Every pull is recorded.
  Every signal carries the price it was made at. Every grade comes from an
  official result.
- **Kinetic layer**: agents (Hermes, Gnoesis, the Risk Officer) never write
  orders. They call `kb.propose_action`. The database checks the request against
  its JSON schema and the risk limits, records every check, and approves or
  rejects it. Only the executor login can act on an approved request.

This is Phase 1 of the plan in
[*Auto-ontology knowledge graph KB for the sports trading stack*](docs/DESIGN.md#source-plan):
the structured ontology and the action layer, with no LLM extraction yet.

## What runs today

| Piece | State |
|---|---|
| ESPN ingest: teams, schedules, results, DraftKings open/current quotes, injuries | Works on live data, no key needed |
| Alias-table normalizer; unknown names are recorded, never guessed | Works |
| Signals stamped from a pregame snapshot; out-of-game picks rejected and recorded | Works |
| Grader: moneyline, spread, total, push, void, closing line value | Works |
| Ledger view and CSV export | Works |
| `propose_action` with schema, stake, daily exposure, kill switch, price and line checks | Works |
| Executor, `dry_run` mode: builds the order and records it as `not_sent` | Works |
| Executor, `live` mode | **Not available.** No venue adapter implements a real order API yet |
| ProphetX, Kalshi, Polymarket markets | Not ingested yet (ESPN/DraftKings only) |
| News, extracted facts, pgvector, auto-ontology proposals | Phases 2-3, not started |

## Setup

Needs Python 3.11+ and Postgres with `pg_jsonschema` (Supabase has it, and so
does the Supabase Postgres image used for tests).

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env               # set DATABASE_URL
sportspipeline migrate
sportspipeline teams --league nfl
sportspipeline ingest scoreboard --league nfl --dates next7
sportspipeline ingest injuries --league nfl
sportspipeline runs                # every run, with status ok / empty / failed
```

On Supabase, use the direct or session-pooler connection string as
`DATABASE_URL`. Everything lives in schema `kb`, which Supabase does not expose
through its REST API. `anon` and `authenticated` get no access.

## Agents and the action layer

Each bot gets its own Postgres login, a member of exactly one group role:

| Group | Can do |
|---|---|
| `sp_agent` | read the KB, `propose_action`, `size_stake`, `neighbors` |
| `sp_risk_officer` | the same, plus cancel any order; can only tighten limits |
| `sp_executor` | `claim_approved`, `record_execution` |
| `sp_human` | `set_risk_limit_human`: the only way to raise a limit or release a kill switch |

```bash
sportspipeline agents --id hermes --name Hermes --role agent --login hermes_bot
sportspipeline agents --id risk_officer --name "Risk Officer" --role risk_officer --login risk_officer_bot
sportspipeline agents --id executor --name Executor --role executor --login executor_bot
sportspipeline agents --id jeff --name Jeff --role human --login jeff_human

# a human sets the first limit; with no global limit every order is rejected
sportspipeline limits set --scope global --max-stake 50 --max-daily 100 --max-age 900 --reason "start"

# as an agent (AGENT_DATABASE_URL)
sportspipeline markets --league nfl
sportspipeline size --market-id 1 --side home --prob 0.56 --bankroll 1000
sportspipeline propose place_order '{"market_id": 1, "side": "home", "stake": 20, "limit_american": -130}'

# as the executor (EXECUTOR_DATABASE_URL)
sportspipeline executor
```

Real output from 26 Nov 2026 (Bears at Lions, DraftKings):

```
11b836e3-...  approved  all checks passed
   1 PASS role                 agent
   2 PASS param_schema
   3 PASS market_open          game 1 is scheduled, starts 2026-11-26 18:00:00+00
   4 PASS side_valid           home on a moneyline market
   5 PASS global_limit_exists  a global risk limit must exist before any order is approved
   6 PASS kill_switch          global:* [limit 1]
   7 PASS max_stake            stake 20, max 50 (global:*) [limit 1]
   8 PASS daily_exposure       used 0 + stake 20, max 100 (global:*) [limit 1]
   9 PASS price_fresh          quote 2 observed 2026-11-26 11:52:28+00, max age 3600s
  10 PASS price_within_limit   current -102, limit -130
```

From Python, an agent uses `sportspipeline.actions.AgentClient(conn)`.

## Signals, grading, ledger

```bash
sportspipeline signal --market-id 1 --side home --model hermes-v1 --by hermes --prob 0.56
sportspipeline grade                       # after the results are ingested
sportspipeline ledger summary
sportspipeline ledger export --out data/ledger.csv
```

## Graph queries

`kb.graph_edge_v` presents every foreign-key relation and every `kb.edge` row
as one edge list. `kb.neighbors(type, id, depth, at)` walks it with a recursive
CTE, respecting validity windows:

```bash
sportspipeline graph --type Team --id 11 --depth 2
```

## Tests

112 tests run against a real Postgres, using recorded ESPN responses
(`tests/fixtures/espn`, listed with URL, fetch time and sha256 in
`MANIFEST.json`, re-recordable with `scripts/record_espn_fixtures.py`).

```bash
docker run -d --name sportspipeline_pg -e POSTGRES_PASSWORD=postgres \
    -p 127.0.0.1:5481:5432 public.ecr.aws/supabase/postgres:17.11.0.004
.venv/bin/pytest -q
```

`SP_TEST_ADMIN_DSN` overrides the server. Each test gets its own database
cloned from a migrated template.

## Conventions

- Every ingest run is recorded, so a failure is visible instead of looking like
  "no data".
- Team and market names normalize through a table of aliases, not code
  branches.
- Signals carry their entry price and timestamp; grades come from official
  results.
- A signal whose team is not in the named game is rejected, never graded.
- No schema reset in place of a migration: an edited migration is refused.
- Secrets live in the environment. `.env` is never committed.
