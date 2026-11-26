from datetime import datetime, timezone
from decimal import Decimal

import pytest

from sportspipeline import espn

from .conftest import load


@pytest.mark.parametrize("raw,expected", [
    ("-110", -110), ("+424", 424), ("424", 424), ("EVEN", 100), ("even", 100),
    ("OFF", None), ("", None), (None, None), ("+50", None), ("-99", None),
])
def test_parse_american(raw, expected):
    assert espn.parse_american(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("-10", Decimal("-10")), ("+6.5", Decimal("6.5")), ("o49.5", Decimal("49.5")),
    ("u48.5", Decimal("48.5")), ("PK", Decimal(0)), ("bogus", None), (None, None),
])
def test_parse_line(raw, expected):
    assert espn.parse_line(raw) == expected


def test_teams_have_id_name_and_aliases():
    nfl = espn.parse_teams(load("nfl_teams"))
    nba = espn.parse_teams(load("nba_teams"))
    assert len(nfl) == 32 and len(nba) == 30
    bills = next(t for t in nfl if t.name == "Buffalo Bills")
    assert bills.external_id == "2"
    assert {"BUF", "Bills", "Buffalo Bills"} <= set(bills.aliases)


def test_thanksgiving_pregame_quotes():
    games = espn.parse_scoreboard(load("nfl_scoreboard_20261126"))
    assert [g.external_id for g in games] == ["401873081", "401873080", "401873082"]
    lions = games[0]
    assert (lions.home_name, lions.away_name) == ("Detroit Lions", "Chicago Bears")
    assert lions.start_time == datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc)
    assert lions.status == "scheduled" and lions.venue == "draftkings"
    assert lions.home_score is None and lions.away_score is None
    q = {(x.kind, x.side, x.quote): (x.line, x.american) for x in lions.quotes}
    assert len(q) == 12
    assert q[("moneyline", "home", "current")] == (None, -102)
    assert q[("moneyline", "away", "open")] == (None, 114)
    assert q[("spread", "home", "current")] == (Decimal("1.5"), -113)
    assert q[("total", "under", "current")] == (Decimal("55.5"), -107)


def test_finals_have_scores_and_no_pregame_quotes():
    games = {g.external_id: g for g in espn.parse_scoreboard(load("nfl_scoreboard_20261122"))}
    assert len(games) == 11
    bills = games["401873070"]
    assert (bills.home_name, bills.home_score, bills.away_name, bills.away_score) == \
        ("Buffalo Bills", 26, "Miami Dolphins", 9)
    assert all(g.status == "final" for g in games.values())
    assert all(q.quote != "current" for g in games.values() for q in g.quotes)


def test_in_play_quotes_are_not_kept():
    doc = load("nfl_scoreboard_20261126")
    comp = doc["events"][0]["competitions"][0]
    comp["status"]["type"] = {"name": "STATUS_IN_PROGRESS", "state": "in", "completed": False}
    game = espn.parse_scoreboard(doc)[0]
    assert game.status == "in_progress"
    assert {q.quote for q in game.quotes} == {"open"}


def test_empty_date():
    assert espn.parse_scoreboard(load("nfl_scoreboard_20261124")) == []


def test_injuries():
    rows, skipped = espn.parse_injuries(load("nfl_injuries"))
    assert len(rows) == 800 and skipped == 0
    slovis = next(r for r in rows if r.athlete_name == "Kedon Slovis")
    assert slovis.athlete_external_id == "4428512"
    assert slovis.team_external_id == "22" and slovis.position == "QB"
    statuses = {r.status for r in rows}
    assert {"Out", "Questionable", "Injured Reserve"} <= statuses


def test_scoreboard_url_validates_dates():
    with pytest.raises(ValueError):
        espn.scoreboard_url("football/nfl", "2026-11-26")
