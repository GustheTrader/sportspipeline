import gzip
import json

from sportspipeline import espn, ingest
from sportspipeline.normalize import Normalizer, add_alias

from .conftest import fetched_at, fixture_fetch, load


def count(conn, table, where="true", params=()):
    return conn.execute(f"select count(*) from {table} where {where}", params).fetchone()[0]


def test_team_seed_is_idempotent(owner):
    r1 = ingest.ingest_teams(owner, "nfl", fetch=fixture_fetch("nfl_teams"))
    assert (r1.status, r1.rows_seen) == ("ok", 32)
    assert count(owner, "kb.team") == 32
    r2 = ingest.ingest_teams(owner, "nfl", fetch=fixture_fetch("nfl_teams"))
    assert (r2.status, r2.rows_written) == ("ok", 0)
    n = Normalizer(owner, "espn", "nfl")
    bills = owner.execute("select id from kb.team where name = 'Buffalo Bills'").fetchone()[0]
    assert n.team("BUF") == n.team("buffalo  bills") == n.team("whatever", "2") == bills


def test_scoreboard_ingest_then_rerun_writes_nothing(nfl):
    assert count(nfl, "kb.game") == 3
    assert count(nfl, "kb.market") == 9
    assert count(nfl, "kb.price_snapshot") == 36
    run = nfl.execute("select status, rows_seen, rows_written from kb.ingest_run where kind = 'scoreboard'").fetchone()
    assert run == ("ok", 3, 3 + 36)
    again = ingest.ingest_scoreboard(nfl, "nfl", "20261126", fetch=fixture_fetch("nfl_scoreboard_20261126"),
                                     observed_at=fetched_at("nfl_scoreboard_20261126"))
    assert (again.status, again.rows_seen, again.rows_written) == ("ok", 3, 0)
    assert count(nfl, "kb.price_snapshot") == 36


def test_finals_record_results(nfl):
    r = ingest.ingest_scoreboard(nfl, "nfl", "20261122", fetch=fixture_fetch("nfl_scoreboard_20261122"))
    assert (r.status, r.rows_seen, r.rows_written, r.unresolved) == ("ok", 11, 11, 0)
    row = nfl.execute(
        "select g.status, g.home_score, g.away_score from kb.game g join kb.game_source s on s.game_id = g.id "
        "where s.external_id = '401873070'"
    ).fetchone()
    assert row == ("final", 26, 9)


def test_final_is_not_overwritten_by_a_stale_response(nfl):
    stale = load("nfl_scoreboard_20261126")
    gid = nfl.execute("select game_id from kb.game_source where external_id = '401873081'").fetchone()[0]
    nfl.execute("update kb.game set status = 'final', home_score = 30, away_score = 20 where id = %s", (gid,))
    nfl.commit()
    r = ingest.ingest_scoreboard(nfl, "nfl", "20261126", fetch=lambda url: stale,
                                 observed_at=fetched_at("nfl_scoreboard_20261126"))
    assert r.status == "ok"
    assert nfl.execute("select status, home_score from kb.game where id = %s", (gid,)).fetchone() == ("final", 30)


def test_unknown_names_are_recorded_not_guessed(owner):
    # NBA teams were never seeded, so every name is unknown.
    r = ingest.ingest_scoreboard(owner, "nba", "20261127", fetch=fixture_fetch("nba_scoreboard_20261127"))
    assert (r.status, r.rows_seen, r.rows_written, r.unresolved) == ("ok", 11, 0, 11)
    assert count(owner, "kb.game") == 0
    assert count(owner, "kb.unresolved_name", "league = 'nba' and kind = 'team'") == 22
    # Fix it with data, not code: seed the teams, and the same response loads.
    ingest.ingest_teams(owner, "nba", fetch=fixture_fetch("nba_teams"))
    r2 = ingest.ingest_scoreboard(owner, "nba", "20261127", fetch=fixture_fetch("nba_scoreboard_20261127"))
    assert (r2.unresolved, count(owner, "kb.game")) == (0, 11)


def test_alias_row_resolves_a_new_spelling(nfl):
    n = Normalizer(nfl, "rundown", "nfl")
    assert n.team("Buffalo Bills") is None  # no rundown aliases yet
    nfl.commit()
    assert count(nfl, "kb.unresolved_name", "source = 'rundown'") == 1
    add_alias(nfl, "rundown", "nfl", "Buffalo Bills", "Buffalo Bills")
    nfl.commit()
    assert Normalizer(nfl, "rundown", "nfl").team("Buffalo Bills") is not None
    assert count(nfl, "kb.unresolved_name", "source = 'rundown'") == 0


def test_failed_fetch_is_a_failed_run(owner):
    ingest.ingest_teams(owner, "nfl", fetch=fixture_fetch("nfl_teams"))

    def broken(url):
        raise espn.EspnError(f"GET {url} returned HTTP 503")

    r = ingest.ingest_scoreboard(owner, "nfl", "20261126", fetch=broken)
    assert r.status == "failed" and "503" in r.error
    row = owner.execute("select status, error, finished_at is not null from kb.ingest_run where id = %s",
                        (r.run_id,)).fetchone()
    assert row[0] == "failed" and "503" in row[1] and row[2]
    assert count(owner, "kb.game") == 0


def test_partial_failure_rolls_back_the_run(owner):
    ingest.ingest_teams(owner, "nfl", fetch=fixture_fetch("nfl_teams"))
    doc = load("nfl_scoreboard_20261126")
    doc["events"][2]["competitions"][0]["competitors"] = doc["events"][2]["competitions"][0]["competitors"][:1]
    r = ingest.ingest_scoreboard(owner, "nfl", "20261126", fetch=lambda url: doc)
    assert r.status == "failed"
    assert count(owner, "kb.game") == 0 and count(owner, "kb.price_snapshot") == 0


def test_empty_day_is_empty_not_failed(owner):
    ingest.ingest_teams(owner, "nfl", fetch=fixture_fetch("nfl_teams"))
    r = ingest.ingest_scoreboard(owner, "nfl", "20261124", fetch=fixture_fetch("nfl_scoreboard_20261124"))
    assert (r.status, r.rows_seen) == ("empty", 0)


def test_quote_change_writes_one_new_snapshot(nfl):
    doc = load("nfl_scoreboard_20261126")
    ml = doc["events"][0]["competitions"][0]["odds"][0]["moneyline"]
    ml["home"]["close"]["odds"] = "-120"
    later = fetched_at("nfl_scoreboard_20261126").replace(minute=59)
    r = ingest.ingest_scoreboard(nfl, "nfl", "20261126", fetch=lambda url: doc, observed_at=later)
    assert r.rows_written == 1
    prices = nfl.execute(
        "select american_odds from kb.price_snapshot s join kb.market m on m.id = s.market_id "
        "join kb.game_source gs on gs.game_id = m.game_id "
        "where gs.external_id = '401873081' and m.kind = 'moneyline' and s.side = 'home' and s.quote = 'current' "
        "order by observed_at"
    ).fetchall()
    assert prices == [(-102,), (-120,)]


def test_injuries_are_time_bounded(owner):
    ingest.ingest_teams(owner, "nfl", fetch=fixture_fetch("nfl_teams"))
    t0 = fetched_at("nfl_injuries")
    r = ingest.ingest_injuries(owner, "nfl", fetch=fixture_fetch("nfl_injuries"), observed_at=t0)
    assert (r.status, r.rows_seen, r.unresolved) == ("ok", 800, 0)
    open_rows = count(owner, "kb.injury", "valid_to is null")
    players = count(owner, "kb.player")
    assert open_rows == players  # one open status per listed player
    assert count(owner, "kb.player_team", "valid_to is null") == players

    again = ingest.ingest_injuries(owner, "nfl", fetch=fixture_fetch("nfl_injuries"), observed_at=t0)
    assert again.rows_written == 0

    # The same real report with Arizona's section removed: those players are
    # no longer listed, so their open rows close.
    doc = load("nfl_injuries")
    arizona = next(t for t in doc["injuries"] if t["id"] == "22")
    n_ari = len({i["athlete"]["links"][0]["href"] for i in arizona["injuries"]})
    doc["injuries"] = [t for t in doc["injuries"] if t["id"] != "22"]
    later = t0.replace(hour=t0.hour + 1)
    r3 = ingest.ingest_injuries(owner, "nfl", fetch=lambda url: doc, observed_at=later)
    assert r3.status == "ok"
    closed = count(owner, "kb.injury", "valid_to = %s", (later,))
    assert closed == n_ari
    assert count(owner, "kb.injury", "valid_to is null") == open_rows - n_ari


def test_injury_open_at_kickoff_affects_the_game(nfl):
    ingest.ingest_injuries(nfl, "nfl", fetch=fixture_fetch("nfl_injuries"),
                           observed_at=fetched_at("nfl_injuries"))
    lions = nfl.execute("select id from kb.team where name = 'Detroit Lions'").fetchone()[0]
    gid = nfl.execute("select game_id from kb.game_source where external_id = '401873081'").fetchone()[0]
    expected = count(nfl, "kb.injury", "team_id = %s and valid_from <= "
                     "(select start_time from kb.game where id = %s) and valid_to is null", (lions, gid))
    assert expected > 0
    got = count(nfl, "kb.injury_game_v", "game_id = %s and team_id = %s", (gid, lions))
    assert got == expected


def test_fixture_manifest_matches_files():
    import hashlib
    from .conftest import FIXTURES, MANIFEST

    for name, entry in MANIFEST.items():
        data = (FIXTURES / entry["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], name
        json.loads(gzip.decompress(data))
