from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest

from sportspipeline import grade, ingest, ledger, signals

from .conftest import fetched_at, fixture_fetch, game_id, market


def _bills_game_with_pregame_quotes(conn):
    """The real Bills 26-9 Dolphins final (Nov 22) with three pregame quotes.

    ESPN's scoreboard carries no odds once a game is final, so the quotes are
    written by hand here as test inputs (DraftKings' closing numbers for that
    game: BUF -1660, BUF -16.5 -110, total 45.5 -110), stamped an hour
    before kickoff.
    """
    ingest.ingest_scoreboard(conn, "nfl", "20261122", fetch=fixture_fetch("nfl_scoreboard_20261122"))
    gid = game_id(conn, "401873070")
    start = conn.execute("select start_time from kb.game where id = %s", (gid,)).fetchone()[0]
    stamp = start - timedelta(hours=1)
    ids = {}
    for kind, side, line, odds in (("moneyline", "home", None, -1660), ("spread", "home", Decimal("-16.5"), -110),
                                   ("total", "under", Decimal("45.5"), -110), ("total", "over", Decimal("45.5"), -110)):
        mid = conn.execute(
            "insert into kb.market (game_id, kind, venue) values (%s, %s, 'draftkings') "
            "on conflict (game_id, kind, venue) do update set venue = excluded.venue returning id",
            (gid, kind),
        ).fetchone()[0]
        sid = conn.execute(
            "insert into kb.price_snapshot (market_id, source, quote, side, line, american_odds, observed_at) "
            "values (%s, 'test', 'current', %s, %s, %s, %s) returning id",
            (mid, side, line, odds, stamp),
        ).fetchone()[0]
        ids[(kind, side)] = (mid, sid)
    conn.commit()
    return gid, ids


def test_signal_copies_price_and_stamp_from_snapshot(nfl):
    mid = market(nfl, "401873081", "moneyline")
    q = signals.latest_quote(nfl, mid, "home")
    sid = signals.pick(nfl, model="test", created_by="pytest", market_id=mid, side="home", model_prob=0.55)
    row = nfl.execute("select entry_american, stamped_at, team_id, line from kb.signal where id = %s", (sid,)).fetchone()
    lions = nfl.execute("select id from kb.team where name = 'Detroit Lions'").fetchone()[0]
    assert row == (-102, fetched_at("nfl_scoreboard_20261126"), lions, None)
    assert q.american == -102


def test_signal_cannot_override_its_price(nfl):
    mid = market(nfl, "401873081", "moneyline")
    q = signals.latest_quote(nfl, mid, "home")
    nfl.execute(
        "insert into kb.signal (model, created_by, game_id, market_id, side, price_snapshot_id, "
        "entry_american, entry_decimal, stamped_at) values ('t', 't', %s, %s, 'home', %s, 500, 6, now())",
        (q.game_id, mid, q.snapshot_id),
    )
    assert nfl.execute("select entry_american from kb.signal").fetchone()[0] == -102


def test_signal_without_snapshot_is_rejected(nfl):
    mid = market(nfl, "401873081", "moneyline")
    with pytest.raises(signals.SignalRejected) as exc:
        signals.create_signal(nfl, model="t", created_by="t", game_id=game_id(nfl, "401873081"),
                              market_id=mid, side="home", price_snapshot_id=999999)
    assert exc.value.code == signals.NO_STAMPED_PRICE
    assert nfl.execute("select count(*) from kb.signal_rejection").fetchone()[0] == 1


def test_quote_observed_after_kickoff_cannot_back_a_signal(nfl):
    # The recorded Lions price, observed one hour after kickoff.
    mid = market(nfl, "401873081", "moneyline")
    late = nfl.execute(
        "insert into kb.price_snapshot (market_id, source, quote, side, american_odds, observed_at) "
        "select %s, 'espn', 'current', 'home', -102, start_time + interval '1 hour' from kb.game where id = %s "
        "returning id", (mid, game_id(nfl, "401873081")),
    ).fetchone()[0]
    nfl.commit()
    with pytest.raises(signals.SignalRejected, match="not before game start") as exc:
        signals.create_signal(nfl, model="t", created_by="t", game_id=game_id(nfl, "401873081"),
                              market_id=mid, side="home", price_snapshot_id=late)
    assert exc.value.code == signals.NO_STAMPED_PRICE


def test_team_not_in_game_is_rejected_and_recorded(nfl):
    mid = market(nfl, "401873081", "moneyline")  # Bears at Lions
    eagles = nfl.execute("select id from kb.team where name = 'Philadelphia Eagles'").fetchone()[0]
    with pytest.raises(signals.SignalRejected) as exc:
        signals.pick(nfl, model="t", created_by="t", market_id=mid, side="home", team_id=eagles)
    assert exc.value.code == signals.TEAM_NOT_IN_GAME
    assert nfl.execute("select count(*) from kb.signal").fetchone()[0] == 0
    reason = nfl.execute("select reason from kb.signal_rejection").fetchone()[0]
    assert "not in game" in reason


def test_team_must_match_side(nfl):
    mid = market(nfl, "401873081", "moneyline")
    bears = nfl.execute("select id from kb.team where name = 'Chicago Bears'").fetchone()[0]
    with pytest.raises(signals.SignalRejected, match="not the home side"):
        signals.pick(nfl, model="t", created_by="t", market_id=mid, side="home", team_id=bears)


def test_snapshot_from_another_market_is_rejected(nfl):
    ml = market(nfl, "401873081", "moneyline")
    other = signals.latest_quote(nfl, market(nfl, "401873080", "moneyline"), "home")
    with pytest.raises(signals.SignalRejected):
        signals.create_signal(nfl, model="t", created_by="t", game_id=game_id(nfl, "401873081"),
                              market_id=ml, side="home", price_snapshot_id=other.snapshot_id)


def test_signals_are_append_only(nfl):
    sid = signals.pick(nfl, model="t", created_by="t", market_id=market(nfl, "401873081", "total"), side="over")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        nfl.execute("update kb.signal set model = 'x' where id = %s", (sid,))
    nfl.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        nfl.execute("delete from kb.signal where id = %s", (sid,))


@pytest.mark.parametrize("kind,side,line,home,away,expected", [
    ("moneyline", "home", None, 26, 9, "win"),
    ("moneyline", "away", None, 26, 9, "loss"),
    ("moneyline", "home", None, 20, 20, "push"),
    ("spread", "home", Decimal("-16.5"), 26, 9, "win"),
    ("spread", "home", Decimal("-17"), 26, 9, "push"),
    ("spread", "away", Decimal("16.5"), 26, 9, "loss"),
    ("spread", "away", Decimal("3.5"), 20, 23, "win"),
    ("total", "over", Decimal("45.5"), 26, 9, "loss"),
    ("total", "under", Decimal("45.5"), 26, 9, "win"),
    ("total", "over", Decimal("35"), 26, 9, "push"),
])
def test_outcome(kind, side, line, home, away, expected):
    assert grade.outcome(kind, side, line, home, away) == expected


def test_grading_real_final(nfl):
    gid, ids = _bills_game_with_pregame_quotes(nfl)
    s_ml = signals.pick(nfl, model="m", created_by="t", market_id=ids[("moneyline", "home")][0], side="home")
    s_sp = signals.pick(nfl, model="m", created_by="t", market_id=ids[("spread", "home")][0], side="home")
    s_un = signals.pick(nfl, model="m", created_by="t", market_id=ids[("total", "under")][0], side="under")
    s_ov = signals.pick(nfl, model="m", created_by="t", market_id=ids[("total", "over")][0], side="over")
    # A Thanksgiving signal: that game has not been played in the fixture.
    signals.pick(nfl, model="m", created_by="t", market_id=market(nfl, "401873081", "moneyline"), side="home")

    rep = grade.grade_pending(nfl)
    assert (rep.graded, rep.waiting, rep.voided, rep.rejected) == (4, 1, 0, [])
    rows = dict(nfl.execute("select signal_id, outcome from kb.grade").fetchall())
    assert rows == {s_ml: "win", s_sp: "win", s_un: "win", s_ov: "loss"}
    p = dict(nfl.execute("select signal_id, profit_units from kb.grade").fetchall())
    assert p[s_ml] == Decimal("0.0602")  # 100/1660
    assert p[s_sp] == Decimal("0.9091")
    assert p[s_ov] == Decimal("-1")
    # Entry price equals the last pregame quote, so closing line value is 0.
    assert nfl.execute("select clv from kb.grade where signal_id = %s", (s_ml,)).fetchone()[0] == 0

    again = grade.grade_pending(nfl)
    assert again.graded == 0


def test_db_refuses_grade_before_final(nfl):
    sid = signals.pick(nfl, model="m", created_by="t", market_id=market(nfl, "401873081", "moneyline"), side="home")
    with pytest.raises(psycopg.Error, match="not final"):
        nfl.execute("insert into kb.grade (signal_id, outcome, profit_units, result_source) "
                    "values (%s, 'win', 1, 'manual')", (sid,))


def test_grades_are_append_only(nfl):
    _, ids = _bills_game_with_pregame_quotes(nfl)
    signals.pick(nfl, model="m", created_by="t", market_id=ids[("moneyline", "home")][0], side="home")
    grade.grade_pending(nfl)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        nfl.execute("update kb.grade set outcome = 'loss'")


def test_canceled_game_voids(nfl):
    sid = signals.pick(nfl, model="m", created_by="t", market_id=market(nfl, "401873081", "moneyline"), side="away")
    nfl.execute("update kb.game set status = 'canceled' where id = %s", (game_id(nfl, "401873081"),))
    nfl.commit()
    rep = grade.grade_pending(nfl)
    assert rep.voided == 1
    assert nfl.execute("select outcome, profit_units from kb.grade where signal_id = %s", (sid,)).fetchone() == \
        ("void", 0)


def test_ledger_export(nfl, tmp_path):
    _, ids = _bills_game_with_pregame_quotes(nfl)
    signals.pick(nfl, model="m", created_by="t", market_id=ids[("moneyline", "home")][0], side="home")
    signals.pick(nfl, model="m", created_by="t", market_id=market(nfl, "401873081", "moneyline"), side="home")
    grade.grade_pending(nfl)
    out = tmp_path / "ledger.csv"
    assert ledger.export_csv(nfl, out) == 2
    lines = out.read_text().splitlines()
    assert lines[0].startswith("signal_id,created_at,model")
    assert "Buffalo Bills" in lines[1] and ",win," in lines[1]
    assert ledger.export_csv(nfl, out, graded_only=True) == 1
    summary = ledger.summary(nfl)
    assert summary[0][:6] == ("m", "moneyline", 2, 1, 1, 0)
