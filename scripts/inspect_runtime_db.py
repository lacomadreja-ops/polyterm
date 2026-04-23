from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Permite ejecutar este script directamente desde `scripts/`
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polyterm.utils.paths import get_polyterm_dir


def main() -> int:
    runtime_dir = get_polyterm_dir()
    db_path = runtime_dir / "data.db"
    log_path = runtime_dir / "polyterm.log"

    print(f"runtime_dir: {runtime_dir}")
    print(f"db_path: {db_path} (exists={db_path.exists()})")
    print(f"log_path: {log_path} (exists={log_path.exists()})")

    if not db_path.exists():
        return 2

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    print(f"tables_count: {len(tables)}")
    print(f"has_arb_executions: {'arb_executions' in tables}")
    print(f"has_arb_decisions: {'arb_decisions' in tables}")

    if "arb_executions" in tables:
        print("arb_executions_count:", cur.execute("SELECT COUNT(*) FROM arb_executions").fetchone()[0])

    if "arb_decisions" in tables:
        print("arb_decisions_count:", cur.execute("SELECT COUNT(*) FROM arb_decisions").fetchone()[0])

        print("\nTop SKIP reasons:")
        for reason, c in cur.execute(
            "SELECT reason, COUNT(*) FROM arb_decisions WHERE decision='SKIP' GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 15"
        ).fetchall():
            print(f"- {reason}: {c}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

