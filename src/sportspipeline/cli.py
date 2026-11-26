"""Command line.

Connections:
  DATABASE_URL           owner/pipeline connection: migrate, ingest, grade, admin
  AGENT_DATABASE_URL     an agent login (sp_agent or sp_risk_officer member)
  EXECUTOR_DATABASE_URL  the executor login (sp_executor member)
  HUMAN_DATABASE_URL     a human login (sp_human member), for raising limits
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, grade, ingest, ledger, normalize, signals
from .actions import AgentClient
from .admin import add_agent
from .executor import Executor, LiveUnavailable


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _print_rows(rows, headers) -> None:
    rows = [["" if v is None else str(v) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))


def _run_result(res: ingest.RunResult) -> int:
    print(f"run {res.run_id}: {res.status}  seen={res.rows_seen} written={res.rows_written} "
          f"unresolved={res.unresolved}" + (f"  error={res.error}" if res.error else ""))
    if res.unresolved:
        print("  unresolved names are listed by `sportspipeline unresolved`", file=sys.stderr)
    return 1 if res.status == "failed" else 0


def _dates(value: str) -> list[str]:
    if value == "today":
        return [datetime.now(timezone.utc).strftime("%Y%m%d")]
    if value.startswith("next"):
        n = int(value[4:] or 1)
        today = datetime.now(timezone.utc).date()
        return [(today + timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]
    return [value]


def cmd_migrate(args) -> int:
    with db.connect() as conn:
        if args.status:
            done = db.applied(conn)
            for m in db.list_migrations():
                print(f"{m.version}  {'applied' if m.version in done else 'PENDING'}")
            return 0
        applied = db.migrate(conn)
        print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    return 0


def cmd_teams(args) -> int:
    with db.connect() as conn:
        return _run_result(ingest.ingest_teams(conn, args.league))


def cmd_ingest(args) -> int:
    rc = 0
    with db.connect() as conn:
        if args.what == "scoreboard":
            for d in _dates(args.dates):
                rc |= _run_result(ingest.ingest_scoreboard(conn, args.league, d))
        else:
            rc |= _run_result(ingest.ingest_injuries(conn, args.league))
    return rc


def cmd_runs(args) -> int:
    with db.connect() as conn:
        rows = conn.execute(
            "select id, source, kind, league, window_label, status, rows_seen, rows_written, unresolved, "
            "started_at, left(coalesce(error, ''), 80) from kb.ingest_run order by id desc limit %s",
            (args.limit,),
        ).fetchall()
    _print_rows(rows, ["id", "source", "kind", "league", "window", "status", "seen", "written",
                       "unresolved", "started_at", "error"])
    return 0


def cmd_unresolved(args) -> int:
    with db.connect() as conn:
        rows = conn.execute(
            "select source, league, kind, raw_name, seen_count, last_seen_at from kb.unresolved_name "
            "order by last_seen_at desc"
        ).fetchall()
    _print_rows(rows, ["source", "league", "kind", "raw_name", "seen", "last_seen"])
    return 0


def cmd_alias(args) -> int:
    with db.connect() as conn:
        with conn.transaction():
            team_id = normalize.add_alias(conn, args.source, args.league, args.alias, args.team)
    print(f"alias {normalize.norm(args.alias)!r} -> team {team_id}")
    return 0


def cmd_markets(args) -> int:
    with db.connect() as conn:
        rows = conn.execute(
            """
            select m.id, g.league, g.start_time, at.name || ' at ' || ht.name, g.status, m.kind, m.venue,
                   s.side, s.line, s.american_odds, s.observed_at
            from kb.market m
            join kb.game g on g.id = m.game_id
            join kb.team ht on ht.id = g.home_team_id
            join kb.team at on at.id = g.away_team_id
            left join lateral (
                select distinct on (side) side, line, american_odds, observed_at
                from kb.price_snapshot ps where ps.market_id = m.id and ps.quote = 'current'
                order by side, observed_at desc, id desc) s on true
            where (%s::text is null or g.league = %s) and g.start_time > now() - interval '12 hours'
            order by g.start_time, m.id, s.side
            """,
            (args.league, args.league),
        ).fetchall()
    _print_rows(rows, ["market", "league", "start", "game", "status", "kind", "venue", "side", "line",
                       "odds", "observed_at"])
    return 0


def cmd_signal(args) -> int:
    with db.connect() as conn:
        try:
            sid = signals.pick(conn, model=args.model, created_by=args.by, market_id=args.market_id,
                               side=args.side, team_id=args.team_id, model_prob=args.prob,
                               stake_units=args.units, note=args.note)
        except signals.SignalRejected as exc:
            print(f"rejected ({exc.code}): {exc.reason}  [signal_rejection {exc.rejection_id}]")
            return 2
    print(f"signal {sid}")
    return 0


def cmd_grade(args) -> int:
    with db.connect() as conn:
        rep = grade.grade_pending(conn)
    print(f"graded={rep.graded} voided={rep.voided} waiting={rep.waiting} rejected={len(rep.rejected)}")
    return 0


def cmd_ledger(args) -> int:
    with db.connect() as conn:
        if args.what == "export":
            n = ledger.export_csv(conn, Path(args.out), graded_only=args.graded_only)
            print(f"{n} rows -> {args.out}")
        else:
            _print_rows(ledger.summary(conn), ["model", "kind", "signals", "graded", "wins", "losses",
                                               "profit_units", "avg_clv"])
    return 0


def cmd_agents(args) -> int:
    password = None
    if not args.existing_login:
        password = os.environ.get("SP_NEW_LOGIN_PASSWORD") or getpass.getpass(f"password for {args.login}: ")
    with db.connect() as conn:
        add_agent(conn, agent_id=args.id, name=args.name, role=args.role, login=args.login, password=password)
    print(f"agent {args.id} ({args.role}) -> login {args.login}")
    return 0


def cmd_limits(args) -> int:
    if args.what == "show":
        with db.connect() as conn:
            rows = conn.execute(
                "select id, scope, scope_key, max_stake, max_daily_exposure, max_price_age_s, kill_switch, "
                "set_by, set_at, reason from kb.risk_limit_current order by scope, scope_key"
            ).fetchall()
        _print_rows(rows, ["id", "scope", "key", "max_stake", "max_daily", "max_age_s", "kill",
                           "set_by", "set_at", "reason"])
        return 0
    with db.connect(var="HUMAN_DATABASE_URL") as conn:
        with conn.transaction():
            lid = conn.execute(
                "select kb.set_risk_limit_human(%s::text, %s::text, %s::numeric, %s::numeric, %s::integer, "
                "%s::boolean, %s::text)",
                (args.scope, args.key, args.max_stake, args.max_daily, args.max_age, args.kill, args.reason),
            ).fetchone()[0]
    print(f"risk_limit {lid}")
    return 0


def cmd_propose(args) -> int:
    params = json.loads(args.params)
    with db.connect(var="AGENT_DATABASE_URL") as conn:
        client = AgentClient(conn)
        d = client.propose(args.action, params)
        print(f"{d.request_id}  {d.status}  {d.reason}")
        for seq, name, passed, detail, limit_id in client.checks(d.request_id):
            print(f"  {seq:>2} {'PASS' if passed else 'FAIL'} {name:<20} {detail or ''}"
                  + (f" [limit {limit_id}]" if limit_id else ""))
    return 0 if d.status != "rejected" else 2


def cmd_size(args) -> int:
    with db.connect(var="AGENT_DATABASE_URL") as conn:
        s = AgentClient(conn).size_stake(args.market_id, args.side, args.prob, args.bankroll, args.kelly)
    print(f"stake={s.stake} decimal={s.decimal_odds:.4f} edge={s.edge:.4f} kelly={s.kelly:.4f} cap={s.cap}")
    return 0


def cmd_executor(args) -> int:
    try:
        with db.connect(var="EXECUTOR_DATABASE_URL") as conn:
            ex = Executor(conn, mode="live" if args.live else "dry_run")
            summary = ex.run_once(args.limit)
    except LiveUnavailable as exc:
        print(f"live mode unavailable: {exc}")
        return 2
    print(f"claimed={summary.claimed} executed={summary.executed} failed={summary.failed}")
    for line in summary.details:
        print("  " + line)
    return 0


def cmd_graph(args) -> int:
    with db.connect() as conn:
        rows = conn.execute("select * from kb.neighbors(%s, %s, %s)", (args.type, args.id, args.depth)).fetchall()
    _print_rows(rows, ["depth", "src_type", "src_id", "rel", "dst_type", "dst_id"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sportspipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("migrate", help="apply pending migrations")
    s.add_argument("--status", action="store_true")
    s.set_defaults(fn=cmd_migrate)

    s = sub.add_parser("teams", help="seed teams and aliases from ESPN")
    s.add_argument("--league", required=True)
    s.set_defaults(fn=cmd_teams)

    s = sub.add_parser("ingest", help="pull ESPN data")
    s.add_argument("what", choices=["scoreboard", "injuries"])
    s.add_argument("--league", required=True)
    s.add_argument("--dates", default="today", help="YYYYMMDD, YYYYMMDD-YYYYMMDD, today, nextN")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("runs", help="recent ingest runs")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_runs)

    s = sub.add_parser("unresolved", help="names the normalizer could not map")
    s.set_defaults(fn=cmd_unresolved)

    s = sub.add_parser("alias", help="map a vendor name to a team")
    s.add_argument("--league", required=True)
    s.add_argument("--source", default="espn")
    s.add_argument("--alias", required=True)
    s.add_argument("--team", required=True, help="canonical team name")
    s.set_defaults(fn=cmd_alias)

    s = sub.add_parser("markets", help="upcoming markets with their latest quotes")
    s.add_argument("--league")
    s.set_defaults(fn=cmd_markets)

    s = sub.add_parser("signal", help="record a signal at the latest pregame quote")
    s.add_argument("--market-id", type=int, required=True)
    s.add_argument("--side", required=True, choices=["home", "away", "over", "under"])
    s.add_argument("--model", required=True)
    s.add_argument("--by", required=True)
    s.add_argument("--team-id", type=int)
    s.add_argument("--prob", type=float)
    s.add_argument("--units", type=float, default=1)
    s.add_argument("--note")
    s.set_defaults(fn=cmd_signal)

    s = sub.add_parser("grade", help="grade signals whose games are final")
    s.set_defaults(fn=cmd_grade)

    s = sub.add_parser("ledger", help="ledger summary or CSV export")
    s.add_argument("what", choices=["summary", "export"])
    s.add_argument("--out", default="data/ledger.csv")
    s.add_argument("--graded-only", action="store_true")
    s.set_defaults(fn=cmd_ledger)

    s = sub.add_parser("agents", help="register an agent with its own database login")
    s.add_argument("--id", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--role", required=True, choices=["agent", "risk_officer", "executor", "human"])
    s.add_argument("--login", required=True)
    s.add_argument("--existing-login", action="store_true", help="login role already exists")
    s.set_defaults(fn=cmd_agents)

    s = sub.add_parser("limits", help="show limits, or set one as a human")
    s.add_argument("what", choices=["show", "set"])
    s.add_argument("--scope", choices=["global", "league", "market", "agent"])
    s.add_argument("--key", default="*")
    s.add_argument("--max-stake", type=float)
    s.add_argument("--max-daily", type=float)
    s.add_argument("--max-age", type=int)
    s.add_argument("--kill", action=argparse.BooleanOptionalAction, default=False)
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_limits)

    s = sub.add_parser("propose", help="propose an action as an agent")
    s.add_argument("action")
    s.add_argument("params", help="JSON object")
    s.set_defaults(fn=cmd_propose)

    s = sub.add_parser("size", help="size a stake as an agent")
    s.add_argument("--market-id", type=int, required=True)
    s.add_argument("--side", required=True)
    s.add_argument("--prob", type=float, required=True)
    s.add_argument("--bankroll", type=float, required=True)
    s.add_argument("--kelly", type=float, default=0.25)
    s.set_defaults(fn=cmd_size)

    s = sub.add_parser("executor", help="claim and execute approved requests")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--live", action="store_true")
    s.set_defaults(fn=cmd_executor)

    s = sub.add_parser("graph", help="multi-hop neighbourhood of an object")
    s.add_argument("--type", required=True)
    s.add_argument("--id", required=True)
    s.add_argument("--depth", type=int, default=2)
    s.set_defaults(fn=cmd_graph)
    return p


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)
    if args.cmd == "limits" and args.what == "set" and (not args.scope or not args.reason):
        print("limits set needs --scope and --reason", file=sys.stderr)
        return 2
    try:
        return args.fn(args)
    except db.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
