"""Record real ESPN responses as offline test fixtures.

Each fixture is the response body with only image blocks removed (`logos`,
`headshot`), gzipped, plus a MANIFEST.json entry with the URL, fetch time and
sha256 of the stored file. Nothing is generated or edited beyond that.

    python scripts/record_espn_fixtures.py
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "espn"
BASE = "https://site.api.espn.com/apis/site/v2/sports"

FIXTURES = {
    "nfl_teams": f"{BASE}/football/nfl/teams",
    "nfl_scoreboard_20261122": f"{BASE}/football/nfl/scoreboard?dates=20261122",
    "nfl_scoreboard_20261126": f"{BASE}/football/nfl/scoreboard?dates=20261126",
    "nfl_scoreboard_20261129": f"{BASE}/football/nfl/scoreboard?dates=20261129",
    "nfl_scoreboard_20261124": f"{BASE}/football/nfl/scoreboard?dates=20261124",
    "nfl_injuries": f"{BASE}/football/nfl/injuries",
    "nba_teams": f"{BASE}/basketball/nba/teams",
    "nba_scoreboard_20261127": f"{BASE}/basketball/nba/scoreboard?dates=20261127",
}

DROP_KEYS = {"logos", "headshot"}


def strip(node):
    if isinstance(node, dict):
        return {k: strip(v) for k, v in node.items() if k not in DROP_KEYS}
    if isinstance(node, list):
        return [strip(v) for v in node]
    return node


def main(names: list[str]) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    with httpx.Client(timeout=30, headers={"User-Agent": "sportspipeline-fixture-recorder"}) as client:
        for name in names or list(FIXTURES):
            url = FIXTURES[name]
            resp = client.get(url)
            resp.raise_for_status()
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            body = json.dumps(strip(resp.json()), separators=(",", ":"), sort_keys=True).encode()
            path = OUT / f"{name}.json.gz"
            path.write_bytes(gzip.compress(body, mtime=0))
            manifest[name] = {
                "url": url,
                "fetched_at": fetched_at,
                "file": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "removed_keys": sorted(DROP_KEYS),
            }
            print(f"{name}: {len(body):,} bytes json, {path.stat().st_size:,} gz")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
