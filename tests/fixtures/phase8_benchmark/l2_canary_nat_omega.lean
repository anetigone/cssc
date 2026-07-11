/--
L2 repeated-repair canary (pure Lean core, no Mathlib import).

Expected terminal: accepted.

Primary signal: a single goal where naive first guesses such as `rfl`, `simp`,
or applying the hypothesis directly fail, and a later candidate using `omega`
closes it. The controlled scenario fixes `wrong_1 -> wrong_2 -> correct`; the
live track only requires the legacy median attempt count to land in 2-4.
-/
theorem l2_canary_nat_omega
    (a b : Nat)
    (h : a + 2 = b + 2) : a = b := by
  {{proof}}
