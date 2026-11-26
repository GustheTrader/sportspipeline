# Design

## Goal

One pipeline that turns sports data into graded signals, and keeps the evidence
needed to trust them later.

## Stages

1. **Ingest** — pull schedules, live and closing odds, and final results from
   upstream feeds. Every pull is recorded as a run with its source, window, and
   outcome, so an empty table can be told apart from a failed fetch.
2. **Normalize** — map vendor team names and market names onto our own
   identifiers. Normalization is a table, not code branches, so a new alias is a
   row.
3. **Signal** — compute signals from normalized inputs. A signal is only
   eligible for grading if it carries its entry price and the time it was
   stamped.
4. **Grade** — resolve each signal against official results. A pick whose team
   does not appear in the named game is rejected, never silently graded.
5. **Ledger** — append-only record of signals, prices, and grades. Reporting
   reads the ledger; nothing reports from live feeds.

## Non-negotiables

- No signal without a stamped price.
- No grade against a game the pick does not belong to.
- No schema reset in place of a migration.
- Secrets from the environment only.

## First milestones

- [ ] Store schema: teams, aliases, games, odds snapshots, signals, grades, runs
- [ ] One ingest adapter end to end, with idempotent re-runs
- [ ] Normalizer with an alias table and a test for unknown names
- [ ] Grader that rejects out-of-game picks
- [ ] Ledger export for backtesting
