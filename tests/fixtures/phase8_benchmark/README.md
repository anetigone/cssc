# Phase 8.5 Benchmark Fixtures (Stage 0)

Curated canary task set for the cost-aware / value-per-cost ablation described in
[`tmp/phase8_5_benchmark_plan.md`](../../../tmp/phase8_5_benchmark_plan.md). This is **not** a pytest
suite — it is a human-run benchmark. The data lives here; the orchestration lives in `scripts/`.

Stage 0 ships **6 canaries** (L1–L6, one per layer). The remaining 18 tasks are added in a later
stage after pilot (see plan §9). Each task is one row in `manifest.jsonl`; the `.lean` file holds
only the theorem scaffold, all evaluation semantics live in the manifest.

## Layout

```text
manifest.jsonl        # authoritative suite entry — one task per line
l{1..6}_canary_*.lean  # theorem scaffolds (pure Lean core, one `{{proof}}` hole each)
scenarios/*.json       # controlled-track scripted proposal sequences (Stage 2 input)
```

## Expected terminals

The `expected_terminal` field uses `WorkspaceStatus` values (`accepted` / `partial` / `blocked`).
There is **no** `explainably_blocked` status in this codebase — L3's explainable block is the plain
`blocked` terminal plus the `expected_block_category` / `expected_probe_signature` evidence fields.

## Validation (no model calls)

```bash
python scripts/phase8_benchmark_validate.py -v
python scripts/phase8_benchmark_validate.py --skip-lean-smoke   # static checks only
```

See `scenarios/README.md` for the controlled-scenario schema.
