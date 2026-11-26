"""The kinetic layer: agents propose, the database decides, the executor acts.

All prices, lines and teams are the recorded Thanksgiving 2026 DraftKings
quotes. `shift_to_upcoming` only moves wall-clock times so pregame and
price-age checks can run on any date.
"""

from decimal import Decimal

import psycopg
import pytest

from sportspipeline import signals
from sportspipeline.actions import AgentClient
from sportspipeline.executor import Executor, LiveUnavailable

from .conftest import market, shift_to_upcoming

LIONS = "401873081"  # Bears at Lions: ML home -102 / away -116, spread home +1.5 -113, total 55.5


@pytest.fixture
def board(nfl):
    shift_to_upcoming(nfl, LIONS)
    return nfl


def human_limit(conn_as, scope="global", key="*", max_stake=50, max_daily=100, max_age=3600, kill=False,
                reason="test limit"):
    with conn_as("jeff") as c:
        return c.execute("select kb.set_risk_limit_human(%s, %s, %s, %s, %s, %s, %s)",
                         (scope, key, max_stake, max_daily, max_age, kill, reason)).fetchone()[0]


def order(mid, side="home", stake=20, limit=-110, **extra):
    return {"market_id": mid, "side": side, "stake": stake, "limit_american": limit, **extra}


def failed_check(client, decision):
    return [name for _, name, passed, _, _ in client.checks(decision.request_id) if not passed]


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "insert into kb.order_ticket (request_id, market_id, side, limit_american, stake, venue, mode, status, "
    "request_payload) values (gen_random_uuid(), 1, 'home', -110, 1, 'x', 'dry_run', 'not_sent', '{}')",
    "insert into kb.action_request (action, params, requested_by, status, decision_reason) "
    "values ('place_order', '{}', 'hermes', 'approved', 'trust me')",
    "insert into kb.risk_limit (scope, max_stake, set_by) values ('global', 1000000, 'hermes')",
    "update kb.risk_limit set kill_switch = false",
    "insert into kb.price_snapshot (market_id, source, quote, side, american_odds, observed_at) "
    "values (1, 'x', 'current', 'home', 1000, now())",
    "select * from kb.claim_approved(10)",
    "select kb.set_risk_limit_human('global', '*', 1e9, 1e9, 3600, false, 'mine now')",
])
def test_agent_cannot_write_or_escalate(board, connect_as, sql):
    with connect_as("hermes") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(sql)


def test_executor_cannot_propose_or_raise_limits(board, connect_as):
    with connect_as("executor") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select * from kb.propose_action('place_order', '{}')")
    with connect_as("executor") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select kb.set_risk_limit_human('global', '*', 1, 1, 1, false, 'x')")


def test_unregistered_login_gets_nothing(board, connect_as):
    with connect_as("stranger") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select * from kb.propose_action('place_order', '{}')")


def test_inactive_agent_cannot_propose(board, connect_as):
    board.execute("update kb.agent set active = false where id = 'gnoesis'")
    board.commit()
    with connect_as("gnoesis") as c:
        with pytest.raises(psycopg.Error, match="no active agent"):
            AgentClient(c).propose("place_order", order(1))


def test_action_requests_are_immutable(board, connect_as):
    human_limit(connect_as)
    with connect_as("hermes") as c:
        d = AgentClient(c).propose("place_order", order(market(board, LIONS, "moneyline")))
    with pytest.raises(psycopg.errors.RaiseException, match="only status"):
        board.execute("update kb.action_request set params = '{}' where id = %s", (d.request_id,))
    board.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        board.execute("delete from kb.action_request where id = %s", (d.request_id,))
    board.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="not an allowed transition"):
        board.execute("update kb.action_request set status = 'executed' where id = %s", (d.request_id,))


# ---------------------------------------------------------------------------
# place_order checks
# ---------------------------------------------------------------------------

def test_no_global_limit_means_no_orders(board, connect_as):
    with connect_as("hermes") as c:
        client = AgentClient(c)
        d = client.propose("place_order", order(market(board, LIONS, "moneyline")))
        assert d.status == "rejected"
        assert failed_check(client, d) == ["global_limit_exists"]


def test_order_within_limits_is_approved_and_audited(board, connect_as):
    lid = human_limit(connect_as)
    with connect_as("hermes") as c:
        client = AgentClient(c)
        d = client.propose("place_order", order(market(board, LIONS, "moneyline"), limit=-110))
        assert (d.status, d.reason) == ("approved", "all checks passed")
        checks = client.checks(d.request_id)
    names = [n for _, n, _, _, _ in checks]
    assert names[:2] == ["role", "param_schema"]
    assert {"kill_switch", "max_stake", "daily_exposure", "price_fresh", "price_within_limit"} <= set(names)
    assert all(p for _, _, p, _, _ in checks)
    assert any(limit_id == lid for *_, limit_id in checks)  # GOVERNED_BY


@pytest.mark.parametrize("params,check", [
    ({"stake": 51}, "max_stake"),
    ({"limit": -100}, "price_within_limit"),          # current is -102; -100 is a better price than offered
    ({"side": "over"}, "side_valid"),
])
def test_order_check_failures(board, connect_as, params, check):
    human_limit(connect_as)
    p = order(market(board, LIONS, "moneyline"), side=params.get("side", "home"),
              stake=params.get("stake", 20), limit=params.get("limit", -110))
    with connect_as("hermes") as c:
        client = AgentClient(c)
        d = client.propose("place_order", p)
        assert d.status == "rejected"
        assert check in failed_check(client, d)


@pytest.mark.parametrize("params", [
    {"market_id": 1, "side": "home", "stake": 20},                                       # no limit price
    {"market_id": 1, "side": "home", "stake": -5, "limit_american": -110},
    {"market_id": 1, "side": "home", "stake": 5, "limit_american": 50},                  # not American odds
    {"market_id": 1, "side": "home", "stake": 5, "limit_american": -110, "account": "x"},  # extra field
])
def test_params_must_match_schema(board, connect_as, params):
    human_limit(connect_as)
    with connect_as("hermes") as c:
        client = AgentClient(c)
        d = client.propose("place_order", params)
        assert d.status == "rejected"
        assert failed_check(client, d) == ["param_schema"]


def test_daily_exposure_is_cumulative(board, connect_as):
    human_limit(connect_as, max_stake=50, max_daily=100)
    mid = market(board, LIONS, "moneyline")
    with connect_as("hermes") as c:
        client = AgentClient(c)
        assert client.propose("place_order", order(mid, stake=40)).status == "approved"
        assert client.propose("place_order", order(mid, stake=40)).status == "approved"
        d = client.propose("place_order", order(mid, stake=40))
        assert d.status == "rejected"
        assert failed_check(client, d) == ["daily_exposure"]
    # Rejected requests do not count against the budget.
    with connect_as("gnoesis") as c:
        assert AgentClient(c).propose("place_order", order(mid, stake=20)).status == "approved"


def test_agent_scope_limit_applies_to_that_agent_only(board, connect_as):
    human_limit(connect_as)
    human_limit(connect_as, scope="agent", key="hermes", max_stake=5, max_daily=None, max_age=None)
    mid = market(board, LIONS, "moneyline")
    with connect_as("hermes") as c:
        assert AgentClient(c).propose("place_order", order(mid, stake=10)).status == "rejected"
    with connect_as("gnoesis") as c:
        assert AgentClient(c).propose("place_order", order(mid, stake=10)).status == "approved"


def test_stale_price_is_rejected(nfl, connect_as):
    shift_to_upcoming(nfl, LIONS, quotes_age="10 minutes")
    human_limit(connect_as, max_age=60)
    with connect_as("hermes") as c:
        client = AgentClient(c)
        d = client.propose("place_order", order(market(nfl, LIONS, "moneyline")))
        assert failed_check(client, d) == ["price_fresh"]


def test_spread_order_needs_the_current_line(board, connect_as):
    human_limit(connect_as)
    mid = market(board, LIONS, "spread")
    with connect_as("hermes") as c:
        client = AgentClient(c)
        moved = client.propose("place_order", order(mid, limit=-120, line=2.5))
        assert failed_check(client, moved) == ["line_unchanged"]
        missing = client.propose("place_order", order(mid, limit=-120))
        assert failed_check(client, missing) == ["line_unchanged"]
        ok = client.propose("place_order", order(mid, limit=-120, line=1.5))
        assert ok.status == "approved"


def test_started_game_is_closed(nfl, connect_as):
    shift_to_upcoming(nfl, LIONS, starts_in="-5 minutes")
    human_limit(connect_as)
    with connect_as("hermes") as c:
        client = AgentClient(c)
        d = client.propose("place_order", order(market(nfl, LIONS, "moneyline")))
        assert "market_open" in failed_check(client, d)


def test_unknown_market(board, connect_as):
    human_limit(connect_as)
    with connect_as("hermes") as c:
        client = AgentClient(c)
        d = client.propose("place_order", order(999999))
        assert failed_check(client, d) == ["market_exists"]


def test_risk_officer_cannot_place_orders(board, connect_as):
    human_limit(connect_as)
    with connect_as("risk_officer") as c:
        client = AgentClient(c)
        d = client.propose("place_order", order(market(board, LIONS, "moneyline")))
        assert failed_check(client, d) == ["role"]


# ---------------------------------------------------------------------------
# Limits: agents and the risk officer may only tighten
# ---------------------------------------------------------------------------

def test_kill_switch_engage_and_release(board, connect_as):
    human_limit(connect_as)
    mid = market(board, LIONS, "moneyline")
    with connect_as("risk_officer") as c:
        d = AgentClient(c).propose("set_risk_limit", {"scope": "global", "kill_switch": True,
                                                       "rationale": "feed looks wrong"})
        assert d.status == "executed"
    with connect_as("hermes") as c:
        client = AgentClient(c)
        d = client.propose("place_order", order(mid))
        assert failed_check(client, d) == ["kill_switch"]
    with connect_as("risk_officer") as c:
        client = AgentClient(c)
        d = client.propose("set_risk_limit", {"scope": "global", "kill_switch": False})
        assert d.status == "rejected" and failed_check(client, d) == ["param_schema"]
        # tightening something else must not silently release the switch
        assert client.propose("set_risk_limit", {"scope": "global", "max_stake": 40}).status == "executed"
    cur = board.execute("select kill_switch, max_stake, set_by from kb.risk_limit_current where scope = 'global'")
    assert cur.fetchone() == (True, Decimal("40"), "risk_officer")
    board.commit()
    human_limit(connect_as, max_stake=50, kill=False, reason="checked the feed")
    with connect_as("hermes") as c:
        assert AgentClient(c).propose("place_order", order(mid)).status == "approved"


def test_agents_cannot_loosen_limits(board, connect_as):
    human_limit(connect_as, max_stake=50, max_daily=100, max_age=600)
    with connect_as("hermes") as c:
        client = AgentClient(c)
        for field, value in (("max_stake", 51), ("max_daily_exposure", 1000), ("max_price_age_s", 601)):
            d = client.propose("set_risk_limit", {"scope": "global", field: value})
            assert d.status == "rejected", field
            assert failed_check(client, d) == [f"tightens_{field}"]
        d = client.propose("set_risk_limit", {"scope": "global", "max_stake": 10})
        assert d.status == "executed"
    assert board.execute("select max_stake, max_daily_exposure from kb.risk_limit_current "
                         "where scope = 'global'").fetchone() == (Decimal("10"), Decimal("100"))


def test_a_new_scope_row_cannot_loosen_the_global_limit(board, connect_as):
    human_limit(connect_as, max_stake=50)
    with connect_as("hermes") as c:
        client = AgentClient(c)
        assert client.propose("set_risk_limit", {"scope": "league", "scope_key": "nfl",
                                                 "max_stake": 1000}).status == "executed"
        d = client.propose("place_order", order(market(board, LIONS, "moneyline"), stake=60))
        assert d.status == "rejected" and "max_stake" in failed_check(client, d)


def test_human_function_needs_a_reason(board, connect_as):
    with connect_as("jeff") as c:
        with pytest.raises(psycopg.Error, match="reason"):
            c.execute("select kb.set_risk_limit_human('global', '*', 1, 1, 1, false, '')")


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

def test_executor_dry_run_records_order(board, connect_as):
    human_limit(connect_as)
    mid = market(board, LIONS, "moneyline")
    with connect_as("hermes") as c:
        d = AgentClient(c).propose("place_order", order(mid, stake=25, limit=-110))
    with connect_as("executor") as c:
        summary = Executor(c).run_once()
    assert (summary.claimed, summary.executed, summary.failed) == (1, 1, 0)
    o = board.execute("select mode, status, stake, venue, request_payload from kb.order_ticket "
                      "where request_id = %s", (d.request_id,)).fetchone()
    assert o[:4] == ("dry_run", "not_sent", Decimal("25"), "draftkings")
    assert o[4]["game"] == "Chicago Bears at Detroit Lions" and o[4]["limit_american"] == -110
    trail = board.execute("select from_status, to_status, actor from kb.action_event where request_id = %s "
                          "order by id", (d.request_id,)).fetchall()
    assert trail == [(None, "approved", "hermes"), ("approved", "claimed", "executor"),
                     ("claimed", "executed", "executor")]
    with connect_as("executor") as c:
        assert Executor(c).run_once().claimed == 0


def test_informational_actions_are_not_claimed(board, connect_as):
    with connect_as("hermes") as c:
        d = AgentClient(c).propose("propose_edge", {"market_id": market(board, LIONS, "moneyline"),
                                                    "side": "home", "model_prob": 0.56,
                                                    "source": "hermes-v1", "confidence": 0.6})
        assert d.status == "approved"
    with connect_as("executor") as c:
        assert Executor(c).run_once().claimed == 0


def test_kill_switch_after_approval_expires_the_request(board, connect_as):
    human_limit(connect_as)
    with connect_as("hermes") as c:
        d = AgentClient(c).propose("place_order", order(market(board, LIONS, "moneyline")))
        assert d.status == "approved"
    with connect_as("risk_officer") as c:
        AgentClient(c).propose("set_risk_limit", {"scope": "league", "scope_key": "nfl", "kill_switch": True})
    with connect_as("executor") as c:
        assert Executor(c).run_once().claimed == 0
    row = board.execute("select status from kb.action_request where id = %s", (d.request_id,)).fetchone()
    assert row == ("expired",)
    note = board.execute("select note from kb.action_event where request_id = %s and to_status = 'expired'",
                         (d.request_id,)).fetchone()[0]
    assert "kill switch" in note
    assert board.execute("select count(*) from kb.order_ticket").fetchone()[0] == 0


def test_live_mode_refuses_without_a_live_venue(board, connect_as, monkeypatch):
    with connect_as("executor") as c:
        with pytest.raises(LiveUnavailable, match="SPORTSPIPELINE_LIVE"):
            Executor(c, mode="live")
        monkeypatch.setenv("SPORTSPIPELINE_LIVE", "1")
        with pytest.raises(LiveUnavailable, match="no venue adapter"):
            Executor(c, mode="live")


def test_cancel_own_order_only(board, connect_as):
    human_limit(connect_as)
    with connect_as("hermes") as c:
        AgentClient(c).propose("place_order", order(market(board, LIONS, "moneyline")))
    with connect_as("executor") as c:
        Executor(c).run_once()
    oid = board.execute("select id from kb.order_ticket").fetchone()[0]
    board.commit()
    with connect_as("gnoesis") as c:
        client = AgentClient(c)
        d = client.propose("cancel_order", {"order_id": oid})
        assert failed_check(client, d) == ["order_owner"]
    with connect_as("hermes") as c:
        assert AgentClient(c).propose("cancel_order", {"order_id": oid}).status == "approved"
    with connect_as("executor") as c:
        assert Executor(c).run_once().executed == 1
    assert board.execute("select status from kb.order_ticket where id = %s", (oid,)).fetchone() == ("canceled",)
    with connect_as("hermes") as c:
        client = AgentClient(c)
        d = client.propose("cancel_order", {"order_id": oid})
        assert failed_check(client, d) == ["order_cancellable"]


def test_executor_can_only_record_its_own_claims(board, connect_as):
    human_limit(connect_as)
    with connect_as("hermes") as c:
        d = AgentClient(c).propose("place_order", order(market(board, LIONS, "moneyline")))
    with connect_as("executor") as c:
        with pytest.raises(psycopg.Error, match="not claimed"):
            c.execute("select kb.record_execution(%s, true, 'dry_run', 'not_sent', 'x', null, '{}', null, null)",
                      (d.request_id,))


# ---------------------------------------------------------------------------
# Visibility, sizing and the graph
# ---------------------------------------------------------------------------

def test_agents_see_only_their_own_requests(board, connect_as):
    human_limit(connect_as)
    mid = market(board, LIONS, "moneyline")
    with connect_as("hermes") as c:
        AgentClient(c).propose("place_order", order(mid))
    with connect_as("gnoesis") as c:
        AgentClient(c).propose("place_order", order(mid))
        mine = c.execute("select requested_by from kb.action_request").fetchall()
        assert mine == [("gnoesis",)]
        assert {r[0] for r in c.execute("select actor from kb.action_event").fetchall()} == {"gnoesis"}
    with connect_as("risk_officer") as c:
        assert c.execute("select count(*) from kb.action_request").fetchone()[0] == 2


def test_size_stake_is_fractional_kelly_capped_by_limits(board, connect_as):
    human_limit(connect_as, max_stake=50)
    mid = market(board, LIONS, "moneyline")
    with connect_as("hermes") as c:
        client = AgentClient(c)
        s = client.size_stake(mid, "home", 0.60, 1000)       # -102 -> 1.98039
        assert s.decimal_odds.quantize(Decimal("0.0001")) == Decimal("1.9804")
        assert s.edge.quantize(Decimal("0.0001")) == Decimal("0.1882")
        assert s.kelly.quantize(Decimal("0.0001")) == Decimal("0.1920")
        assert (s.stake, s.cap) == (Decimal("48.00"), Decimal("50"))   # 1000 * 0.192 / 4, under the cap
        assert client.size_stake(mid, "home", 0.70, 1000).stake == Decimal("50.00")  # 98.5 -> capped
        small = client.size_stake(mid, "home", 0.52, 1000)
        assert Decimal("0") < small.stake < Decimal("50")
        assert client.size_stake(mid, "home", 0.40, 1000).stake == 0  # negative edge
    with connect_as("risk_officer") as c:
        AgentClient(c).propose("set_risk_limit", {"scope": "global", "kill_switch": True})
    with connect_as("hermes") as c:
        assert AgentClient(c).size_stake(mid, "home", 0.60, 1000).stake == 0


def test_neighbors_walks_fk_relations(board, connect_as):
    lions = board.execute("select id from kb.team where name = 'Detroit Lions'").fetchone()[0]
    with connect_as("hermes") as c:
        rows = AgentClient(c).neighbors("Team", str(lions), 2)
    rels = {(d, r) for d, _, _, r, _, _ in rows}
    assert (1, "PARTICIPATES_IN") in rels and (2, "OFFERS") in rels
    assert sum(1 for d, *_, r, _, _ in rows if r == "OFFERS") == 3


def test_signal_survives_caller_rollback(nfl):
    mid = market(nfl, LIONS, "moneyline")
    sid = signals.pick(nfl, model="t", created_by="t", market_id=mid, side="home")
    nfl.rollback()
    assert nfl.execute("select count(*) from kb.signal where id = %s", (sid,)).fetchone()[0] == 1


def test_concurrent_proposals_cannot_overspend_the_daily_budget(board, connect_as):
    import threading

    human_limit(connect_as, max_stake=60, max_daily=100)
    mid = market(board, LIONS, "moneyline")
    first = connect_as("hermes")
    second = connect_as("gnoesis")
    # hermes proposes inside an open transaction and holds it
    first.execute("select * from kb.propose_action('place_order', %s::jsonb)",
                  (f'{{"market_id": {mid}, "side": "home", "stake": 60, "limit_american": -110}}',))
    result = {}

    def other():
        result["d"] = AgentClient(second).propose("place_order", order(mid, stake=60))

    t = threading.Thread(target=other)
    t.start()
    t.join(timeout=1.5)
    assert t.is_alive(), "second proposal should wait for the first to commit"
    first.commit()
    t.join(timeout=10)
    assert result["d"].status == "rejected"
    assert "daily_exposure" in result["d"].reason
