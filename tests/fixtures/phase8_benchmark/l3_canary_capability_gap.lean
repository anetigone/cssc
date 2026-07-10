/-
L3 capability/import-gap canary (pure Lean core, no Mathlib import).

Expected terminal: blocked.

Primary signal: the constants below intentionally give the theorem no available
proof path in the local environment. Minimal is expected to keep failing or
exhaust candidates. Structured should use a native RUN_CAPABILITY_TEST proposal
to probe a missing bridge lemma such as `widgetGood target`; the resulting
UNKNOWN_IDENTIFIER observation should block the branch and obligation with an
explainable reason.

This fixture is not intended to be accepted.
-/
namespace CanaryL3

axiom widget : Type
axiom good : widget -> Prop
axiom target : widget

theorem l3_canary_capability_gap : good target := by
  {{proof}}

end CanaryL3
