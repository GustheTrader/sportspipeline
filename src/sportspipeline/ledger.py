"""Ledger export for backtesting. Reads kb.ledger_v only, never live feeds."""

from __future__ import annotations

import csv
from pathlib import Path

import psycopg


def export_csv(conn: psycopg.Connection, path: Path, *, graded_only: bool = False) -> int:
    sql = "select * from kb.ledger_v"
    if graded_only:
        sql += " where outcome is not null"
    sql += " order by signal_id"
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
    return len(rows)


def summary(conn: psycopg.Connection) -> list[tuple]:
    return conn.execute(
        """
        select model, market_kind, count(*) as signals,
               count(outcome) filter (where outcome in ('win', 'loss', 'push')) as graded,
               count(*) filter (where outcome = 'win') as wins,
               count(*) filter (where outcome = 'loss') as losses,
               coalesce(sum(profit_units), 0) as profit_units,
               avg(clv) as avg_clv
        from kb.ledger_v group by model, market_kind order by model, market_kind
        """
    ).fetchall()
