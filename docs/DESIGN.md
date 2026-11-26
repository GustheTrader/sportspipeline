# Design

## Goal

One pipeline that turns sports data into graded signals, keeps the evidence
needed to trust them later, and lets agents act on them only through a layer
that keeps risk limits outside the agents.

## Source plan

This repository implements the plan *Auto-ontology knowledge graph KB for the
sports trading stack* (Grok Bot research page, 25 Nov 2026). Its
recommendation:

1. Start in Supabase, not in a new graph database. Put the ontology registry,
   typed tables, edges, news embeddings and the action layer in Postgres.
2. Use LLM extraction against a fixed schema for news and injuries.
3. Run auto-ontology only as a proposal step a human approves.
4. Add Graphiti later, only if temporal multi-hop questions need it.

| Plan phase | Status here |
|---|---|
| 0. Inventory: Hermes data, Drive backups, ProphetX order API, Zo service slots, extraction LLM | Open: see [Open questions](#open-questions) |
| 1. Structured ontology and action layer, executor in paper mode, no LLM | **Built** |
| 2. NewsItem + pgvector, LLM extraction of Injury/Fact with provenance | Not started. `NewsItem`, `Fact` and their relations are registered as `proposed` |
| 3. Auto-ontology into an `ontology_proposal` table, human-approved | Not started |
| 4. Graphiti on AuraDB Free or embedded, fed from the same rows | Not started, only if needed |
| 5. Modal for backfills and large extraction runs | Not started |

## Stages

1. **Ingest**: pull schedules, pregame odds, final results and injury
   reports. Every pull is a `kb.ingest_run` row with source, window, URL and
   outcome (`ok`, `empty`, `failed` with the error). The run row commits first;
   the work runs in its own transaction, so a failure rolls back the data and
   leaves the failed run visible.
2. **Normalize**: map vendor team names onto our ids through
   `kb.team_alias`. An unknown name goes to `kb.unresolved_name` with a count,
   and the game is skipped. The fix is a new alias row
   (`sportspipeline alias ...`), not code.
3. **Signal**: `kb.signal` must reference a `price_snapshot` observed before
   the game started. The trigger copies the entry price, line and stamp from
   the snapshot; the caller cannot type them in.
4. **Grade**: resolved from `kb.game` rows an ingest marked `final`. A pick
   whose team is not in the game is written to `kb.signal_rejection` and never
   graded; a trigger on `kb.grade` refuses it as well.
5. **Ledger**: `kb.signal`, `kb.grade`, `kb.signal_rejection`,
   `kb.risk_limit`, `kb.action_check` and `kb.action_event` are append-only.
   Reports read `kb.ledger_v`.

## Semantic layer

The registry (`kb.object_type`, `kb.link_type`) is the source of truth for what
exists. Entities are real tables with foreign keys; each foreign key is already
an edge. `kb.edge` holds relations that are not foreign keys, and its foreign key
to `kb.link_type` means only a registered, active, edge-stored relation can be
written. `kb.graph_edge_v` unions both into one edge list for traversal.

| Object | Table | Origin |
|---|---|---|
| Team, Player, Game | `kb.team`, `kb.player`, `kb.game` | ESPN (structured) |
| Market | `kb.market` | One per game, kind and venue |
| PriceSnapshot | `kb.price_snapshot` | Written only when a quote changes. Never put in a graph store |
| Injury | `kb.injury` | ESPN injury report, valid from report to resolution |
| Signal, Grade | `kb.signal`, `kb.grade` | Pipeline |
| Agent, RiskLimit, ActionRequest, Order | `kb.agent`, `kb.risk_limit`, `kb.action_request`, `kb.order_ticket` | Action layer |
| NewsItem, Fact | none yet | `proposed` (Phase 2) |

Time-bounded relations (`PLAYS_FOR`, `AFFECTS`) carry `valid_from`/`valid_to`.
An injury affects a game when it was open at kickoff (`kb.injury_game_v`).
League-specific fields stay optional properties until the data needs a type.

Quotes come in three labels. `open` is the opening line as the feed reports it.
`current` is the live pregame quote at `observed_at`. `close` is what the feed
reports after the game left `scheduled`. Signals and orders may use only
`current` quotes observed before kickoff. In-play quotes are not stored
(pregame only).

## Kinetic layer

```
agent login ──propose_action(action, params)──▶ kb (SECURITY DEFINER, search_path '')
                                                  ├─ role allowed for this action?
                                                  ├─ params match the action's JSON schema? (pg_jsonschema)
                                                  ├─ action checks (below), each written to kb.action_check
                                                  └─ kb.action_request: approved | rejected, with the first failed check
executor login ──claim_approved()──▶ re-checks kill switch and kickoff, else 'expired'
               ──record_execution()──▶ kb.order_ticket (dry_run: not_sent)
```

| Action | Who | Checks |
|---|---|---|
| `propose_edge` | agent, risk officer | market exists, game scheduled and not started, side fits market. Informational |
| `place_order` | agent | the above, a global limit exists, no kill switch on any applicable scope, stake ≤ every applicable `max_stake`, today's committed stake + this ≤ every `max_daily_exposure`, a current quote exists and is no older than the smallest `max_price_age_s` (default 900 s), current price at least as good as the limit, line unchanged for spreads and totals |
| `cancel_order` | agent (own orders), risk officer (any) | order exists and is still cancellable |
| `set_risk_limit` | agent, risk officer | may only lower a value or engage a kill switch; applied at once |
| `set_risk_limit_human()` | human only | any change, with a reason |
| `size_stake()` | agent, risk officer | fractional Kelly on the current quote, capped by every applicable `max_stake`; 0 when a kill switch is on or no limit exists |

Applicable limits are the current rows for `global`, the market's league,
the market, and the agent. They combine as the minimum, so adding a looser row
at a narrower scope cannot loosen anything. Order proposals take an advisory
lock, so two agents cannot both spend the last of a daily budget.

Permissions are Postgres grants, not application code. Agent groups have
`SELECT` on the KB and `EXECUTE` on three functions; no group has
`INSERT/UPDATE/DELETE` on any action-layer table. Row level security limits an
agent to its own requests, checks, events and orders. The views agents read
are `security_invoker`, so the same row-level security applies through them.

### Executor modes

`dry_run` builds the exact venue request and records it with status
`not_sent`. Nothing is sent and no fills are simulated. `live` requires
`SPORTSPIPELINE_LIVE=1` and a venue adapter with a real order API; none
exists yet, so live mode refuses to start and approved requests stay
unclaimed.

## Non-negotiables

- No signal without a stamped price.
- No grade against a game the pick does not belong to.
- No schema reset in place of a migration (applied migrations are checksummed).
- Agents propose; only the database decides and only the executor acts.
- Agents can tighten limits, never loosen them.
- Secrets from the environment only.

## Milestones

- [x] Store schema: teams, aliases, games, odds snapshots, signals, grades, runs
- [x] One ingest adapter end to end, with idempotent re-runs (ESPN)
- [x] Normalizer with an alias table and a test for unknown names
- [x] Grader that rejects out-of-game picks
- [x] Ledger export for backtesting
- [x] Ontology registry, edge table, graph traversal
- [x] Action layer: propose, validate, risk limits, executor, audit trail
- [ ] ProphetX market ingest (the arbbot recorder already has the client)
- [ ] A live venue adapter, once the ProphetX order API and key are confirmed
- [ ] Phase 2: news, pgvector, schema-constrained extraction

## Open questions

1. What exactly does Hermes store on Zen, and in what format? The ontology
   should follow that data.
2. Does the ProphetX key include an order API? Until then the executor stays
   in `dry_run`.
3. Does Zo count a self-hosted database as a service slot? (This only matters
   if Neo4j is ever wanted; nothing here needs it.)
4. Which LLM does Phase 2 extraction, and does it return structured output
   reliably?
5. ESPN's odds are DraftKings' and arrive without a timestamp of their own.
   `observed_at` is when we fetched them, so the stamp is honest, but a quote
   can be older than its stamp by up to the polling interval.
