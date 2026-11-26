"""Signals: picks stamped with the price they were made at.

The caller names a price snapshot. The database copies the entry price, line
and stamp from it, and refuses the signal if the snapshot is not a pregame
quote for that market and side, or if a named team is not in the game.
Every refusal is written to kb.signal_rejection, so rejected picks are
counted, not lost.

These are ledger writes: each function commits the connection, so neither a
signal nor a rejection can be undone by a caller's later rollback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

# SQLSTATEs raised by kb.signal_stamp (migration 0002).
NO_STAMPED_PRICE = "SP001"
TEAM_NOT_IN_GAME = "SP002"


class SignalRejected(Exception):
    def __init__(self, reason: str, code: str, rejection_id: int):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.rejection_id = rejection_id


@dataclass(frozen=True)
class Quote:
    snapshot_id: int
    market_id: int
    game_id: int
    kind: str
    side: str
    line: object
    american: int
    observed_at: object


def latest_quote(conn: psycopg.Connection, market_id: int, side: str) -> Quote | None:
    row = conn.execute(
        "select s.id, s.market_id, m.game_id, m.kind, s.side, s.line, s.american_odds, s.observed_at "
        "from kb.price_snapshot s join kb.market m on m.id = s.market_id "
        "where s.market_id = %s and s.side = %s and s.quote = 'current' "
        "order by s.observed_at desc, s.id desc limit 1",
        (market_id, side),
    ).fetchone()
    return Quote(*row) if row else None


def create_signal(conn: psycopg.Connection, *, model: str, created_by: str, game_id: int,
                  market_id: int, side: str, price_snapshot_id: int, team_id: int | None = None,
                  model_prob: float | None = None, stake_units: float = 1,
                  note: str | None = None) -> int:
    payload = {
        "model": model, "created_by": created_by, "game_id": game_id, "market_id": market_id,
        "side": side, "price_snapshot_id": price_snapshot_id, "team_id": team_id,
        "model_prob": model_prob, "stake_units": stake_units, "note": note,
    }
    conn.commit()
    try:
        with conn.transaction():
            row = conn.execute(
                "insert into kb.signal (model, created_by, game_id, market_id, side, team_id, "
                "price_snapshot_id, model_prob, stake_units, note, entry_american, entry_decimal, stamped_at) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, now()) returning id",
                (model, created_by, game_id, market_id, side, team_id, price_snapshot_id,
                 model_prob, stake_units, note),
            ).fetchone()
        return row[0]
    except psycopg.Error as exc:
        code = exc.sqlstate or ""
        if code not in (NO_STAMPED_PRICE, TEAM_NOT_IN_GAME):
            raise
        reason = exc.diag.message_primary or str(exc)
        with conn.transaction():
            rid = conn.execute(
                "insert into kb.signal_rejection (reason, payload) values (%s, %s) returning id",
                (f"{code}: {reason}", Jsonb(json.loads(json.dumps(payload, default=str)))),
            ).fetchone()[0]
        raise SignalRejected(reason, code, rid) from exc


def pick(conn: psycopg.Connection, *, model: str, created_by: str, market_id: int, side: str,
         team_id: int | None = None, model_prob: float | None = None, stake_units: float = 1,
         note: str | None = None) -> int:
    """Signal at the latest pregame quote for a market side."""
    q = latest_quote(conn, market_id, side)
    conn.commit()
    if q is None:
        payload = {"model": model, "created_by": created_by, "market_id": market_id, "side": side}
        with conn.transaction():
            rid = conn.execute(
                "insert into kb.signal_rejection (reason, payload) values (%s, %s) returning id",
                (f"{NO_STAMPED_PRICE}: no pregame quote for market {market_id} side {side}", Jsonb(payload)),
            ).fetchone()[0]
        raise SignalRejected(f"no pregame quote for market {market_id} side {side}", NO_STAMPED_PRICE, rid)
    return create_signal(conn, model=model, created_by=created_by, game_id=q.game_id, market_id=market_id,
                         side=side, price_snapshot_id=q.snapshot_id, team_id=team_id,
                         model_prob=model_prob, stake_units=stake_units, note=note)
