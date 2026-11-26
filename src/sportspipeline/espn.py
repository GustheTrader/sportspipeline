"""ESPN public site API: fetch and parse.

Parsers are pure functions of the decoded JSON so they can be tested on
recorded responses. No key is needed. Endpoints used:

    /apis/site/v2/sports/{sport}/{league}/teams
    /apis/site/v2/sports/{sport}/{league}/scoreboard?dates=YYYYMMDD
    /apis/site/v2/sports/{sport}/{league}/injuries

Scoreboard odds come from the provider ESPN shows (currently DraftKings), as
an `open` and a `close` quote per side. For a game that has not started,
`close` is the live pregame quote, so it is stored as quote='current'. Once a
game has left 'scheduled', the quote is stored as 'close'; the schema refuses
to let a signal use it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import httpx

BASE = "https://site.api.espn.com/apis/site/v2/sports"
SOURCE = "espn"

Fetch = Callable[[str], dict]


class EspnError(RuntimeError):
    pass


def teams_url(league_path: str) -> str:
    return f"{BASE}/{league_path}/teams"


def scoreboard_url(league_path: str, dates: str) -> str:
    if not re.fullmatch(r"\d{8}(-\d{8})?", dates):
        raise ValueError(f"dates must be YYYYMMDD or YYYYMMDD-YYYYMMDD, got {dates!r}")
    return f"{BASE}/{league_path}/scoreboard?dates={dates}&limit=1000"


def injuries_url(league_path: str) -> str:
    return f"{BASE}/{league_path}/injuries"


def http_fetch(url: str, *, timeout: float = 30.0) -> dict:
    try:
        resp = httpx.get(url, timeout=timeout, headers={"User-Agent": "sportspipeline"})
    except httpx.HTTPError as exc:
        raise EspnError(f"GET {url} failed: {exc}") from exc
    if resp.status_code != 200:
        raise EspnError(f"GET {url} returned HTTP {resp.status_code}")
    try:
        doc = resp.json()
    except ValueError as exc:
        raise EspnError(f"GET {url} did not return JSON") from exc
    if not isinstance(doc, dict):
        raise EspnError(f"GET {url} returned {type(doc).__name__}, expected an object")
    return doc


def parse_time(value: str) -> datetime:
    # ESPN uses "2026-11-26T18:00Z" (no seconds) and sometimes full ISO.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_american(value: Any) -> int | None:
    if value is None:
        return None
    s = str(value).strip().upper()
    if s in ("EVEN", "EV"):
        return 100
    m = re.fullmatch(r"([+-]?)(\d+)", s)
    if not m:
        return None
    n = int(m.group(2))
    if n < 100:
        return None
    return -n if m.group(1) == "-" else n


def parse_line(value: Any) -> Decimal | None:
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("pk", "pick", "pick'em", "ev", "even"):
        return Decimal(0)
    s = s.lstrip("ou")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


_STATUS = {
    "STATUS_SCHEDULED": "scheduled",
    "STATUS_POSTPONED": "postponed",
    "STATUS_CANCELED": "canceled",
    "STATUS_CANCELLED": "canceled",
    "STATUS_FINAL": "final",
    "STATUS_FINAL_OT": "final",
    "STATUS_FINAL_SO": "final",
}


def map_status(status_type: dict) -> str:
    name = status_type.get("name", "")
    if name in _STATUS:
        return _STATUS[name]
    state = status_type.get("state")
    if state == "pre":
        return "scheduled"
    if state == "in":
        return "in_progress"
    if state == "post":
        return "final" if status_type.get("completed") else "canceled"
    raise EspnError(f"unknown ESPN status {name!r} / state {state!r}")


def venue_key(provider_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", provider_name.lower())


@dataclass(frozen=True)
class ParsedTeam:
    external_id: str
    name: str
    abbreviation: str | None
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ParsedQuote:
    kind: str       # moneyline | spread | total
    side: str       # home | away | over | under
    quote: str      # open | current | close
    line: Decimal | None
    american: int


@dataclass
class ParsedGame:
    external_id: str
    start_time: datetime
    status: str
    home_name: str
    home_external_id: str
    away_name: str
    away_external_id: str
    home_score: int | None
    away_score: int | None
    venue: str | None = None
    quotes: list[ParsedQuote] = field(default_factory=list)
    unparsed_quotes: int = 0


def parse_teams(doc: dict) -> list[ParsedTeam]:
    out = []
    for league in doc.get("sports", [{}])[0].get("leagues", []):
        for item in league.get("teams", []):
            t = item["team"]
            aliases = {t.get("displayName"), t.get("shortDisplayName"), t.get("abbreviation"),
                       t.get("name"), t.get("nickname")}
            if t.get("location") and t.get("name"):
                aliases.add(f"{t['location']} {t['name']}")
            out.append(ParsedTeam(
                external_id=str(t["id"]),
                name=t["displayName"],
                abbreviation=t.get("abbreviation"),
                aliases=tuple(sorted(a for a in aliases if a)),
            ))
    return out


def _score(competitor: dict) -> int | None:
    s = competitor.get("score")
    if s in (None, ""):
        return None
    try:
        return int(Decimal(str(s)))
    except InvalidOperation:
        return None


def _quotes(odds: dict, status: str) -> tuple[list[ParsedQuote], int]:
    quotes: list[ParsedQuote] = []
    bad = 0
    later = "current" if status == "scheduled" else "close"
    if status == "in_progress":
        # Pregame only: an in-play quote is neither a pregame price nor a close.
        later = None
    blocks = (
        ("moneyline", odds.get("moneyline") or {}, ("home", "away")),
        ("spread", odds.get("pointSpread") or {}, ("home", "away")),
        ("total", odds.get("total") or {}, ("over", "under")),
    )
    for kind, block, sides in blocks:
        for side in sides:
            for espn_key, label in (("open", "open"), ("close", later)):
                if label is None:
                    continue
                q = (block.get(side) or {}).get(espn_key)
                if not q:
                    continue
                american = parse_american(q.get("odds"))
                line = parse_line(q.get("line")) if kind != "moneyline" else None
                if american is None or (kind != "moneyline" and line is None):
                    bad += 1
                    continue
                quotes.append(ParsedQuote(kind, side, label, line, american))
    return quotes, bad


def parse_scoreboard(doc: dict) -> list[ParsedGame]:
    games = []
    for event in doc.get("events", []):
        comp = event["competitions"][0]
        status = map_status(comp["status"]["type"])
        sides = {c["homeAway"]: c for c in comp["competitors"]}
        if set(sides) != {"home", "away"}:
            raise EspnError(f"event {event['id']} does not have one home and one away competitor")
        home, away = sides["home"], sides["away"]
        game = ParsedGame(
            external_id=str(event["id"]),
            start_time=parse_time(comp.get("date") or event["date"]),
            status=status,
            home_name=home["team"]["displayName"],
            home_external_id=str(home["team"]["id"]),
            away_name=away["team"]["displayName"],
            away_external_id=str(away["team"]["id"]),
            home_score=_score(home) if status != "scheduled" else None,
            away_score=_score(away) if status != "scheduled" else None,
        )
        odds_list = comp.get("odds") or []
        if odds_list:
            odds = sorted(odds_list, key=lambda o: (o.get("provider") or {}).get("priority", 99))[0]
            provider = (odds.get("provider") or {}).get("name") or "unknown"
            game.venue = venue_key(provider)
            game.quotes, game.unparsed_quotes = _quotes(odds, status)
        games.append(game)
    return games


@dataclass(frozen=True)
class ParsedInjury:
    team_external_id: str
    team_name: str
    athlete_external_id: str
    athlete_name: str
    position: str | None
    status: str
    detail: str | None
    reported_at: datetime | None
    note_id: str


_ATHLETE_ID = re.compile(r"/id/(\d+)")


def parse_injuries(doc: dict) -> tuple[list[ParsedInjury], int]:
    """Returns (injuries, skipped) where skipped rows had no athlete id or name."""
    out = []
    skipped = 0
    for team in doc.get("injuries", []):
        for item in team.get("injuries", []):
            athlete = item.get("athlete") or {}
            athlete_id = None
            for link in athlete.get("links", []):
                m = _ATHLETE_ID.search(link.get("href", ""))
                if m:
                    athlete_id = m.group(1)
                    break
            if athlete_id is None or not athlete.get("displayName"):
                skipped += 1
                continue
            pos = (athlete.get("position") or {}).get("abbreviation")
            out.append(ParsedInjury(
                team_external_id=str(team["id"]),
                team_name=team.get("displayName", ""),
                athlete_external_id=athlete_id,
                athlete_name=athlete["displayName"],
                position=pos,
                status=item.get("status") or (item.get("type") or {}).get("description") or "unknown",
                detail=item.get("shortComment"),
                reported_at=parse_time(item["date"]) if item.get("date") else None,
                note_id=str(item.get("id")),
            ))
    return out, skipped
