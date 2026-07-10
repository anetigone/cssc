/--
L5 representation-change canary (pure Lean core, no Mathlib import).

Expected terminal: accepted.

Primary signal: whether a representation switch trades a little structural cost
for fewer failures. The goal states list equality directly, but `ls1` and `ls2`
are not definitionally equal, so `rfl` and a naive `simp` fail. Closing it
requires switching to the pointwise-membership representation (the two given
forall hypotheses) and applying list extensionality.

A/B evidence (machine-checked at pilot time via `validate --ab-evidence`):

- old representation fails: `rfl`, `simp [h1]`;
- new representation succeeds: unfold to forall membership and close via
  extensionality (e.g. `ext x; constructor <;> intro hx; simp_all`, or a core
  list extensionality lemma — the exact name is pinned during pilot, not here).

`ab_evidence` lives in `manifest.jsonl`; the proof candidate name is NOT hard
-coded into this scaffold.
-/
theorem l5_canary_rep_change
    (ls1 ls2 : List Nat)
    (h1 : ∀ x, x ∈ ls1 → x ∈ ls2)
    (h2 : ∀ x, x ∈ ls2 → x ∈ ls1) :
    ls1 = ls2 := by
  {{proof}}
