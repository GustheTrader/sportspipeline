"""Admin tasks run with the owner connection (DATABASE_URL)."""

from __future__ import annotations

import re

import psycopg
from psycopg import sql

GROUP_FOR_ROLE = {
    "agent": "sp_agent",
    "risk_officer": "sp_risk_officer",
    "executor": "sp_executor",
    "human": "sp_human",
}


def add_agent(conn: psycopg.Connection, *, agent_id: str, name: str, role: str, login: str,
              password: str | None) -> None:
    """Register an agent and give it its own Postgres login in one group role.

    With password=None the login role must already exist (for example one
    created in the Supabase dashboard); it is only added to the group.
    """
    if role not in GROUP_FOR_ROLE:
        raise ValueError(f"role must be one of {sorted(GROUP_FOR_ROLE)}")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", login):
        raise ValueError("login must be lower-case letters, digits and underscores")
    with conn.transaction():
        exists = conn.execute("select 1 from pg_roles where rolname = %s", (login,)).fetchone()
        if not exists:
            if password is None:
                raise ValueError(f"login role {login} does not exist; give a password to create it")
            conn.execute(
                sql.SQL("create role {} login inherit password {}").format(
                    sql.Identifier(login), sql.Literal(password)
                )
            )
        # Plain GRANT works on Postgres 15 and later; the login inherits the
        # group's privileges and carries nothing else.
        conn.execute(
            sql.SQL("grant {} to {}").format(
                sql.Identifier(GROUP_FOR_ROLE[role]), sql.Identifier(login)
            )
        )
        conn.execute(
            "insert into kb.agent (id, name, role, db_role) values (%s, %s, %s, %s)",
            (agent_id, name, role, login),
        )


def deactivate_agent(conn: psycopg.Connection, agent_id: str) -> None:
    with conn.transaction():
        conn.execute("update kb.agent set active = false where id = %s", (agent_id,))
