"""Connections and the migration runner.

Migrations are numbered SQL files applied in order, each in its own
transaction. An applied migration's checksum is stored; if the file on disk
later changes, `migrate` refuses to run instead of silently diverging. The fix
for a wrong migration is a new migration, never a reset.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import psycopg


class ConfigError(RuntimeError):
    pass


class MigrationError(RuntimeError):
    pass


def dsn_from_env(var: str = "DATABASE_URL") -> str:
    dsn = os.environ.get(var, "").strip()
    if not dsn:
        raise ConfigError(
            f"{var} is not set. Point it at a Postgres database (Supabase or local); "
            "see .env.example."
        )
    return dsn


def connect(dsn: str | None = None, *, var: str = "DATABASE_URL", autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(dsn or dsn_from_env(var), autocommit=autocommit)


def migrations_dir() -> Path:
    here = Path(__file__).resolve()
    for candidate in (here.parent / "migrations", here.parents[2] / "migrations"):
        if candidate.is_dir():
            return candidate
    raise MigrationError("migrations directory not found")


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text()

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


def list_migrations(directory: Path | None = None) -> list[Migration]:
    directory = directory or migrations_dir()
    found = sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    return [Migration(p.stem, p) for p in found]


_META = """
create table if not exists public.sportspipeline_migration (
    version    text primary key,
    checksum   text not null,
    applied_at timestamptz not null default now()
)
"""


def applied(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(_META)
        cur.execute("select version, checksum from public.sportspipeline_migration")
        rows = dict(cur.fetchall())
    conn.commit()
    return rows


def migrate(conn: psycopg.Connection, directory: Path | None = None) -> list[str]:
    """Apply pending migrations. Returns the versions applied."""
    done = applied(conn)
    pending: list[Migration] = []
    for m in list_migrations(directory):
        if m.version in done:
            if done[m.version] != m.checksum:
                raise MigrationError(
                    f"migration {m.version} was edited after it was applied "
                    "(checksum mismatch). Write a new migration instead."
                )
        else:
            pending.append(m)
    unknown = set(done) - {m.version for m in list_migrations(directory)}
    if unknown:
        raise MigrationError(f"database has migrations this checkout does not: {sorted(unknown)}")
    out = []
    for m in pending:
        with conn.transaction():
            conn.execute(m.sql)
            conn.execute(
                "insert into public.sportspipeline_migration (version, checksum) values (%s, %s)",
                (m.version, m.checksum),
            )
        out.append(m.version)
    return out
