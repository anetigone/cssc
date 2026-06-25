from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent.proof_system.base import (
    BudgetSlice,
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    GoalState,
    ParsedFeedback,
    ProgressSignal,
    ProofSystemAdapter,
    ProofTask,
)
from agent.runtime.workspace import AttemptWorkspace
from agent.search.action import ActionCandidate, ActionGenerationRequest
from agent.search.budget import BudgetConfig
from agent.search.controller import ControllerConfig
from agent.search.execution import ExecutionMode
from agent.search.safety import SafetyVerdict
from agent.search.structured import StructuredController


def _task() -> ProofTask:
    return ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")


class StructuredFakeAdapter(ProofSystemAdapter):
    """Adapter whose verdict depends on the candidate's proof text.

    ``trivial`` → accepted; ``stuck`` → unsolved with a fixed goal fingerprint
    (so stall / repair-fork detection fires); otherwise plain unsolved.
    """

    def __init__(self) -> None:
        self.checked_files: list[Path] = []

    def render_candidate(self, task: ProofTask, edit: CandidateEdit) -> str:
        return task.source_template.replace(task.hole_marker, edit.text)

    def check(self, candidate_file: Path, budget_slice: BudgetSlice) -> CheckResult:
        self.checked_files.append(candidate_file)
        source = candidate_file.read_text(encoding="utf-8")
        if "trivial" in source:
            return CheckResult(
                accepted=True,
                category=DiagnosticCategory.PROOF_ACCEPTED,
                raw_output="",
                candidate_file=candidate_file,
                parsed_feedback=ParsedFeedback(
                    category=DiagnosticCategory.PROOF_ACCEPTED, message="ok"
                ),
            )
        if "stuck" in source:
            goal = GoalState(text="unsolved", goal_fingerprint="fp-stuck")
            return CheckResult(
                accepted=False,
                category=DiagnosticCategory.UNSOLVED_GOALS,
                raw_output="unsolved",
                candidate_file=candidate_file,
                parsed_feedback=ParsedFeedback(
                    category=DiagnosticCategory.UNSOLVED_GOALS,
                    message="unsolved",
                    goal_state=(goal,),
                ),
            )
        return CheckResult(
            accepted=False,
            category=DiagnosticCategory.UNSOLVED_GOALS,
            raw_output="unsolved",
            candidate_file=candidate_file,
            parsed_feedback=ParsedFeedback(
                category=DiagnosticCategory.UNSOLVED_GOALS, message="unsolved"
            ),
        )

    def parse_feedback(self, raw_output: str) -> ParsedFeedback:
        return ParsedFeedback(category=DiagnosticCategory.UNKNOWN, raw_output=raw_output)

    def extract_progress(
        self, parent_state: Any, check_result: CheckResult
    ) -> ProgressSignal:
        return ProgressSignal(diagnostic_category=check_result.category)


class QueueGenerator:
    """Pop one batch of proof texts per ``generate`` call."""

    def __init__(self, batches: list[list[str]]) -> None:
        self.batches = list(batches)
        self.requests: list[ActionGenerationRequest] = []

    def generate(self, request: ActionGenerationRequest):
        self.requests.append(request)
        if not self.batches:
            return []
        return [
            ActionCandidate(proof_text=text, action="queued")
            for text in self.batches.pop(0)
        ]


class RejectProofTextReviewer:
    def __init__(self, rejected_text: str) -> None:
        self.rejected_text = rejected_text

    def accepts(self, task, candidate_source, check_result) -> SafetyVerdict:
        del task, check_result
        if self.rejected_text in candidate_source:
            return SafetyVerdict(False, ("test_shortcut",))
        return SafetyVerdict(True)


class StructuredControllerTests(unittest.TestCase):
    def _controller(
        self,
        tmp: str,
        generator: QueueGenerator,
        *,
        budget: BudgetConfig | None = None,
        safety_reviewer: Any = None,
        adapter: ProofSystemAdapter | None = None,
        max_candidates: int = 1,
        retriever: Any = None,
        context_summarizer: Any = None,
    ) -> StructuredController:
        return StructuredController(
            adapter=adapter or StructuredFakeAdapter(),
            action_generator=generator,
            workspace=AttemptWorkspace(tmp),
            budget_config=budget or BudgetConfig(max_checks=8, max_model_calls=8),
            config=ControllerConfig(
                execution_mode=ExecutionMode.STRUCTURED,
                max_candidates_per_model_call=max_candidates,
            ),
            safety_reviewer=safety_reviewer,
            retriever=retriever,
            context_summarizer=context_summarizer,
        )

    def test_accepted_path_serializes_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp, QueueGenerator([["trivial"]])
            )
            result = controller.run(_task())

        self.assertTrue(result.accepted)
        self.assertEqual(result.stop_reason, "accepted")
        self.assertEqual(result.metrics.execution_mode, ExecutionMode.STRUCTURED)
        self.assertIn("workspace", result.metadata)
        # The workspace snapshot records the accepted root branch.
        workspace = result.metadata["workspace"]
        self.assertTrue(any(b["status"] == "accepted" for b in workspace["branches"]))
        # Assembly consumed one extra check on top of the single attempt.
        self.assertEqual(result.metrics.budget_checks_used, 2)

    def test_generation_metadata_carries_context_projection(self) -> None:
        generator = QueueGenerator([["trivial"]])
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp, generator)
            controller.run(_task())

        # The controller derives the structured context projection in
        # _generation_metadata and serializes it for the shared prompt renderer.
        self.assertTrue(generator.requests)
        projection = generator.requests[0].metadata["structured_projection"]
        self.assertEqual(projection["branch_id"], "sample:root")
        self.assertIsNotNone(projection["current_obligation"])
        self.assertEqual(projection["current_obligation"]["obligation_id"], "sample")
        # branch_obligation / verified_facts are derived from the same projection.
        self.assertEqual(
            generator.requests[0].metadata["branch_obligation"]["obligation_id"],
            "sample",
        )

    def test_budget_exhaustion_returns_unaccepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                QueueGenerator([["fail"], ["fail"], ["fail"], ["fail"]]),
                budget=BudgetConfig(max_checks=2, max_model_calls=2),
            )
            result = controller.run(_task())

        self.assertFalse(result.accepted)
        self.assertTrue(result.stop_reason.startswith("budget"))
        self.assertEqual(len(result.attempts), 2)

    def test_repair_child_spawns_on_repeated_stall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                # Three identical stalled attempts: the second spawns a REPAIR
                # child (root-branch.r0), the third retires the parent to
                # DORMANT. The child has no generator batches left, so the run
                # ends with no_actions.
                QueueGenerator([["stuck"], ["stuck"], ["stuck"]]),
                budget=BudgetConfig(max_checks=8, max_model_calls=8),
            )
            result = controller.run(_task())

        self.assertFalse(result.accepted)
        branch_ids = {b["branch_id"] for b in result.metadata["workspace"]["branches"]}
        self.assertIn("sample:root.r0", branch_ids)

    def test_safety_rejection_keeps_branch_active(self) -> None:
        reviewer = RejectProofTextReviewer("trivial")
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                QueueGenerator([["trivial"], ["fail"]]),
                budget=BudgetConfig(max_checks=8, max_model_calls=8),
                safety_reviewer=reviewer,
            )
            result = controller.run(_task())

        self.assertFalse(result.accepted)
        # The first attempt was checker-accepted but safety-rejected; that is
        # recorded as a safety rejection, not a successful assembly.
        self.assertTrue(result.metadata["safety_rejections"])

    def test_tool_unavailable_short_circuits(self) -> None:
        class ToolUnavailableAdapter(StructuredFakeAdapter):
            def check(self, candidate_file, budget_slice):
                return CheckResult(
                    accepted=False,
                    category=DiagnosticCategory.TOOL_UNAVAILABLE,
                    raw_output="no lean",
                    candidate_file=candidate_file,
                )

        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                QueueGenerator([["fail"]]),
                budget=BudgetConfig(max_checks=8, max_model_calls=8),
                adapter=ToolUnavailableAdapter(),
            )
            result = controller.run(_task())

        self.assertFalse(result.accepted)
        self.assertEqual(result.stop_reason, "tool_unavailable")
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.metrics.budget_checks_used, 1)
        observations = result.metadata["workspace"]["branches"][0]["observations"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["category"], "tool_unavailable")

    def test_checks_all_candidates_and_preserves_or_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                QueueGenerator([["fail", "trivial"]]),
                max_candidates=2,
                budget=BudgetConfig(max_checks=3, max_model_calls=1),
            )
            result = controller.run(_task())

        self.assertTrue(result.accepted)
        self.assertEqual(len(result.attempts), 2)
        branches = result.metadata["workspace"]["branches"]
        self.assertEqual(len(branches), 2)
        self.assertTrue(any(branch["parent_branch_id"] for branch in branches))

    def test_final_assembly_inserts_proof_snippet_only_once(self) -> None:
        class StrictSourceAdapter(StructuredFakeAdapter):
            def check(self, candidate_file, budget_slice):
                source = candidate_file.read_text(encoding="utf-8")
                if source.count("theorem sample") != 1:
                    return CheckResult(
                        accepted=False,
                        category=DiagnosticCategory.CHECKER_ERROR,
                        raw_output="nested theorem",
                        candidate_file=candidate_file,
                    )
                return super().check(candidate_file, budget_slice)

        with tempfile.TemporaryDirectory() as tmp:
            result = self._controller(
                tmp,
                QueueGenerator([["trivial"]]),
                adapter=StrictSourceAdapter(),
            ).run(_task())

        self.assertTrue(result.accepted)

    def test_no_actions_blocks_only_selected_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp, QueueGenerator([]))
            result = controller.run(_task())

        self.assertFalse(result.accepted)
        self.assertEqual(result.stop_reason, "no_actions")
        self.assertEqual(
            result.metadata["workspace"]["branches"][0]["status"],
            "blocked",
        )

    def test_assembly_reserves_its_own_check_budget(self) -> None:
        # max_checks=2: one attempt + the assembly recheck. The run must still
        # reach assembly because has_complete_solution fires after attempt 1.
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                QueueGenerator([["trivial"]]),
                budget=BudgetConfig(max_checks=2, max_model_calls=2),
            )
            result = controller.run(_task())

        self.assertTrue(result.accepted)
        self.assertEqual(result.metrics.budget_checks_used, 2)

    def test_rejects_non_structured_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                StructuredController(
                    adapter=StructuredFakeAdapter(),
                    action_generator=QueueGenerator([]),
                    workspace=AttemptWorkspace(tmp),
                    config=ControllerConfig(execution_mode=ExecutionMode.MINIMAL),
                )

    def test_legacy_generator_finalizes_kind(self) -> None:
        # A legacy ActionGenerator (ActionCandidate) is adapted; the controller
        # finalizes IMPLEMENT on the first attempt of a branch and
        # REPAIR_IMPLEMENTATION on the second (reproducing _pick_action).
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(
                tmp,
                QueueGenerator([["fail"], ["trivial"]]),
                budget=BudgetConfig(max_checks=8, max_model_calls=8),
            )
            result = controller.run(_task())

        kinds = [att.edit.metadata["structured_action_kind"] for att in result.attempts]
        self.assertEqual(kinds, ["implement", "repair_implementation"])

    def test_native_structured_generator_is_not_re_wrapped(self) -> None:
        # A native StructuredActionGenerator carries finalized IMPLEMENT
        # proposals; _finalize_kind must be a no-op (no LEGACY_KIND_DEFERRED),
        # and the controller must accept it without re-wrapping.
        from agent.proof_system.workspace.action import (
            DEFAULT_ALLOWED_MUTATIONS,
            SearchAction,
            SearchActionKind,
        )
        from agent.search.structured.proposal import (
            ImplementPayload,
            StructuredActionProposal,
        )

        class NativeImplementGenerator:
            _is_structured_generator = True

            def __init__(self) -> None:
                self.requests: list[ActionGenerationRequest] = []

            def generate(self, request: ActionGenerationRequest):
                self.requests.append(request)
                action = SearchAction(
                    kind=SearchActionKind.IMPLEMENT,
                    target_branch_id="sample:root",
                    allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                        SearchActionKind.IMPLEMENT
                    ],
                    rationale="native implement",
                )
                return (
                    StructuredActionProposal(
                        action=action,
                        payload=ImplementPayload(proof_text="trivial"),
                    ),
                )

        native = NativeImplementGenerator()
        with tempfile.TemporaryDirectory() as tmp:
            controller = self._controller(tmp, native)  # type: ignore[arg-type]
            # Constructor adapted the generator — but a native generator
            # declares _is_structured_generator, so it is used as-is.
            self.assertIs(controller.action_generator, native)
            result = controller.run(_task())

        self.assertTrue(result.accepted)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(
            result.attempts[0].edit.metadata["structured_action_kind"], "implement"
        )
        # The skipped-proposals log stays empty: only IMPLEMENT was emitted.
        self.assertEqual(result.metadata["skipped_proposals"], ())

    def test_native_multi_candidate_actions_are_retargeted_to_materialized_branches(self) -> None:
        from agent.proof_system.workspace.action import (
            DEFAULT_ALLOWED_MUTATIONS,
            SearchAction,
            SearchActionKind,
        )
        from agent.search.structured.proposal import (
            ImplementPayload,
            StructuredActionProposal,
        )

        class NativeMultiImplementGenerator:
            _is_structured_generator = True

            def generate(self, request: ActionGenerationRequest):
                def proposal(proof_text: str) -> StructuredActionProposal:
                    return StructuredActionProposal(
                        action=SearchAction(
                            kind=SearchActionKind.IMPLEMENT,
                            target_branch_id="sample:root",
                            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                                SearchActionKind.IMPLEMENT
                            ],
                            rationale="native implement",
                        ),
                        payload=ImplementPayload(proof_text=proof_text),
                    )

                return (proposal("fail"), proposal("trivial"))

        with tempfile.TemporaryDirectory() as tmp:
            result = self._controller(
                tmp,
                NativeMultiImplementGenerator(),  # type: ignore[arg-type]
                max_candidates=2,
                budget=BudgetConfig(max_checks=3, max_model_calls=1),
            ).run(_task())

        self.assertTrue(result.accepted)
        for branch in result.metadata["workspace"]["branches"]:
            last_action = branch.get("last_action")
            if last_action is not None:
                self.assertEqual(last_action["target_branch_id"], branch["branch_id"])

    def test_native_decompose_executes_and_structures_the_workspace(self) -> None:
        # Phase 7.4: a native StructuredActionGenerator emitting a DECOMPOSE
        # proposal is executed (not skipped). The root obligation is split into
        # a helper, the old root branch is superseded, and new branches are
        # seeded for the helper and the new parent version. The decompose is
        # recorded under ``decompose_records`` and never reaches
        # ``skipped_proposals``.
        from agent.proof_system.workspace.action import (
            DEFAULT_ALLOWED_MUTATIONS,
            SearchAction,
            SearchActionKind,
        )
        from agent.search.structured.proposal import (
            DecomposeChildSpec,
            DecomposePayload,
            StructuredActionProposal,
        )

        class NativeDecomposeGenerator:
            _is_structured_generator = True

            def __init__(self) -> None:
                self._fired = False

            def generate(self, request: ActionGenerationRequest):
                if self._fired:
                    return ()
                self._fired = True
                return (
                    StructuredActionProposal(
                        action=SearchAction(
                            kind=SearchActionKind.DECOMPOSE,
                            target_branch_id="sample:root",
                            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                                SearchActionKind.DECOMPOSE
                            ],
                            rationale="split first",
                        ),
                        payload=DecomposePayload(
                            children=(
                                DecomposeChildSpec(
                                    child_id="helper",
                                    statement="helper statement",
                                ),
                            )
                        ),
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            result = self._controller(
                tmp,
                NativeDecomposeGenerator(),  # type: ignore[arg-type]
                budget=BudgetConfig(max_checks=3, max_model_calls=2),
            ).run(_task())

        self.assertFalse(result.accepted)
        branches = result.metadata["workspace"]["branches"]
        # Old root branch (superseded) + new parent branch + one helper branch.
        self.assertGreater(len(branches), 1)
        branch_ids = [b["branch_id"] for b in branches]
        self.assertTrue(
            any(b["status"] == "superseded" and b["branch_id"] == "sample:root" for b in branches)
        )
        self.assertTrue(any("helper" in bid for bid in branch_ids))
        # The decompose was executed, not skipped.
        self.assertEqual(len(result.metadata["decompose_records"]), 1)
        self.assertEqual(
            result.metadata["decompose_records"][0]["children"][0]["child_id"],
            "helper",
        )
        self.assertEqual(result.metadata["skipped_proposals"], ())

    def test_multi_obligation_decompose_then_helpers_then_root_assembles(self) -> None:
        # Phase 7.4 end-to-end: a native generator decomposes the root into a
        # helper, the helper is implemented and accepted, the new parent version
        # (now ready, its dependency closed) is implemented and accepted, and the
        # final whole-source assembly passes. This is the AND-OR search closing.
        from agent.proof_system.workspace.action import (
            DEFAULT_ALLOWED_MUTATIONS,
            SearchAction,
            SearchActionKind,
        )
        from agent.search.structured.proposal import (
            DecomposeChildSpec,
            DecomposePayload,
            ImplementPayload,
            StructuredActionProposal,
        )

        helper_statement = "lemma helper : True := by\n  {{proof}}\n"

        class MultiObligationGenerator:
            _is_structured_generator = True

            def generate(self, request: ActionGenerationRequest):
                branch_id = request.metadata.get("branch_id", "")
                if branch_id == "sample:root":
                    return (
                        StructuredActionProposal(
                            action=SearchAction(
                                kind=SearchActionKind.DECOMPOSE,
                                target_branch_id=branch_id,
                                allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                                    SearchActionKind.DECOMPOSE
                                ],
                                rationale="split into helper",
                            ),
                            payload=DecomposePayload(
                                children=(
                                    DecomposeChildSpec(
                                        child_id="helper",
                                        statement=helper_statement,
                                    ),
                                )
                            ),
                        ),
                    )
                # Helper branch and new parent branch: implement with trivial.
                return (
                    StructuredActionProposal(
                        action=SearchAction(
                            kind=SearchActionKind.IMPLEMENT,
                            target_branch_id=branch_id,
                            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                                SearchActionKind.IMPLEMENT
                            ],
                            rationale="implement",
                        ),
                        payload=ImplementPayload(proof_text="trivial"),
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            result = self._controller(
                tmp,
                MultiObligationGenerator(),  # type: ignore[arg-type]
                budget=BudgetConfig(max_checks=6, max_model_calls=6),
            ).run(_task())

        self.assertTrue(result.accepted)
        self.assertEqual(result.stop_reason, "accepted")
        summary = result.metadata["result_summary"]
        # Root + helper both accepted; nothing left open or blocked.
        accepted_ids = {o["obligation_id"] for o in summary["accepted_obligations"]}
        self.assertIn("sample", accepted_ids)
        self.assertIn("helper", accepted_ids)
        self.assertEqual(summary["open_obligations"], [])
        self.assertEqual(summary["blocked_obligations"], [])
        self.assertTrue(summary["assembly"]["executed"])
        self.assertTrue(summary["assembly"]["accepted"])
        # The helper fact carries its rendered declaration for parent reuse.
        helper_facts = [
            f for f in result.metadata["workspace"]["accepted_facts"]
            if f["obligation_id"] == "helper"
        ]
        self.assertEqual(len(helper_facts), 1)
        self.assertIn("lemma helper", helper_facts[0]["statement"])

    def test_capability_audit_blocks_route_when_capability_missing(self) -> None:
        # Phase 7.3: a native generator proposes RUN_CAPABILITY_TEST. The audit
        # renders the signature and checks it; an UNKNOWN_IDENTIFIER result
        # blocks the branch AND the obligation, closes the result-summary gap,
        # and the loop terminates without spending an IMPLEMENT attempt.
        from agent.proof_system.workspace.action import (
            DEFAULT_ALLOWED_MUTATIONS,
            SearchAction,
            SearchActionKind,
        )
        from agent.proof_system.workspace.obligation import ObligationStatus
        from agent.search.structured.proposal import (
            CapabilityTestPayload,
            StructuredActionProposal,
        )

        class CapabilityProbeGenerator:
            _is_structured_generator = True

            def generate(self, request: ActionGenerationRequest):
                return (
                    StructuredActionProposal(
                        action=SearchAction(
                            kind=SearchActionKind.RUN_CAPABILITY_TEST,
                            target_branch_id="sample:root",
                            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                                SearchActionKind.RUN_CAPABILITY_TEST
                            ],
                            rationale="probe tactic#simp",
                        ),
                        payload=CapabilityTestPayload(
                            requirement="tactic#simp",
                            signature="by simp",
                        ),
                    ),
                )

        class MissingCapabilityAdapter(StructuredFakeAdapter):
            def check(self, candidate_file, budget_slice):
                self.checked_files.append(candidate_file)
                return CheckResult(
                    accepted=False,
                    category=DiagnosticCategory.UNKNOWN_IDENTIFIER,
                    raw_output="unknown identifier 'simp'",
                    candidate_file=candidate_file,
                    parsed_feedback=ParsedFeedback(
                        category=DiagnosticCategory.UNKNOWN_IDENTIFIER,
                        message="unknown identifier 'simp'",
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            result = self._controller(
                tmp,
                CapabilityProbeGenerator(),  # type: ignore[arg-type]
                adapter=MissingCapabilityAdapter(),
            ).run(_task())

        self.assertFalse(result.accepted)
        # The capability attempt was recorded (1 check), no IMPLEMENT followed.
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].edit.action, "capability_test")

        # The branch and obligation are blocked together — no gap.
        branch = result.metadata["workspace"]["branches"][0]
        self.assertEqual(branch["status"], "blocked")
        obligation = result.metadata["workspace"]["obligation_graph"]["obligations"]
        self.assertEqual(obligation[0]["status"], ObligationStatus.BLOCKED.value)

        summary = result.metadata["result_summary"]
        self.assertEqual(len(summary["blocked_obligations"]), 1)
        self.assertEqual(
            summary["blocked_obligations"][0]["obligation_id"], "sample"
        )
        self.assertEqual(summary["blocked_branch_obligation_ids"], [])

    def test_blocked_helper_obligation_yields_blocked_stop_reason(self) -> None:
        # Phase 7.5: when a decomposed helper is blocked (capability missing),
        # the parent can never become ready, the frontier drains, and the run
        # terminates with stop_reason "blocked" — distinguishing a mechanical
        # dead-end from a run that merely exhausted ready work.
        from agent.proof_system.workspace.action import (
            DEFAULT_ALLOWED_MUTATIONS,
            SearchAction,
            SearchActionKind,
        )
        from agent.search.structured.proposal import (
            CapabilityTestPayload,
            DecomposeChildSpec,
            DecomposePayload,
            StructuredActionProposal,
        )

        class HelperBlockingGenerator:
            _is_structured_generator = True

            def generate(self, request: ActionGenerationRequest):
                branch_id = request.metadata.get("branch_id", "")
                if branch_id == "sample:root":
                    return (
                        StructuredActionProposal(
                            action=SearchAction(
                                kind=SearchActionKind.DECOMPOSE,
                                target_branch_id=branch_id,
                                allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                                    SearchActionKind.DECOMPOSE
                                ],
                                rationale="split into helper",
                            ),
                            payload=DecomposePayload(
                                children=(
                                    DecomposeChildSpec(
                                        child_id="helper",
                                        statement="lemma helper : True := by\n  {{proof}}\n",
                                    ),
                                )
                            ),
                        ),
                    )
                # Helper branch: probe a capability the environment lacks.
                return (
                    StructuredActionProposal(
                        action=SearchAction(
                            kind=SearchActionKind.RUN_CAPABILITY_TEST,
                            target_branch_id=branch_id,
                            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                                SearchActionKind.RUN_CAPABILITY_TEST
                            ],
                            rationale="probe tactic#simp",
                        ),
                        payload=CapabilityTestPayload(
                            requirement="tactic#simp",
                            signature="by simp",
                        ),
                    ),
                )

        class MissingCapabilityAdapter(StructuredFakeAdapter):
            def check(self, candidate_file, budget_slice):
                self.checked_files.append(candidate_file)
                return CheckResult(
                    accepted=False,
                    category=DiagnosticCategory.UNKNOWN_IDENTIFIER,
                    raw_output="unknown identifier 'simp'",
                    candidate_file=candidate_file,
                    parsed_feedback=ParsedFeedback(
                        category=DiagnosticCategory.UNKNOWN_IDENTIFIER,
                        message="unknown identifier 'simp'",
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            result = self._controller(
                tmp,
                HelperBlockingGenerator(),  # type: ignore[arg-type]
                adapter=MissingCapabilityAdapter(),
                budget=BudgetConfig(max_checks=6, max_model_calls=6),
            ).run(_task())

        self.assertFalse(result.accepted)
        self.assertEqual(result.stop_reason, "blocked")
        summary = result.metadata["result_summary"]
        self.assertTrue(any(o["obligation_id"] == "helper" for o in summary["blocked_obligations"]))
        # RUN_CAPABILITY_TEST is now executed, not skipped.
        self.assertEqual(result.metadata["skipped_proposals"], ())

    def test_capability_audit_available_keeps_route_active(self) -> None:
        # When the probe is accepted the route stays ACTIVE; the generator then
        # runs out of candidates and the branch blocks via no_actions (the
        # capability audit itself never blocks on an available capability).
        from agent.proof_system.workspace.action import (
            DEFAULT_ALLOWED_MUTATIONS,
            SearchAction,
            SearchActionKind,
        )
        from agent.search.structured.proposal import (
            CapabilityTestPayload,
            StructuredActionProposal,
        )

        class CapabilityThenEmptyGenerator:
            _is_structured_generator = True

            def __init__(self) -> None:
                self.calls = 0

            def generate(self, request: ActionGenerationRequest):
                self.calls += 1
                if self.calls == 1:
                    return (
                        StructuredActionProposal(
                            action=SearchAction(
                                kind=SearchActionKind.RUN_CAPABILITY_TEST,
                                target_branch_id="sample:root",
                                allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                                    SearchActionKind.RUN_CAPABILITY_TEST
                                ],
                                rationale="probe",
                            ),
                            payload=CapabilityTestPayload(
                                requirement="trivial",
                                signature="by trivial",
                            ),
                        ),
                    )
                return ()

        with tempfile.TemporaryDirectory() as tmp:
            result = self._controller(
                tmp,
                CapabilityThenEmptyGenerator(),  # type: ignore[arg-type]
            ).run(_task())

        self.assertFalse(result.accepted)
        # First attempt is the accepted capability probe; the route stayed
        # ACTIVE afterwards, so a second iteration ran and produced no_actions.
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].edit.action, "capability_test")
        # An accepted capability does NOT register a verified fact.
        self.assertEqual(
            len(result.metadata["workspace"]["accepted_facts"]), 0
        )
        observations = result.metadata["workspace"]["branches"][0]["observations"]
        self.assertTrue(any(o["source"] == "capability_audit" for o in observations))

    def test_capability_audit_does_not_consume_implement_candidate_limit(self) -> None:
        # Default max_candidates_per_model_call is 1 for proof candidates, but
        # capability audits are probes, not proof candidates. A batch containing
        # (RUN_CAPABILITY_TEST, IMPLEMENT) must execute both: first the audit,
        # then the single allowed implementation candidate.
        from agent.proof_system.workspace.action import (
            DEFAULT_ALLOWED_MUTATIONS,
            SearchAction,
            SearchActionKind,
        )
        from agent.search.structured.proposal import (
            CapabilityTestPayload,
            ImplementPayload,
            StructuredActionProposal,
        )

        class CapabilityThenImplementGenerator:
            _is_structured_generator = True

            def generate(self, request: ActionGenerationRequest):
                return (
                    StructuredActionProposal(
                        action=SearchAction(
                            kind=SearchActionKind.RUN_CAPABILITY_TEST,
                            target_branch_id="sample:root",
                            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                                SearchActionKind.RUN_CAPABILITY_TEST
                            ],
                            rationale="probe trivial",
                        ),
                        payload=CapabilityTestPayload(
                            requirement="trivial",
                            signature="by trivial",
                        ),
                    ),
                    StructuredActionProposal(
                        action=SearchAction(
                            kind=SearchActionKind.IMPLEMENT,
                            target_branch_id="sample:root",
                            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                                SearchActionKind.IMPLEMENT
                            ],
                            rationale="prove after probe",
                        ),
                        payload=ImplementPayload(proof_text="trivial"),
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            result = self._controller(
                tmp,
                CapabilityThenImplementGenerator(),  # type: ignore[arg-type]
                budget=BudgetConfig(max_checks=3, max_model_calls=1),
            ).run(_task())

        self.assertTrue(result.accepted)
        self.assertEqual([attempt.edit.action for attempt in result.attempts], [
            "capability_test",
            "model_complete",
        ])
        self.assertEqual(result.metrics.budget_checks_used, 3)


if __name__ == "__main__":
    unittest.main()
