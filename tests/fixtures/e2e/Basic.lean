/-- Trivial end-to-end smoke target: a single tactic (`trivial`) closes it.
Used by the deterministic Lean smoke documented in `tmp/phase7_plan.md`.

Deliberately avoids `import Mathlib`: importing mathlib forces elaboration
of the whole library (~3 min per file here), which defeats the point of a
fast smoke. The goal is to exercise the CLI → task builder → controller →
Lean checker → trace pipeline deterministically, not to test Mathlib. -/
theorem e2e_true : True := by
  {{proof}}
