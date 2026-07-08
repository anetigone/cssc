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

## Execution Modes

Use `--execution-mode minimal` for the original proof generator. Use
`--execution-mode structured` for `ChatStructuredActionGenerator`, which emits
typed structured proposals such as RUN_CAPABILITY_TEST, DECOMPOSE,
PROPOSE_ARGUMENT, CHANGE_REPRESENTATION, and IMPLEMENT.

## Fixture Intent

| file | layer | intended signal |
|---|---|---|
| `l1_true_core.lean` | L1 trivial | Measures the fixed structured tax on a one-tactic goal. |
| `l2_feedback_nat_omega.lean` | L2 iterative | Gives a single goal where naive candidates fail but `omega` closes it. |
| `l3_missing_widget_capability.lean` | L3 capability missing | Gives an intentionally under-specified theorem; structured should block when a native capability audit probes the absent witness lemma. |
| `l4_two_helper_logic.lean` | L4 multi-obligation | Gives a parent goal naturally decomposable into two helper facts before the root proof. |
