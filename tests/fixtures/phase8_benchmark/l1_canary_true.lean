/--
L1 trivial canary (pure Lean core, no Mathlib import).

Expected terminal: accepted.

Primary signal: the fixed structured tax on a one-tactic goal. Minimal should
accept with a single candidate such as `trivial`; structured should also accept,
with the extra assembly check and projection context cost visible in trace
metrics.
-/
theorem l1_canary_true : True := by
  {{proof}}
