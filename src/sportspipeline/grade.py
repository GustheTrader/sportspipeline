"""Grade signals against official results.

Results come from kb.game rows that an ingest run marked 'final'. A signal
whose team is not in its game is rejected (written to kb.signal_rejection)
and never graded; the database also refuses such a grade on its own.
Canceled games grade as 'void'. Postponed games wait.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb


@dataclass
class GradeReport:
    graded: int = 0
    voided: int = 0
    rejected: list[int] = field(default_factory=list)
    waiting: int = 0


def outcome(kind: str, side: str, line: Decimal | None, home: int, away: int) -> str:
    if kind == "total":
        if line is None:
            raise ValueError("a total needs a line")
        total = Decimal(home + away)
        if total == line:
            return "push"
        return "win" if (total > line) == (side == "over") else "loss"
    mine, theirs = (home, away) if side == "home" else (away, home)
    margin = Decimal(mine - theirs)
    if kind == "spread":
        if line is None:
            raise ValueError("a spread needs a line")
        margin += line
    if margin == 0:
        return "push"
    return "win" if margin > 0 else "loss"


def profit(result: str, entry_decimal: Decimal, stake: Decimal) -> Decimal:
    if result == "win":
        return (stake * (entry_decimal - 1)).quantize(Decimal("0.0001"))
    if result == "loss":
        return -stake
    return Decimal(0)


def _closing_quote(cur: psycopg.Cursor, market_id: int, side: str, start_time) -> tuple | None:
    # Prefer the last pregame quote we stamped ourselves; fall back to the
    # feed's reported close.
    cur.execute(
        "select id, line, decimal_odds from kb.price_snapshot "
        "where market_id = %s and side = %s and quote = 'current' and observed_at < %s "
        "order by observed_at desc, id desc limit 1",
        (market_id, side, start_time),
    )
    row = cur.fetchone()
    if row:
        return row
    cur.execute(
        "select id, line, decimal_odds from kb.price_snapshot "
        "where market_id = %s and side = %s and quote = 'close' "
        "order by observed_at asc, id asc limit 1",
        (market_id, side),
    )
    return cur.fetchone()


def grade_pending(conn: psycopg.Connection, *, result_source: str = "espn") -> GradeReport:
    report = GradeReport()
    conn.commit()  # grades are ledger writes; never nest them in a caller's transaction
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            select s.id, s.side, s.team_id, s.line, s.entry_decimal, s.stake_units, s.market_id,
                   m.kind, g.id, g.status, g.home_team_id, g.away_team_id, g.home_score, g.away_score,
                   g.start_time
            from kb.signal s
            join kb.market m on m.id = s.market_id
            join kb.game g on g.id = s.game_id
            where not exists (select 1 from kb.grade gr where gr.signal_id = s.id)
              and not exists (select 1 from kb.signal_rejection r
                              where r.payload ->> 'signal_id' = s.id::text)
            order by s.id
            """
        )
        for (sid, side, team_id, line, entry_dec, stake, market_id, kind, game_id, status,
             home_id, away_id, hs, as_, start) in cur.fetchall():
            if team_id is not None and team_id not in (home_id, away_id):
                cur.execute(
                    "insert into kb.signal_rejection (reason, payload) values (%s, %s)",
                    (f"SP002: team {team_id} is not in game {game_id}; not graded",
                     Jsonb({"signal_id": sid, "team_id": team_id, "game_id": game_id})),
                )
                report.rejected.append(sid)
                continue
            if status == "canceled":
                cur.execute(
                    "insert into kb.grade (signal_id, outcome, profit_units, result_source) "
                    "values (%s, 'void', 0, %s)",
                    (sid, result_source),
                )
                report.voided += 1
                continue
            if status != "final":
                report.waiting += 1
                continue
            res = outcome(kind, side, line, hs, as_)
            close = _closing_quote(cur, market_id, side, start)
            close_id = clv = None
            if close is not None:
                close_id, close_line, close_dec = close
                if kind == "moneyline" or close_line == line:
                    clv = (Decimal(entry_dec) / Decimal(close_dec) - 1).quantize(Decimal("0.0001"))
            cur.execute(
                "insert into kb.grade (signal_id, outcome, home_score, away_score, profit_units, "
                "close_snapshot_id, clv, result_source) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (sid, res, hs, as_, profit(res, Decimal(entry_dec), Decimal(stake)), close_id, clv, result_source),
            )
            report.graded += 1
    return report
