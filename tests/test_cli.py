"""CLI paths, run through main() with per-role connection strings."""

import json

from sportspipeline import cli

from .conftest import AGENTS, LOGIN_PASSWORD, dsn_for, market, shift_to_upcoming

LIONS = "401873081"


def env_for(monkeypatch, test_db):
    monkeypatch.setenv("DATABASE_URL", dsn_for(test_db))
    monkeypatch.setenv("AGENT_DATABASE_URL", dsn_for(test_db, AGENTS["hermes"][1], LOGIN_PASSWORD))
    monkeypatch.setenv("EXECUTOR_DATABASE_URL", dsn_for(test_db, AGENTS["executor"][1], LOGIN_PASSWORD))
    monkeypatch.setenv("HUMAN_DATABASE_URL", dsn_for(test_db, AGENTS["jeff"][1], LOGIN_PASSWORD))
    monkeypatch.chdir("/")  # no stray .env


def test_cli_limit_propose_execute(nfl, test_db, monkeypatch, capsys):
    shift_to_upcoming(nfl, LIONS)
    env_for(monkeypatch, test_db)
    mid = market(nfl, LIONS, "moneyline")
    # floats on purpose: they arrive as double precision and need casts
    assert cli.main(["limits", "set", "--scope", "global", "--max-stake", "50.5", "--max-daily", "100.25",
                     "--max-age", "3600", "--reason", "cli test"]) == 0
    assert cli.main(["size", "--market-id", str(mid), "--side", "home", "--prob", "0.56",
                     "--bankroll", "1000.0"]) == 0
    assert "cap=50.5" in capsys.readouterr().out
    params = json.dumps({"market_id": mid, "side": "home", "stake": 20.5, "limit_american": -130})
    assert cli.main(["propose", "place_order", params]) == 0
    assert " approved " in capsys.readouterr().out
    assert cli.main(["executor"]) == 0
    assert "dry_run not_sent" in capsys.readouterr().out
    assert cli.main(["ledger", "summary"]) == 0


def test_cli_rejection_exit_code(nfl, test_db, monkeypatch, capsys):
    shift_to_upcoming(nfl, LIONS)
    env_for(monkeypatch, test_db)
    params = json.dumps({"market_id": market(nfl, LIONS, "moneyline"), "side": "home", "stake": 5,
                         "limit_american": -130})
    assert cli.main(["propose", "place_order", params]) == 2
    assert "global_limit_exists" in capsys.readouterr().out


def test_cli_missing_database_url(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir("/")
    assert cli.main(["runs"]) == 2
    assert "DATABASE_URL is not set" in capsys.readouterr().err
