/--
L1 trivial ablation target.

Expected signal:
- minimal should accept with a single proof candidate such as `trivial`;
- structured D1 should also accept, with the extra assembly check and projection
  context cost visible in trace metrics.

No imports: this stays in Lean core for cheap manual smoke runs.
-/
theorem ablation_l1_true_core : True := by
  {{proof}}

