/-- Slightly harder end-to-end smoke target: needs a real decision tactic
(`omega`, built into Lean 4 core since 4.30) rather than `trivial`, so it
exercises marker replacement and candidate rendering more fully.

No `import Mathlib` — see `Basic.lean` for why the smoke stays in core. -/
theorem e2e_succ (n : Nat) : 0 < n + 1 := by
  {{proof}}
