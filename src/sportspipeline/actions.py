"""Client for agents. An agent's connection can only call kb.propose_action,
kb.size_stake and kb.neighbors, and read the knowledge base. It cannot insert
into any table; that is enforced by Postgres grants, not by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class Decision:
    request_id: UUID
    status: str
    reason: str

    @property
    def approved(self) -> bool:
        return self.status in ("approved", "executed")


@dataclass(frozen=True)
class Sizing:
    stake: Decimal
    decimal_odds: Decimal
    edge: Decimal
    kelly: Decimal
    cap: Decimal | None


class AgentClient:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def propose(self, action: str, params: dict) -> Decision:
        with self.conn.transaction():
            row = self.conn.execute(
                "select request_id, status, reason from kb.propose_action(%s, %s)",
                (action, Jsonb(params)),
            ).fetchone()
        return Decision(*row)

    def size_stake(self, market_id: int, side: str, model_prob: float, bankroll: float,
                   kelly_fraction: float = 0.25) -> Sizing:
        row = self.conn.execute(
            "select * from kb.size_stake(%s::bigint, %s::text, %s::numeric, %s::numeric, %s::numeric)",
            (market_id, side, model_prob, bankroll, kelly_fraction),
        ).fetchone()
        self.conn.commit()
        return Sizing(*row)

    def checks(self, request_id: UUID) -> list[tuple]:
        rows = self.conn.execute(
            "select seq, check_name, passed, detail, risk_limit_id from kb.action_check "
            "where request_id = %s order by seq",
            (request_id,),
        ).fetchall()
        self.conn.commit()
        return rows

    def neighbors(self, obj_type: str, obj_id: str, depth: int = 2) -> list[tuple]:
        rows = self.conn.execute(
            "select * from kb.neighbors(%s, %s, %s)", (obj_type, obj_id, depth)
        ).fetchall()
        self.conn.commit()
        return rows
