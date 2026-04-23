#!/usr/bin/env python3
"""
prelive_report.py
Genera un reporte pre-LIVE desde ~/.polyterm/data.db:
- Conteos de decisiones (EXECUTE/SKIP/ERROR) y razones
- Conteos de ejecuciones y tasa de éxito
- Diversidad por mercado y clustering temporal simple
 - Gates (PASS/FAIL) con umbrales configurables
"""

import json
import sqlite3
import sys
from pathlib import Path as _PathForSysPath
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Ensure repo root is importable when running as a script
_REPO_ROOT = _PathForSysPath(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _db_path() -> Path:
    """Return the same DB path PolyTerm uses at runtime."""
    from polyterm.utils.paths import get_polyterm_dir

    return get_polyterm_dir() / "data.db"


def _dt(x: Any) -> datetime | None:
    if x is None:
        return None
    if isinstance(x, datetime):
        return x
    s = str(x)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        # sqlite may store as "YYYY-MM-DD HH:MM:SS"
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def load_decisions(conn: sqlite3.Connection, hours: float) -> List[Dict[str, Any]]:
    cutoff = datetime.now() - timedelta(hours=hours)
    rows = conn.execute(
        "SELECT * FROM arb_decisions WHERE timestamp >= datetime(?) ORDER BY timestamp ASC",
        (cutoff.isoformat(sep=" "),),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["data"] = json.loads(d.get("data") or "{}")
        except Exception:
            d["data"] = {}
        d["timestamp"] = _dt(d.get("timestamp"))
        out.append(d)
    return out


def load_executions(conn: sqlite3.Connection, hours: float) -> List[Dict[str, Any]]:
    cutoff = datetime.now() - timedelta(hours=hours)
    rows = conn.execute(
        "SELECT * FROM arb_executions WHERE timestamp >= datetime(?) ORDER BY timestamp ASC",
        (cutoff.isoformat(sep=" "),),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["timestamp"] = _dt(d.get("timestamp"))
        out.append(d)
    return out


def summarize(hours: float = 24.0) -> Dict[str, Any]:
    db_path = _db_path()
    if not db_path.exists():
        return {"ok": False, "error": f"DB not found: {db_path}"}

    conn = _connect()
    try:
        decisions = load_decisions(conn, hours)
        execs = load_executions(conn, hours)
    finally:
        conn.close()

    decision_counts = Counter((d.get("decision") or "").upper() for d in decisions)
    reason_counts = Counter((d.get("reason") or "") for d in decisions if (d.get("decision") or "").upper() != "")

    # Diversity by market
    by_market = Counter(d.get("market_id") or "" for d in decisions if d.get("market_id"))
    top_markets = by_market.most_common(10)

    # Temporal clustering: gaps between EXECUTE(pre_execute) attempts
    exec_attempt_ts = [
        d["timestamp"]
        for d in decisions
        if (d.get("decision") or "").upper() == "EXECUTE" and d.get("reason") == "pre_execute" and d.get("timestamp")
    ]
    gaps_sec: List[float] = []
    for a, b in zip(exec_attempt_ts, exec_attempt_ts[1:]):
        gaps_sec.append((b - a).total_seconds())

    executions_ok = sum(1 for e in execs if int(e.get("both_success") or 0) == 1)
    executions_total = len(execs)
    errors_total = sum(1 for d in decisions if (d.get("decision") or "").upper() == "ERROR")

    return {
        "ok": True,
        "db": str(db_path),
        "hours": hours,
        "decisions_total": len(decisions),
        "decision_counts": dict(decision_counts),
        "top_reasons": [{"reason": r, "count": c} for r, c in reason_counts.most_common(15)],
        "market_diversity": {
            "unique_markets": len([m for m in by_market.keys() if m]),
            "top_markets": [{"market_id": m, "count": c} for m, c in top_markets if m],
        },
        "executions": {
            "total": executions_total,
            "successful": executions_ok,
            "success_rate": (executions_ok / executions_total) if executions_total else None,
        },
        "errors": {
            "total": errors_total,
            "error_rate_over_decisions": (errors_total / len(decisions)) if decisions else None,
        },
        "exec_attempt_gaps_sec": {
            "count": len(gaps_sec),
            "p50": sorted(gaps_sec)[len(gaps_sec)//2] if gaps_sec else None,
            "min": min(gaps_sec) if gaps_sec else None,
            "max": max(gaps_sec) if gaps_sec else None,
        },
    }


def evaluate_gates(report: Dict[str, Any], gates: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evalúa gates cuantitativos y retorna veredicto PASS/FAIL.

    gates soportados:
      - min_decisions
      - min_executions
      - min_success_rate
      - max_error_rate
      - min_unique_markets
      - max_top_market_share
    """
    if not report.get("ok"):
        return {"pass": False, "gates": [], "error": report.get("error")}

    decisions_total = int(report.get("decisions_total") or 0)
    executions_total = int(report.get("executions", {}).get("total") or 0)
    success_rate = report.get("executions", {}).get("success_rate")
    err_rate = report.get("errors", {}).get("error_rate_over_decisions")
    unique_markets = int(report.get("market_diversity", {}).get("unique_markets") or 0)

    top_markets = report.get("market_diversity", {}).get("top_markets") or []
    top_count = int(top_markets[0]["count"]) if top_markets else 0
    top_share = (top_count / decisions_total) if decisions_total else None

    def gate(name: str, passed: bool, observed: Any, threshold: Any) -> Dict[str, Any]:
        return {"name": name, "pass": bool(passed), "observed": observed, "threshold": threshold}

    out: List[Dict[str, Any]] = []

    out.append(gate("min_decisions", decisions_total >= int(gates["min_decisions"]), decisions_total, int(gates["min_decisions"])))
    out.append(gate("min_executions", executions_total >= int(gates["min_executions"]), executions_total, int(gates["min_executions"])))

    if success_rate is None:
        out.append(gate("min_success_rate", False, None, float(gates["min_success_rate"])))
    else:
        out.append(gate("min_success_rate", float(success_rate) >= float(gates["min_success_rate"]), float(success_rate), float(gates["min_success_rate"])))

    if err_rate is None:
        out.append(gate("max_error_rate", True, None, float(gates["max_error_rate"])))
    else:
        out.append(gate("max_error_rate", float(err_rate) <= float(gates["max_error_rate"]), float(err_rate), float(gates["max_error_rate"])))

    out.append(gate("min_unique_markets", unique_markets >= int(gates["min_unique_markets"]), unique_markets, int(gates["min_unique_markets"])))

    if top_share is None:
        out.append(gate("max_top_market_share", True, None, float(gates["max_top_market_share"])))
    else:
        out.append(gate("max_top_market_share", float(top_share) <= float(gates["max_top_market_share"]), float(top_share), float(gates["max_top_market_share"])))

    overall = all(g["pass"] for g in out)
    return {"pass": overall, "gates": out}


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--json", action="store_true", help="Emit JSON (default: pretty text)")
    ap.add_argument("--gate", action="store_true", help="Incluir validación PASS/FAIL por umbrales")
    ap.add_argument("--min-decisions", type=int, default=200)
    ap.add_argument("--min-executions", type=int, default=30)
    ap.add_argument("--min-success-rate", type=float, default=0.90)
    ap.add_argument("--max-error-rate", type=float, default=0.02)
    ap.add_argument("--min-unique-markets", type=int, default=10)
    ap.add_argument("--max-top-market-share", type=float, default=0.35)
    args = ap.parse_args()

    rep = summarize(hours=args.hours)
    if args.gate:
        gates_cfg = {
            "min_decisions": args.min_decisions,
            "min_executions": args.min_executions,
            "min_success_rate": args.min_success_rate,
            "max_error_rate": args.max_error_rate,
            "min_unique_markets": args.min_unique_markets,
            "max_top_market_share": args.max_top_market_share,
        }
        rep["gate"] = {"thresholds": gates_cfg, **evaluate_gates(rep, gates_cfg)}
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return

    if not rep.get("ok"):
        print(rep.get("error"))
        return

    print(f"DB: {rep['db']}")
    print(f"Window: last {rep['hours']}h")
    print()
    print("Decisions:")
    for k, v in rep["decision_counts"].items():
        print(f"  {k}: {v}")
    print()
    print("Top reasons:")
    for rr in rep["top_reasons"][:10]:
        print(f"  {rr['reason'] or '(none)'}: {rr['count']}")
    print()
    md = rep["market_diversity"]
    print(f"Market diversity: unique_markets={md['unique_markets']}")
    for tm in md["top_markets"][:10]:
        print(f"  {tm['market_id']}: {tm['count']}")
    print()
    ex = rep["executions"]
    print(f"Executions: total={ex['total']} successful={ex['successful']} success_rate={ex['success_rate']}")
    print()
    gaps = rep["exec_attempt_gaps_sec"]
    print("Execute-attempt gaps (sec):")
    print(f"  count={gaps['count']} p50={gaps['p50']} min={gaps['min']} max={gaps['max']}")
    if "gate" in rep:
        g = rep["gate"]
        verdict = "PASS" if g.get("pass") else "FAIL"
        print()
        print(f"GATE: {verdict}")
        for it in g.get("gates", []):
            print(f"  {it['name']}: {'PASS' if it['pass'] else 'FAIL'} (obs={it['observed']} thr={it['threshold']})")


if __name__ == "__main__":
    main()

