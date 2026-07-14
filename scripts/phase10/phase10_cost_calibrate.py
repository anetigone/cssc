"""Build a frozen empirical cost-history snapshot from Phase 10 JSONL traces."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.search.cost_ledger import cost_ledger_from_dict
from agent.search.structured.cost_estimator import CostHistorySnapshot, cost_bucket_from_dict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--buckets", type=Path, required=True, help="Frozen action_id -> CostBucket JSON map")
    parser.add_argument("--version", default="phase10-controlled-v1")
    args = parser.parse_args()
    ledgers = []
    for path in args.traces:
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "run_summary":
                ledger = (event.get("metadata") or {}).get("cost_ledger")
                if ledger is not None:
                    ledgers.append(cost_ledger_from_dict(ledger))
    merged = cost_ledger_from_dict({"events": [e.to_dict() for ledger in ledgers for e in ledger.events]})
    raw_buckets = json.loads(args.buckets.read_text(encoding="utf-8"))
    buckets = {str(action_id): cost_bucket_from_dict(value) for action_id, value in raw_buckets.items()}
    snapshot = CostHistorySnapshot.from_completed_ledger(
        merged, snapshot_id=args.version, buckets_by_action_id=buckets,
        estimator_version=args.version,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot.to_dict(), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
