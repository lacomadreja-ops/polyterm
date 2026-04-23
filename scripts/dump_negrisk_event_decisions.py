from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Permite ejecutar este script directamente desde `scripts/`
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polyterm.utils.paths import get_polyterm_dir


EVENT_TITLES = [
    "Presidential Election Winner 2028",
    "Democratic Presidential Nominee 2028",
]


def _pretty_data(data: str | None) -> str:
    if not data:
        return ""
    try:
        obj = json.loads(data)
    except Exception:
        return str(data)
    return json.dumps(obj, ensure_ascii=False, indent=2)


def main() -> int:
    runtime = get_polyterm_dir()
    db_path = runtime / "data.db"
    print(f"runtime_dir: {runtime}")
    print(f"db_path: {db_path} (exists={db_path.exists()})")
    if not db_path.exists():
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    sql = (
        "SELECT timestamp, opportunity_id, market_id, market_title, decision, reason, data "
        "FROM arb_decisions "
        "WHERE market_title LIKE ? "
        "ORDER BY timestamp DESC "
        "LIMIT 20"
    )

    for t in EVENT_TITLES:
        print("\n" + "=" * 88)
        print(f"EVENT: {t}")
        rows = cur.execute(sql, (f"%{t}%",)).fetchall()
        print(f"rows: {len(rows)}")

        for r in rows:
            print("\n---")
            print(f"timestamp:      {r['timestamp']}")
            print(f"decision:       {r['decision']}")
            print(f"reason:         {r['reason']}")
            print(f"market_id:      {r['market_id']}")
            print(f"opportunity_id: {r['opportunity_id']}")
            print("data:")
            print(_pretty_data(r["data"]))

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

