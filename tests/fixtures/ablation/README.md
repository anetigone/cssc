# Phase 7.8 Ablation Fixtures

These fixtures are for the manual minimal-vs-structured ablation described in
`tmp/phase7_8_plan.md`. They are deliberately small and curated; they are not a
pytest suite.

Run them with the same budgets across modes. Example:

```bash
python -m agent.cli.app prove tests/fixtures/ablation/l1_true_core.lean \
  --project-root lean_workspace \
  --execution-mode minimal \
  --max-model-calls 16 \
  --max-checks 20 \
  --lean-timeout 60 \
  --trace-jsonl .runs/ablation/d0/l1_true_core.jsonl
```

## Current CLI Gap

As of this fixture set, `agent/cli/parser.py` and `agent/cli/generators.py` do
not expose a flag for a native `StructuredActionGenerator`. A structured CLI
run therefore wraps the ordinary proof `ActionGenerator` via
`adapt_legacy_generator`, producing IMPLEMENT proposals only. That is a valid
D1 run, but it is not a full D2 run because capability/decompose/argument
proposals cannot yet be emitted from the CLI.

Until that entry point exists, D2 should be exercised either through a small
script/test harness that injects a native generator directly into
`StructuredController`, or postponed.

## Fixture Intent

| file | layer | intended signal |
|---|---|---|
| `l1_true_core.lean` | L1 trivial | Measures the fixed structured tax on a one-tactic goal. |
| `l2_feedback_nat_omega.lean` | L2 iterative | Gives a single goal where naive candidates fail but `omega` closes it. |
| `l3_missing_widget_capability.lean` | L3 capability missing | Gives an intentionally under-specified theorem; D2 should block when a native capability audit probes the absent witness lemma. |
| `l4_two_helper_logic.lean` | L4 multi-obligation | Gives a parent goal naturally decomposable into two helper facts before the root proof. |

