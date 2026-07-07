/--
L3 capability-missing ablation target.

The constants below intentionally give the theorem no available proof path in
the local environment. A minimal run is expected to keep failing or exhaust
candidates. A full D2 structured run should use a native
RUN_CAPABILITY_TEST proposal to probe a missing bridge lemma such as
`WidgetGood target`; the resulting UNKNOWN_IDENTIFIER/INVALID_REFERENCE
observation should block the branch and obligation with an explainable reason.

This fixture is not intended to be accepted.
-/
namespace AblationL3

constant Widget : Type
constant Good : Widget -> Prop
constant target : Widget

theorem missing_widget_capability : Good target := by
  {{proof}}

end AblationL3

