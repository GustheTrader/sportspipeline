"""The executor: the only identity that acts on approved requests.

It connects as a login in the sp_executor group. It claims approved requests
(kb.claim_approved re-checks the kill switch and that the game has not
started), builds the venue request, and records what it did with
kb.record_execution.

Modes:
  dry_run  build the exact venue request and record it with status
           'not_sent'. Nothing leaves the machine. No fills are simulated.
  live     send it. Only allowed for a venue adapter that implements a real
           order API, and only when SPORTSPIPELINE_LIVE=1 is set. No adapter in
           this repository implements live orders yet (the ProphetX order API
           and key are still open), so live mode refuses to start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb


class LiveUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketInfo:
    market_id: int
    venue: str
    venue_market_id: str | None
    kind: str
    game_id: int
    league: str
    home_team: str
    away_team: str
    start_time: object


class VenueAdapter(Protocol):
    venue: str
    live_supported: bool

    def build_order(self, market: MarketInfo, params: dict) -> dict: ...

    def submit(self, payload: dict) -> tuple[str, str | None, dict]: ...

    def cancel(self, venue_order_id: str) -> dict: ...


class RecordOnlyAdapter:
    """Builds a venue-neutral order description. Has no order API."""

    live_supported = False

    def __init__(self, venue: str):
        self.venue = venue

    def build_order(self, market: MarketInfo, params: dict) -> dict:
        return {
            "venue": market.venue,
            "venue_market_id": market.venue_market_id,
            "market_id": market.market_id,
            "kind": market.kind,
            "game": f"{market.away_team} at {market.home_team}",
            "start_time": str(market.start_time),
            "side": params["side"],
            "line": params.get("line"),
            "limit_american": params["limit_american"],
            "stake": params["stake"],
        }

    def submit(self, payload: dict) -> tuple[str, str | None, dict]:
        raise LiveUnavailable(f"{self.venue}: no order API is implemented")

    def cancel(self, venue_order_id: str) -> dict:
        raise LiveUnavailable(f"{self.venue}: no order API is implemented")


@dataclass
class RunSummary:
    claimed: int = 0
    executed: int = 0
    failed: int = 0
    details: list[str] = field(default_factory=list)


class Executor:
    def __init__(self, conn: psycopg.Connection, *, mode: str = "dry_run",
                 adapters: dict[str, VenueAdapter] | None = None):
        if mode not in ("dry_run", "live"):
            raise ValueError("mode must be dry_run or live")
        self.conn = conn
        self.mode = mode
        self.adapters = adapters or {}
        if mode == "live":
            if os.environ.get("SPORTSPIPELINE_LIVE") != "1":
                raise LiveUnavailable("live mode needs SPORTSPIPELINE_LIVE=1")
            if not any(a.live_supported for a in self.adapters.values()):
                raise LiveUnavailable(
                    "no venue adapter with a live order API is configured; "
                    "orders stay approved and unclaimed"
                )

    def adapter(self, venue: str) -> VenueAdapter:
        return self.adapters.get(venue) or RecordOnlyAdapter(venue)

    def _market(self, market_id: int) -> MarketInfo:
        row = self.conn.execute(
            "select m.id, m.venue, m.venue_market_id, m.kind, g.id, g.league, ht.name, at.name, g.start_time "
            "from kb.market m join kb.game g on g.id = m.game_id "
            "join kb.team ht on ht.id = g.home_team_id join kb.team at on at.id = g.away_team_id "
            "where m.id = %s",
            (market_id,),
        ).fetchone()
        return MarketInfo(*row)

    def run_once(self, limit: int = 10) -> RunSummary:
        out = RunSummary()
        with self.conn.transaction():
            claimed = self.conn.execute(
                "select id, action, params from kb.claim_approved(%s)", (limit,)
            ).fetchall()
        out.claimed = len(claimed)
        for request_id, action, params in claimed:
            try:
                note = self._execute(request_id, action, params)
                out.executed += 1
                out.details.append(f"{request_id} {action}: {note}")
            except Exception as exc:  # noqa: BLE001 - recorded as a failed execution
                self.conn.rollback()
                with self.conn.transaction():
                    self.conn.execute(
                        "select kb.record_execution(%s, false, %s, null, null, null, null, null, %s)",
                        (request_id, self.mode, f"{type(exc).__name__}: {exc}"[:2000]),
                    )
                out.failed += 1
                out.details.append(f"{request_id} {action}: FAILED {exc}")
        return out

    def _execute(self, request_id, action: str, params: dict) -> str:
        if action == "place_order":
            market = self._market(int(params["market_id"]))
            adapter = self.adapter(market.venue)
            payload = adapter.build_order(market, params)
            if self.mode == "live" and adapter.live_supported:
                status, venue_order_id, response = adapter.submit(payload)
                mode = "live"
            else:
                status, venue_order_id, response, mode = "not_sent", None, None, "dry_run"
            with self.conn.transaction():
                order_id = self.conn.execute(
                    "select kb.record_execution(%s, true, %s, %s, %s, %s, %s, %s, %s)",
                    (request_id, mode, status, market.venue, venue_order_id, Jsonb(payload),
                     Jsonb(response) if response is not None else None, f"{mode}: {status}"),
                ).fetchone()[0]
            return f"order {order_id} {mode} {status}"
        if action == "cancel_order":
            order = self.conn.execute(
                "select id, mode, venue, venue_order_id from kb.order_ticket where id = %s",
                (int(params["order_id"]),),
            ).fetchone()
            response = None
            if order[1] == "live":
                response = self.adapter(order[2]).cancel(order[3])
            with self.conn.transaction():
                self.conn.execute(
                    "select kb.record_execution(%s, true, %s, null, %s, %s, null, %s, %s)",
                    (request_id, order[1], order[2], order[3],
                     Jsonb(response) if response is not None else None, f"cancel order {order[0]}"),
                )
            return f"order {order[0]} canceled"
        raise ValueError(f"executor does not handle {action}")
