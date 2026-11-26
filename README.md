# Sportspipeline

Sports data and betting-signal pipeline: ingest feeds, normalize teams and
markets, compute signals, grade them against official results, and keep an
append-only ledger of what was actually priced.

Early days — the design is in [`docs/DESIGN.md`](docs/DESIGN.md).

## Layout

```
docs/          design notes and decisions
src/           pipeline code
tests/         tests and fixtures
```

## Getting started

```
cp .env.example .env    # fill in keys
```

## Conventions

- Every ingest run is recorded, so a failure is visible instead of looking like
  "no data".
- Team and market names normalize through a table of aliases, not code
  branches.
- Signals carry their entry price and timestamp; grades come from official
  results.
- A signal whose team is not in the named game is rejected, never graded.
- Secrets live in the environment. `.env` is never committed.
