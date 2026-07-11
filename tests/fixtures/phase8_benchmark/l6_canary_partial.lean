/--
L6 partial-result canary (pure Lean core, no Mathlib import).

Expected terminal: partial (one of two helpers accepted, root left open).

Primary signal: partial-result quality and cost attribution. The root chains
`P -> Q -> R`. A structured DECOMPOSE yields helper1 (prove `Q`) + helper2
(prove `R` from `Q`) + root. The controlled scenario caps the budget so helper2
stays OPEN at stop, producing `workspace_status=partial` with
accepted=[helper1], open=[helper2, root].

The partial result is manufactured by the controlled scenario's budget cutoff,
NOT by anything in this scaffold — the theorem itself is provable.
-/
theorem l6_canary_partial_root
    (P Q R : Prop)
    (hPQ : P → Q)
    (hQR : Q → R)
    (hP : P) : R := by
  {{proof}}
