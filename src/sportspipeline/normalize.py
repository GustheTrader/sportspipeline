"""Team normalisation through the alias table.

A vendor name maps to a team only through a row in kb.team_alias. There are no
code branches per spelling. An unknown name is written to kb.unresolved_name
and the caller gets None; nothing is guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import psycopg

from .espn import ParsedTeam


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


def id_alias(external_id: str) -> str:
    return f"id:{external_id}"


@dataclass
class SeedReport:
    teams_created: int = 0
    aliases_added: int = 0
    ambiguous: list[str] = field(default_factory=list)


def seed_teams(conn: psycopg.Connection, source: str, league: str, teams: list[ParsedTeam]) -> SeedReport:
    """Create canonical teams and their aliases from a vendor's team list.

    An alias that would point at two different teams in the same league is
    dropped for both and reported, so it shows up as unresolved instead of
    resolving to the wrong team.
    """
    report = SeedReport()
    claims: dict[str, set[str]] = {}
    for t in teams:
        for a in (*t.aliases, id_alias(t.external_id)):
            claims.setdefault(norm(a), set()).add(t.name)
    ambiguous = {a for a, owners in claims.items() if len(owners) > 1}
    report.ambiguous = sorted(ambiguous)

    with conn.cursor() as cur:
        for t in teams:
            cur.execute(
                "insert into kb.team (league, name, abbreviation) values (%s, %s, %s) "
                "on conflict (league, name) do nothing returning id",
                (league, t.name, t.abbreviation),
            )
            row = cur.fetchone()
            if row:
                report.teams_created += 1
                team_id = row[0]
            else:
                cur.execute("select id from kb.team where league = %s and name = %s", (league, t.name))
                team_id = cur.fetchone()[0]
            for a in sorted({norm(x) for x in (*t.aliases, id_alias(t.external_id))} - ambiguous):
                cur.execute(
                    "insert into kb.team_alias (source, league, alias, team_id) values (%s, %s, %s, %s) "
                    "on conflict (source, league, alias) do nothing returning team_id",
                    (source, league, a, team_id),
                )
                if cur.fetchone():
                    report.aliases_added += 1
                else:
                    cur.execute(
                        "select team_id from kb.team_alias where source = %s and league = %s and alias = %s",
                        (source, league, a),
                    )
                    existing = cur.fetchone()[0]
                    if existing != team_id:
                        report.ambiguous.append(a)
    return report


def add_alias(conn: psycopg.Connection, source: str, league: str, alias: str, team_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute("select id from kb.team where league = %s and name = %s", (league, team_name))
        row = cur.fetchone()
        if not row:
            raise LookupError(f"no team named {team_name!r} in {league}")
        cur.execute(
            "insert into kb.team_alias (source, league, alias, team_id) values (%s, %s, %s, %s)",
            (source, league, norm(alias), row[0]),
        )
        cur.execute(
            "delete from kb.unresolved_name where source = %s and league = %s and kind = 'team' "
            "and lower(raw_name) = %s",
            (source, league, norm(alias)),
        )
    return row[0]


class Normalizer:
    def __init__(self, conn: psycopg.Connection, source: str, league: str):
        self.conn = conn
        self.source = source
        self.league = league
        with conn.cursor() as cur:
            cur.execute(
                "select alias, team_id from kb.team_alias where source = %s and league = %s",
                (source, league),
            )
            self._aliases = dict(cur.fetchall())

    def team(self, name: str, external_id: str | None = None, *, run_id: int | None = None) -> int | None:
        if external_id is not None:
            hit = self._aliases.get(id_alias(external_id))
            if hit is not None:
                return hit
        hit = self._aliases.get(norm(name))
        if hit is not None:
            return hit
        self._record_unresolved("team", name, run_id)
        return None

    def _record_unresolved(self, kind: str, raw: str, run_id: int | None) -> None:
        self.conn.execute(
            """
            insert into kb.unresolved_name (source, league, kind, raw_name, last_run_id)
            values (%s, %s, %s, %s, %s)
            on conflict (source, league, kind, raw_name) do update
               set last_seen_at = now(),
                   seen_count = kb.unresolved_name.seen_count + 1,
                   last_run_id = excluded.last_run_id
            """,
            (self.source, self.league, kind, raw, run_id),
        )
