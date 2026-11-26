"""Ingest jobs. Every pull is recorded as a kb.ingest_run row.

The run row is committed before any work starts. The work runs in its own
transaction. If it raises, the work is rolled back and the run is marked
'failed' with the error, so a failed fetch never looks like "no data". A run
that read nothing is 'empty', which is different again.

Re-running an ingest over the same response writes nothing: games update only
when a field changed, and a price snapshot is written only when the quote
differs from the latest one stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import psycopg

from . import espn
from .normalize import Normalizer, seed_teams


@dataclass
class Counts:
    rows_seen: int = 0
    rows_written: int = 0
    unresolved: int = 0


@dataclass
class RunResult:
    run_id: int
    status: str
    rows_seen: int
    rows_written: int
    unresolved: int
    error: str | None = None


def league_path(conn: psycopg.Connection, league: str) -> str:
    row = conn.execute("select espn_path from kb.league where code = %s", (league,)).fetchone()
    if not row:
        raise LookupError(f"unknown league {league!r}")
    return row[0]


def run_ingest(conn: psycopg.Connection, *, source: str, kind: str, league: str | None,
               window: str | None, url: str | None, work: Callable[[int], Counts]) -> RunResult:
    run_id = conn.execute(
        "insert into kb.ingest_run (source, kind, league, window_label, request_url) "
        "values (%s, %s, %s, %s, %s) returning id",
        (source, kind, league, window, url),
    ).fetchone()[0]
    conn.commit()
    try:
        with conn.transaction():
            counts = work(run_id)
    except Exception as exc:  # noqa: BLE001 - recorded, then reported to the caller
        error = f"{type(exc).__name__}: {exc}"[:4000]
        conn.execute(
            "update kb.ingest_run set status = 'failed', finished_at = now(), error = %s where id = %s",
            (error, run_id),
        )
        conn.commit()
        return RunResult(run_id, "failed", 0, 0, 0, error)
    status = "empty" if counts.rows_seen == 0 else "ok"
    conn.execute(
        "update kb.ingest_run set status = %s, finished_at = now(), rows_seen = %s, "
        "rows_written = %s, unresolved = %s where id = %s",
        (status, counts.rows_seen, counts.rows_written, counts.unresolved, run_id),
    )
    conn.commit()
    return RunResult(run_id, status, counts.rows_seen, counts.rows_written, counts.unresolved)


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

def ingest_teams(conn: psycopg.Connection, league: str, *, fetch: espn.Fetch = espn.http_fetch) -> RunResult:
    url = espn.teams_url(league_path(conn, league))

    def work(run_id: int) -> Counts:
        teams = espn.parse_teams(fetch(url))
        report = seed_teams(conn, espn.SOURCE, league, teams)
        return Counts(rows_seen=len(teams),
                      rows_written=report.teams_created + report.aliases_added,
                      unresolved=len(report.ambiguous))

    return run_ingest(conn, source=espn.SOURCE, kind="teams", league=league, window=None, url=url, work=work)


# ---------------------------------------------------------------------------
# Scoreboard: games, results and quotes
# ---------------------------------------------------------------------------

def _upsert_game(cur: psycopg.Cursor, league: str, g: espn.ParsedGame, home_id: int, away_id: int) -> tuple[int, bool]:
    cur.execute(
        "select gm.id, gm.status, gm.start_time, gm.home_score, gm.away_score, "
        "gm.home_team_id, gm.away_team_id "
        "from kb.game_source s join kb.game gm on gm.id = s.game_id "
        "where s.source = %s and s.external_id = %s",
        (espn.SOURCE, g.external_id),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "insert into kb.game (league, home_team_id, away_team_id, start_time, status, home_score, away_score) "
            "values (%s, %s, %s, %s, %s, %s, %s) returning id",
            (league, home_id, away_id, g.start_time, g.status, g.home_score, g.away_score),
        )
        game_id = cur.fetchone()[0]
        cur.execute(
            "insert into kb.game_source (source, external_id, game_id) values (%s, %s, %s)",
            (espn.SOURCE, g.external_id, game_id),
        )
        return game_id, True

    game_id, status, start, hs, as_, h_id, a_id = row
    if (h_id, a_id) != (home_id, away_id):
        raise espn.EspnError(
            f"ESPN event {g.external_id} now lists different teams than game {game_id}; refusing to overwrite"
        )
    new_status, new_hs, new_as = g.status, g.home_score, g.away_score
    if status == "final" and new_status != "final":
        # A final result is never un-finalled by a later, staler response.
        new_status, new_hs, new_as = status, hs, as_
    if (new_status, g.start_time, new_hs, new_as) == (status, start, hs, as_):
        return game_id, False
    cur.execute(
        "update kb.game set status = %s, start_time = %s, home_score = %s, away_score = %s, "
        "updated_at = now() where id = %s",
        (new_status, g.start_time, new_hs, new_as, game_id),
    )
    return game_id, True


def _market_id(cur: psycopg.Cursor, game_id: int, kind: str, venue: str) -> int:
    cur.execute(
        "insert into kb.market (game_id, kind, venue) values (%s, %s, %s) "
        "on conflict (game_id, kind, venue) do nothing returning id",
        (game_id, kind, venue),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("select id from kb.market where game_id = %s and kind = %s and venue = %s", (game_id, kind, venue))
    return cur.fetchone()[0]


def _write_quote(cur: psycopg.Cursor, market_id: int, q: espn.ParsedQuote, observed_at: datetime, run_id: int) -> bool:
    cur.execute(
        "select american_odds, line from kb.price_snapshot "
        "where market_id = %s and source = %s and quote = %s and side = %s "
        "order by observed_at desc, id desc limit 1",
        (market_id, espn.SOURCE, q.quote, q.side),
    )
    last = cur.fetchone()
    if last is not None and last[0] == q.american and last[1] == q.line:
        return False
    cur.execute(
        "insert into kb.price_snapshot (market_id, source, quote, side, line, american_odds, observed_at, run_id) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s) on conflict do nothing returning id",
        (market_id, espn.SOURCE, q.quote, q.side, q.line, q.american, observed_at, run_id),
    )
    return cur.fetchone() is not None


def ingest_scoreboard(conn: psycopg.Connection, league: str, dates: str, *,
                      fetch: espn.Fetch = espn.http_fetch,
                      observed_at: datetime | None = None) -> RunResult:
    url = espn.scoreboard_url(league_path(conn, league), dates)

    def work(run_id: int) -> Counts:
        doc = fetch(url)
        seen_at = observed_at or datetime.now(timezone.utc)
        games = espn.parse_scoreboard(doc)
        norm = Normalizer(conn, espn.SOURCE, league)
        c = Counts(rows_seen=len(games))
        with conn.cursor() as cur:
            for g in games:
                home = norm.team(g.home_name, g.home_external_id, run_id=run_id)
                away = norm.team(g.away_name, g.away_external_id, run_id=run_id)
                if home is None or away is None:
                    c.unresolved += 1
                    continue
                game_id, changed = _upsert_game(cur, league, g, home, away)
                c.rows_written += int(changed)
                if g.venue and g.quotes:
                    for q in g.quotes:
                        mid = _market_id(cur, game_id, q.kind, g.venue)
                        c.rows_written += int(_write_quote(cur, mid, q, seen_at, run_id))
        return c

    return run_ingest(conn, source=espn.SOURCE, kind="scoreboard", league=league, window=dates, url=url, work=work)


# ---------------------------------------------------------------------------
# Injuries: time-bounded rows, closed when the status changes or the player
# drops off the report.
# ---------------------------------------------------------------------------

def _player_id(cur: psycopg.Cursor, league: str, inj: espn.ParsedInjury) -> int:
    cur.execute(
        "select player_id from kb.player_source where source = %s and external_id = %s",
        (espn.SOURCE, inj.athlete_external_id),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            "update kb.player set name = %s, position = coalesce(%s, position) "
            "where id = %s and (name, position) is distinct from (%s, coalesce(%s, position))",
            (inj.athlete_name, inj.position, row[0], inj.athlete_name, inj.position),
        )
        return row[0]
    cur.execute(
        "insert into kb.player (league, name, position) values (%s, %s, %s) returning id",
        (league, inj.athlete_name, inj.position),
    )
    pid = cur.fetchone()[0]
    cur.execute(
        "insert into kb.player_source (source, external_id, player_id) values (%s, %s, %s)",
        (espn.SOURCE, inj.athlete_external_id, pid),
    )
    return pid


def _plays_for(cur: psycopg.Cursor, player_id: int, team_id: int, at: datetime) -> bool:
    cur.execute("select team_id, valid_from from kb.player_team where player_id = %s and valid_to is null", (player_id,))
    row = cur.fetchone()
    if row and row[0] == team_id:
        return False
    if row:
        cur.execute(
            "update kb.player_team set valid_to = greatest(%s, valid_from + interval '1 microsecond') "
            "where player_id = %s and valid_to is null",
            (at, player_id),
        )
        cur.execute("select max(valid_to) from kb.player_team where player_id = %s", (player_id,))
        at = max(at, cur.fetchone()[0])
    cur.execute(
        "insert into kb.player_team (player_id, team_id, valid_from, source) values (%s, %s, %s, %s)",
        (player_id, team_id, at, espn.SOURCE),
    )
    return True


def ingest_injuries(conn: psycopg.Connection, league: str, *,
                    fetch: espn.Fetch = espn.http_fetch,
                    observed_at: datetime | None = None) -> RunResult:
    url = espn.injuries_url(league_path(conn, league))

    def work(run_id: int) -> Counts:
        doc = fetch(url)
        now = observed_at or datetime.now(timezone.utc)
        injuries, skipped = espn.parse_injuries(doc)
        norm = Normalizer(conn, espn.SOURCE, league)
        c = Counts(rows_seen=len(injuries), unresolved=skipped)
        if not injuries:
            return c  # an empty report closes nothing
        seen_players: set[int] = set()
        with conn.cursor() as cur:
            for inj in injuries:
                team_id = norm.team(inj.team_name, inj.team_external_id, run_id=run_id)
                if team_id is None:
                    c.unresolved += 1
                    continue
                pid = _player_id(cur, league, inj)
                seen_players.add(pid)
                c.rows_written += int(_plays_for(cur, pid, team_id, now))
                cur.execute(
                    "select id, status, detail, team_id, valid_from from kb.injury "
                    "where player_id = %s and source = %s and valid_to is null",
                    (pid, espn.SOURCE),
                )
                open_row = cur.fetchone()
                if open_row and (open_row[1], open_row[2], open_row[3]) == (inj.status, inj.detail, team_id):
                    continue
                valid_from = min(inj.reported_at or now, now)
                if open_row:
                    valid_from = max(valid_from, open_row[4])
                    cur.execute(
                        "update kb.injury set valid_to = %s, closed_run_id = %s where id = %s",
                        (valid_from, run_id, open_row[0]),
                    )
                cur.execute(
                    "insert into kb.injury (player_id, team_id, status, detail, reported_at, valid_from, "
                    "source, source_ref, run_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (pid, team_id, inj.status, inj.detail, inj.reported_at, valid_from,
                     espn.SOURCE, inj.note_id, run_id),
                )
                c.rows_written += 1
            # Players on an earlier report who are no longer listed: resolved.
            cur.execute(
                "update kb.injury i set valid_to = greatest(%s, i.valid_from), closed_run_id = %s "
                "from kb.player p where p.id = i.player_id and p.league = %s "
                "and i.source = %s and i.valid_to is null and not (i.player_id = any(%s))",
                (now, run_id, league, espn.SOURCE, list(seen_players)),
            )
            c.rows_written += cur.rowcount
        return c

    return run_ingest(conn, source=espn.SOURCE, kind="injuries", league=league, window=None, url=url, work=work)
