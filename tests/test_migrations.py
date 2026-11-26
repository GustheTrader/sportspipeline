import shutil

import psycopg
import pytest

from sportspipeline import db as spdb

from .conftest import ADMIN_DSN, dsn_for


def test_migrate_is_idempotent(owner):
    assert spdb.migrate(owner) == []


def test_edited_migration_is_refused(owner, tmp_path):
    for m in spdb.list_migrations():
        shutil.copy(m.path, tmp_path / m.path.name)
    first = sorted(tmp_path.glob("*.sql"))[0]
    first.write_text(first.read_text() + "\n-- edited\n")
    with pytest.raises(spdb.MigrationError, match="checksum mismatch"):
        spdb.migrate(owner, tmp_path)


def test_missing_migration_file_is_refused(owner, tmp_path):
    for m in spdb.list_migrations()[:-1]:
        shutil.copy(m.path, tmp_path / m.path.name)
    with pytest.raises(spdb.MigrationError, match="does not"):
        spdb.migrate(owner, tmp_path)


def test_registry_is_consistent(owner):
    rows = owner.execute("select name, table_name, status from kb.object_type").fetchall()
    for name, table, status in rows:
        if status == "active":
            assert owner.execute("select to_regclass(%s)", (table,)).fetchone()[0] is not None, name
    # every relation's endpoints are registered (FK) and proposed types only
    # appear in proposed relations
    bad = owner.execute(
        "select l.name from kb.link_type l join kb.object_type s on s.name = l.src_type "
        "join kb.object_type d on d.name = l.dst_type "
        "where l.status = 'active' and (s.status <> 'active' or d.status <> 'active')"
    ).fetchall()
    assert bad == []


def test_supabase_api_roles_cannot_reach_kb(owner):
    for role in ("anon", "authenticated"):
        exists = owner.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone()
        if not exists:
            continue
        usage = owner.execute("select has_schema_privilege(%s, 'kb', 'usage')", (role,)).fetchone()[0]
        assert usage is False
        can = owner.execute(
            "select has_function_privilege(%s, 'kb.propose_action(text, jsonb)', 'execute')", (role,)
        ).fetchone()[0]
        assert can is False


def test_security_definer_functions_pin_search_path(owner):
    rows = owner.execute(
        "select p.proname, p.proconfig from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'kb' and p.prosecdef"
    ).fetchall()
    assert rows, "expected security definer functions in kb"
    for name, config in rows:
        assert config and 'search_path=""' in config, name


def test_edge_table_only_takes_registered_edge_relations(owner):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        owner.execute(
            "insert into kb.edge (src_type, src_id, rel, dst_type, dst_id, source_kind) "
            "values ('Team', '1', 'LIKES', 'Team', '2', 'human')"
        )
    owner.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="foreign key"):
        owner.execute(
            "insert into kb.edge (src_type, src_id, rel, dst_type, dst_id, source_kind) "
            "values ('Player', '1', 'PLAYS_FOR', 'Team', '2', 'human')"
        )
    owner.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="proposed"):
        owner.execute(
            "insert into kb.edge (src_type, src_id, rel, dst_type, dst_id, source_kind) "
            "values ('NewsItem', '1', 'MENTIONS', 'Team', '2', 'extraction')"
        )
