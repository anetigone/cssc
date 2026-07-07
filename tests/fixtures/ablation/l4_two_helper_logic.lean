/--
L4 multi-obligation ablation target.

The root theorem can be decomposed into two reusable helper facts:

- helper 1: prove `Q` from `hPQ` and `hP`;
- helper 2: prove `R` from `hPR` and `hP`;
- root: combine those helpers with `hQRS`.

Minimal may still solve this if the proof generator finds the direct term, but
structured D2 should expose the value of DECOMPOSE plus accepted helper reuse in
`metadata.result_summary.accepted_obligations`.
-/
theorem ablation_l4_two_helper_logic
    (P Q R S : Prop)
    (hPQ : P -> Q)
    (hPR : P -> R)
    (hQRS : Q -> R -> S)
    (hP : P) : S := by
  {{proof}}

