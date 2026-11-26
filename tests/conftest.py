"""Test harness.

Database tests run against a real Postgres. The default is a local Supabase
Postgres image (it ships pg_jsonschema), for example:

    docker run -d --name sportspipeline_pg -e POSTGRES_PASSWORD=postgres \
        -p 127.0.0.1:5481:5432 public.ecr.aws/supabase/postgres:17.11.0.004

Override with SP_TEST_ADMIN_DSN. A template database is migrated once per
session and every test gets its own copy, so tests never share state.

Fixtures are real ESPN responses recorded by scripts/record_espn_fixtures.py.
"""

from __future__ import annotations

import gzip
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from sportspipeline import db as spdb
from sportspipeline.admin import add_agent

FIXTURES = Path(__file__).parent / "fixtures" / "espn"
MANIFEST = json.loads((FIXTURES / "MANIFEST.json").read_text())
ADMIN_DSN = os.environ.get("SP_TEST_ADMIN_DSN", "postgresql://postgres:postgres@127.0.0.1:5481/postgres")
LOGIN_PASSWORD = "sp-test-" + uuid.uuid4().hex[:12]
SESSION = uuid.uuid4().hex[:6]

# agent id -> (role, login)
AGENTS = {
    "hermes": ("agent", f"spt_{SESSION}_hermes"),
    "gnoesis": ("agent", f"spt_{SESSION}_gnoesis"),
    "risk_officer": ("risk_officer", f"spt_{SESSION}_officer"),
    "executor": ("executor", f"spt_{SESSION}_exec"),
    "jeff": ("human", f"spt_{SESSION}_human"),
}
STRANGER_LOGIN = f"spt_{SESSION}_stranger"


def load(name: str) -> dict:
    return json.loads(gzip.decompress((FIXTURES / MANIFEST[name]["file"]).read_bytes()))


def fetched_at(name: str) -> datetime:
    return datetime.fromisoformat(MANIFEST[name]["fetched_at"])


def fixture_fetch(*names: str):
    """A fetch function that serves recorded responses by URL."""
    by_url = {MANIFEST[n]["url"]: n for n in names}

    def fetch(url: str) -> dict:
        key = url.replace("&limit=1000", "")
        if key not in by_url:
            raise AssertionError(f"test asked for an unrecorded URL: {url}")
        return load(by_url[key])

    return fetch


def dsn_for(dbname: str, user: str | None = None, password: str | None = None) -> str:
    parts = conninfo_to_dict(ADMIN_DSN)
    parts["dbname"] = dbname
    if user:
        parts["user"] = user
        parts["password"] = password
    return make_conninfo(**parts)


@pytest.fixture(scope="session")
def template_db():
    try:
        admin = psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"no test Postgres at {ADMIN_DSN}: {exc}")
    name = f"sp_tmpl_{SESSION}"
    admin.execute(f'create database "{name}"')
    with psycopg.connect(dsn_for(name)) as conn:
        spdb.migrate(conn)
        for agent_id, (role, login) in AGENTS.items():
            add_agent(conn, agent_id=agent_id, name=agent_id.title(), role=role, login=login,
                      password=LOGIN_PASSWORD)
        conn.execute(sql.SQL("create role {} login password {}").format(
            sql.Identifier(STRANGER_LOGIN), sql.Literal(LOGIN_PASSWORD)))
        conn.commit()
    yield name
    admin.execute(f'drop database if exists "{name}" with (force)')
    for _, login in AGENTS.values():
        admin.execute(f'drop role if exists "{login}"')
    admin.execute(f'drop role if exists "{STRANGER_LOGIN}"')
    admin.close()


@pytest.fixture
def test_db(template_db):
    name = f"sp_t_{SESSION}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'create database "{name}" template "{template_db}"')
    yield name
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(f'drop database if exists "{name}" with (force)')


@pytest.fixture
def owner(test_db):
    with psycopg.connect(dsn_for(test_db)) as conn:
        yield conn


@pytest.fixture
def connect_as(test_db):
    opened = []

    def _connect(agent_id: str) -> psycopg.Connection:
        login = STRANGER_LOGIN if agent_id == "stranger" else AGENTS[agent_id][1]
        conn = psycopg.connect(dsn_for(test_db, login, LOGIN_PASSWORD))
        opened.append(conn)
        return conn

    yield _connect
    for c in opened:
        c.close()


@pytest.fixture
def nfl(owner):
    """NFL teams plus the real Thanksgiving 2026 pregame scoreboard."""
    from sportspipeline import ingest

    r = ingest.ingest_teams(owner, "nfl", fetch=fixture_fetch("nfl_teams"))
    assert r.status == "ok"
    r = ingest.ingest_scoreboard(owner, "nfl", "20261126", fetch=fixture_fetch("nfl_scoreboard_20261126"),
                                 observed_at=fetched_at("nfl_scoreboard_20261126"))
    assert r.status == "ok"
    return owner


def market(conn, espn_event: str, kind: str) -> int:
    return conn.execute(
        "select m.id from kb.market m join kb.game_source s on s.game_id = m.game_id "
        "where s.source = 'espn' and s.external_id = %s and m.kind = %s",
        (espn_event, kind),
    ).fetchone()[0]


def game_id(conn, espn_event: str) -> int:
    return conn.execute(
        "select game_id from kb.game_source where source = 'espn' and external_id = %s", (espn_event,)
    ).fetchone()[0]


def shift_to_upcoming(conn, espn_event: str, *, starts_in: str = "1 day", quotes_age: str = "1 minute") -> None:
    """Move a recorded game into the future relative to the test clock.

    Prices, lines, teams and the order of events are the recorded ones; only
    the wall-clock times move, so time-based checks (pregame, price age) can
    be tested on any date.
    """
    gid = game_id(conn, espn_event)
    conn.execute("update kb.game set start_time = now() + %s::interval where id = %s", (starts_in, gid))
    conn.execute(
        "update kb.price_snapshot set observed_at = now() - %s::interval "
        "where market_id in (select id from kb.market where game_id = %s)",
        (quotes_age, gid),
    )
    conn.commit()
