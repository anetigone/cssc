/--
L4 decompose-helper canary (pure Lean core, no Mathlib import).

Expected terminal: accepted.

Primary signal: the root theorem decomposes into two reusable helper facts
before the root proof:

- helper 1: prove `Q` from `hPQ` and `hP`;
- helper 2: prove `R` from `hPR` and `hP`;
- root: combine those helpers with `hQRS`.

Structured should expose the value of DECOMPOSE plus accepted helper reuse in
`metadata.result_summary.accepted_obligations`.
-/
theorem l4_canary_two_helpers
    (P Q R S : Prop)
    (hPQ : P -> Q)
    (hPR : P -> R)
    (hQRS : Q -> R -> S)
    (hP : P) : S := by
  {{proof}}
