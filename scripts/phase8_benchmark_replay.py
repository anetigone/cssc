"""Controlled-track replay engine for the Phase 8.5 benchmark.

Stage 2 of ``tmp/phase8_5_benchmark_plan.md`` §9. The controlled track drives
the *real* :class:`StructuredController` with two scripted components so the
frontier / reducer / assembly / ResultSummary pipeline stays intact — only the
"generate proposals" and "run Lean" seams are replaced. Different
``frontier_policy`` values (legacy / cost_aware_v1 / cost_aware_v2 /
value_per_cost_v1) then produce genuinely different pop orders and cost
attribution on the same scripted proposal set, which is the causal evidence the
controlled track exists to collect.

Components:

- :class:`ReplayGenerator` — a native ``StructuredActionGenerator`` that emits
  the scenario's deserialized proposals one per ``generate()`` call, in
  sequence, then returns ``[]`` so the controller blocks the branch and stops.
- :class:`ScenarioFakeAdapter` — a ``ProofSystemAdapter`` whose ``check()``
  answers from the scenario's ``expected_check_results`` oracle (substring
  match on the rendered candidate), building real ``CheckResult`` objects so
  the reducer's observation extractors work unchanged.
- :func:`build_replay_controller` — assembles a real ``StructuredController``
  wired to both components.

This module imports from ``agent/`` (read-only dependency); nothing in
``agent/`` imports it, keeping benchmark logic out of the product tree
(plan §11).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from agent.proof_system.base import (
    BudgetSlice,
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    GoalState,
    ParsedFeedback,
    ProgressSignal,
    ProofSystemAdapter,
)
from agent.runtime.workspace import AttemptWorkspace
from agent.search.budget import BudgetConfig
from agent.search.controller.types import ControllerConfig, ExecutionMode
from agent.search.structured.controller.core import StructuredController
from agent.search.structured.proposal.core import (
    StructuredActionProposal,
    structured_action_proposal_from_dict,
)

# ---- oracle parsing -------------------------------------------------------


@dataclass(frozen=True)
class _OracleRule:
    """One ``expected_check_results`` entry as a matchable rule."""

    on_candidate_contains: str
    accepted: bool
    category: DiagnosticCategory


def _parse_oracle(rules: Sequence[dict[str, Any]]) -> tuple[_OracleRule, ...]:
    parsed: list[_OracleRule] = []
    for index, rule in enumerate(rules):
        needle = rule.get("on_candidate_contains")
        if not isinstance(needle, str) or not needle:
            raise ValueError(
                f"expected_check_results[{index}] needs a non-empty "
                f"on_candidate_contains substring"
            )
        category_value = rule.get("category")
        try:
            category = DiagnosticCategory(category_value)
        except ValueError as exc:
            raise ValueError(
                f"expected_check_results[{index}].category {category_value!r} "
                f"is not a DiagnosticCategory value"
            ) from exc
        parsed.append(
            _OracleRule(
                on_candidate_contains=needle,
                accepted=bool(rule.get("accepted")),
                category=category,
            )
        )
    return tuple(parsed)


def _deserialized_proposals(
    proposals: Sequence[dict[str, Any]],
) -> tuple[StructuredActionProposal, ...]:
    return tuple(
        structured_action_proposal_from_dict(proposal) for proposal in proposals
    )


# ---- scripted generator ---------------------------------------------------


class ReplayGenerator:
    """Emit scenario proposals one per ``generate()`` call, in sequence.

    The controller pops one branch per loop iteration and calls ``generate()``
    once per pop. Because ``_finalize_kind`` overwrites every proposal's
    ``target_branch_id`` with the popped branch's id, the scenario's
    ``target_branch_id`` is descriptive only — so this generator does not route
    by branch. It emits proposals strictly in scenario order; the scenarios are
    authored to match the frontier's deterministic pop sequence for the task
    structure (decompose first on root, then helpers in dependency order, then
    the parent branch). When proposals are exhausted it returns ``[]`` and the
    controller blocks the branch, stopping the run when the frontier empties.
    """

    _is_structured_generator = True

    def __init__(
        self, proposals: Sequence[StructuredActionProposal]
    ) -> None:
        self._proposals: tuple[StructuredActionProposal, ...] = tuple(proposals)
        self._cursor = 0
        self._calls = 0

    def generate(
        self, request: Any
    ) -> tuple[StructuredActionProposal, ...]:
        del request  # routing is frontier-driven, not request-driven
        self._calls += 1
        if self._cursor >= len(self._proposals):
            return ()
        proposal = self._proposals[self._cursor]
        self._cursor += 1
        return (proposal,)


# ---- scripted adapter -----------------------------------------------------


# Fallback category when no oracle rule matches a candidate (mirrors the
# StructuredFakeAdapter fallback in tests/test_structured_controller.py).
_DEFAULT_CATEGORY = DiagnosticCategory.UNSOLVED_GOALS

# A non-empty fingerprint so observations_from_check_result emits per-goal
# observations with a stable identity (needed by stall / frontier signals).
_CONTROLLED_FINGERPRINT = "fp-controlled"


class ScenarioFakeAdapter(ProofSystemAdapter):
    """Adapter whose verdict is driven by the scenario's check-result oracle.

    ``render_candidate`` substitutes the proposed proof text into the task's
    hole marker (identical to ``StructuredFakeAdapter``). The oracle matches
    its ``on_candidate_contains`` substrings against the **candidate proof
    text** (the ``edit.text`` substituted into the hole), NOT the full rendered
    source — otherwise fixture docstrings that name tactics ("such as ``rfl``,
    ``simp``") pollute the match and the first-named tactic always wins. Each
    category builds a real ``CheckResult`` with the ``ParsedFeedback`` the
    reducer's observation extractors expect.
    """

    def __init__(self, rules: Sequence[_OracleRule]) -> None:
        self._rules: tuple[_OracleRule, ...] = tuple(rules)
        self.checked_files: list[Path] = []
        # Proof text of the most recent render_candidate call. Controlled runs
        # are strictly serial (render -> write -> check -> next pop), so the
        # check that immediately follows a render always sees the matching text
        # without needing the candidate path (unknown at render time).
        self._last_proof_text: str = ""

    def render_candidate(
        self,
        task: Any,
        candidate_edit: CandidateEdit,
        *,
        holes: tuple[int, ...] | None = None,
    ) -> str:
        del holes  # single active marker; multi-hole not used by controlled track
        self._last_proof_text = candidate_edit.text
        return task.source_template.replace(task.hole_marker, candidate_edit.text)

    def subprocess_clone(self) -> "ScenarioFakeAdapter":
        return self

    def check(self, candidate_file: Path, budget_slice: BudgetSlice) -> CheckResult:
        del budget_slice  # no real timeout; verdict is instantaneous
        self.checked_files.append(candidate_file)
        # Match against the proof text first (the segment substituted into the
        # hole), running the FULL rule list against it, so a fixture docstring
        # that names tactics ("such as rfl, simp") cannot pre-empt the real
        # verdict. Only if no rule matches the proof text do we fall back to
        # the rendered source — capability probes skip render_candidate, so
        # _last_proof_text is stale for them; their source embeds the probe
        # signature, which the oracle matches there.
        matched = self._match(self._last_proof_text)
        if matched is None:
            try:
                matched = self._match(
                    candidate_file.read_text(encoding="utf-8")
                )
            except OSError:
                pass
        category = matched.category if matched is not None else _DEFAULT_CATEGORY
        accepted = bool(matched.accepted) if matched is not None else False
        return self._check_result(candidate_file, category, accepted)

    def _match(self, haystack: str) -> _OracleRule | None:
        for rule in self._rules:
            if rule.on_candidate_contains in haystack:
                return rule
        return None

    def parse_feedback(self, raw_output: str) -> ParsedFeedback:
        return ParsedFeedback(
            category=_DEFAULT_CATEGORY, message=raw_output, raw_output=raw_output
        )

    def extract_progress(
        self, parent_state: Any, check_result: CheckResult
    ) -> ProgressSignal:
        del parent_state
        return ProgressSignal(diagnostic_category=check_result.category)

    @staticmethod
    def _check_result(
        candidate_file: Path, category: DiagnosticCategory, accepted: bool
    ) -> CheckResult:
        if accepted or category is DiagnosticCategory.PROOF_ACCEPTED:
            feedback = ParsedFeedback(
                category=DiagnosticCategory.PROOF_ACCEPTED, message="ok"
            )
            return CheckResult(
                accepted=True,
                category=DiagnosticCategory.PROOF_ACCEPTED,
                raw_output="",
                candidate_file=candidate_file,
                parsed_feedback=feedback,
            )

        if category is DiagnosticCategory.UNKNOWN_IDENTIFIER:
            # Capability-gap path: _apply_capability_audit blocks the
            # obligation when the category is in the capability-missing set;
            # declaration_id is surfaced in the observation message.
            goal = GoalState(
                text="unknown identifier",
                goal_fingerprint=_CONTROLLED_FINGERPRINT,
                declaration_id="widgetGood",
            )
        else:
            # unsolved_goals / tactic_failed / etc. — a single finger-printed
            # goal so stall and frontier signals have a stable identity.
            goal = GoalState(
                text="unsolved",
                goal_fingerprint=_CONTROLLED_FINGERPRINT,
            )
        feedback = ParsedFeedback(
            category=category,
            message=category.value,
            goal_state=(goal,),
        )
        return CheckResult(
            accepted=False,
            category=category,
            raw_output=category.value,
            candidate_file=candidate_file,
            parsed_feedback=feedback,
        )


# ---- controller factory ---------------------------------------------------


def build_replay_controller(
    *,
    scenario: dict[str, Any],
    frontier_policy: str,
    budget_config: BudgetConfig,
    workspace_root: str | Path,
) -> tuple[StructuredController, ReplayGenerator, ScenarioFakeAdapter]:
    """Assemble a real ``StructuredController`` wired to scripted components.

    The controller keeps its real frontier / reducer / assembly / ResultSummary
    pipeline; only generation and checking are scripted. ``max_candidates_per_
    model_call=1`` matches the one-proposal-per-pop contract the scenarios
    assume.
    """
    proposals = _deserialized_proposals(scenario.get("proposals", []))
    rules = _parse_oracle(scenario.get("expected_check_results", []))
    generator = ReplayGenerator(proposals)
    adapter = ScenarioFakeAdapter(rules)
    controller = StructuredController(
        adapter=adapter,
        action_generator=generator,
        workspace=AttemptWorkspace(workspace_root),
        budget_config=budget_config,
        config=ControllerConfig(
            execution_mode=ExecutionMode.STRUCTURED,
            frontier_policy=frontier_policy,
            max_candidates_per_model_call=1,
        ),
    )
    return controller, generator, adapter
