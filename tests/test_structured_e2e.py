"""Phase 7.0 end-to-end contract tests for the structured executor.

These tests freeze the *result contract* of a structured run: the shape of
``metadata["result_summary"]`` (and the assembly-error pass-through) across the
terminal paths. They are independent of the real Lean toolchain — a fake
adapter drives the controller the same way ``test_structured_controller.py``
does, but here the assertions target the machine-assertable summary view, not
mechanics like budget counts or repair-fork branch ids.

Three scenarios, matching the Phase 7.0 contract fixtures:

* single-root acceptance (Phase 6 behavior does not regress, contract is full);
* two-helper-plus-root decomposition at the *data-structure* layer (the
  multi-obligation *loop* is Phase 7.4 — the frontier has no AND-readiness yet);
* capability-missing → blocked semantics (freezes the current "branch BLOCKED
  but obligation still OPEN" gap until Phase 7.3 capability-audit fixes it).

Plus one regression guard: assembly failure must surface its ``errors``
(previously dropped on ``assembly_failed``).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent.proof_system.assembler import ArtifactAssembler
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
from agent.proof_system.workspace import (
    ObligationStatus,
    ProofObligation,
    ProofWorkspace,
    initialize_from_task,
    workspace_from_dict,
)
from agent.runtime.workspace import AttemptWorkspace
from agent.search.action import ActionCandidate, ActionGenerationRequest
from agent.search.budget import BudgetConfig
from agent.search.controller import ControllerConfig
from agent.search.execution import ExecutionMode
from agent.search.safety import SafetyVerdict
from agent.search.structured import StructuredController
from agent.search.structured.summary import build_result_summary


def _task() -> ProofTask:
    return ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")


class _FakeAdapter(ProofSystemAdapter):
    """Verdict depends on the candidate's proof text.

    ``trivial`` → accepted; ``stuck`` → unsolved with a fixed fingerprint; any
    other text → plain unsolved.
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


class _QueueGenerator:
    """Pop one batch of proof texts per ``generate`` call."""

    def __init__(self, batches: list[list[str]]) -> None:
        self.batches = list(batches)

    def generate(self, request: ActionGenerationRequest):
        if not self.batches:
            return []
        return [
            ActionCandidate(proof_text=text, action="queued")
            for text in self.batches.pop(0)
        ]


class StructuredEndToEndContractTests(unittest.TestCase):
    def _controller(
        self,
        tmp: str,
        generator: _QueueGenerator,
        *,
        adapter: ProofSystemAdapter | None = None,
        max_candidates: int = 1,
    ) -> StructuredController:
        return StructuredController(
            adapter=adapter or _FakeAdapter(),
            action_generator=generator,
            workspace=AttemptWorkspace(tmp),
            budget_config=BudgetConfig(max_checks=8, max_model_calls=8),
            config=ControllerConfig(
                execution_mode=ExecutionMode.STRUCTURED,
                max_candidates_per_model_call=max_candidates,
            ),
        )

    # --- scenario 1: single-root acceptance, full contract -------------------

    def test_single_root_acceptance_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._controller(tmp, _QueueGenerator([["trivial"]])).run(_task())

        self.assertTrue(result.accepted)
        self.assertEqual(result.stop_reason, "accepted")

        summary = result.metadata["result_summary"]
        # Workspace layer.
        self.assertEqual(summary["workspace_status"], "accepted")
        self.assertTrue(summary["validation_ok"])
        self.assertEqual(summary["validation_errors"], [])

        # Obligation grouping: exactly one accepted root, none open/blocked.
        self.assertEqual(len(summary["accepted_obligations"]), 1)
        self.assertEqual(summary["accepted_obligations"][0]["obligation_id"], "sample")
        self.assertEqual(summary["accepted_obligations"][0]["has_accepted_branch"], True)
        self.assertEqual(summary["open_obligations"], [])
        self.assertEqual(summary["blocked_obligations"], [])

        # Branch grouping: one selected branch with its artifact, no alternatives.
        self.assertEqual(len(summary["selected_branches"]), 1)
        self.assertEqual(summary["selected_branches"][0]["obligation_id"], "sample")
        self.assertEqual(summary["selected_branches"][0]["status"], "accepted")
        self.assertEqual(summary["selected_branches"][0]["has_artifact"], True)
        self.assertEqual(summary["selected_branches"][0]["is_selected"], True)
        self.assertEqual(summary["preserved_alternatives"], [])

        # The branch/obligation-status gap is empty on a clean accept.
        self.assertEqual(summary["blocked_branch_obligation_ids"], [])

        # Assembly executed and succeeded.
        self.assertEqual(summary["assembly"]["executed"], True)
        self.assertEqual(summary["assembly"]["accepted"], True)
        self.assertEqual(summary["assembly"]["errors"], [])

        # The raw assembly dict is also surfaced (proves the pass-through).
        self.assertIn("assembly", result.metadata)
        self.assertEqual(result.metadata["assembly"]["accepted"], True)

    # --- scenario 2: two-helper + root at the data-structure layer ------------

    def test_two_helpers_root_decomposition_data_layer(self) -> None:
        workspace = initialize_from_task(_task())
        helper1 = ProofObligation(
            obligation_id="sample.helper1",
            version=1,
            title="h1",
            lean_statement="lemma helper1 : True := by trivial",
            status=ObligationStatus.OPEN,
        )
        helper2 = ProofObligation(
            obligation_id="sample.helper2",
            version=1,
            title="h2",
            lean_statement="lemma helper2 : True := by trivial",
            status=ObligationStatus.OPEN,
        )
        workspace = workspace.decompose("sample", [helper1, helper2])

        # Graph state: three active obligations, root bumped to v2 depending
        # on both helpers, with the superseded root v1 retained for provenance.
        active = workspace.obligation_graph.active()
        self.assertEqual(len(active), 3)
        root = workspace.obligation_graph.by_id("sample")
        assert root is not None
        self.assertEqual(root.version, 2)
        self.assertIn("sample.helper1", root.dependency_ids)
        self.assertIn("sample.helper2", root.dependency_ids)
        superseded = workspace.obligation_graph.superseded()
        self.assertEqual(len(superseded), 1)
        self.assertEqual(superseded[0].version, 1)

        # The DAG validates.
        report = workspace.validate()
        self.assertTrue(report.ok)
        self.assertEqual(report.errors, ())

        # Serialization round-trips the structure.
        roundtrip = workspace_from_dict(workspace.to_dict())
        self.assertEqual(len(roundtrip.obligation_graph.active()), 3)
        roundtrip_root = roundtrip.obligation_graph.by_id("sample")
        assert roundtrip_root is not None
        self.assertEqual(roundtrip_root.version, 2)
        self.assertEqual(roundtrip_root.dependency_ids, root.dependency_ids)
        self.assertTrue(roundtrip.validate().ok)

        # Assembler precondition: with nothing accepted, assembly short-
        # circuits to a blocked result naming each un-accepted obligation.
        assembly = ArtifactAssembler().assemble(
            roundtrip,
            artifacts={},
            adapter=_FakeAdapter(),
            task=_task(),
        )
        self.assertFalse(assembly.accepted)
        self.assertTrue(any("not accepted" in error for error in assembly.errors))

        # The derived summary reports all three obligations as open.
        summary = build_result_summary(roundtrip).to_dict()
        self.assertEqual(len(summary["open_obligations"]), 3)
        self.assertEqual(summary["accepted_obligations"], [])
        self.assertEqual(summary["blocked_obligations"], [])
        self.assertEqual(summary["assembly"]["executed"], False)
        self.assertEqual(summary["selected_branches"], [])

    # --- scenario 3: capability-missing → blocked semantics ------------------

    def test_capability_missing_blocked_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._controller(tmp, _QueueGenerator([])).run(_task())

        self.assertFalse(result.accepted)
        self.assertEqual(result.stop_reason, "no_actions")

        summary = result.metadata["result_summary"]

        # Branch-layer fact: the root branch is BLOCKED.
        self.assertEqual(summary["workspace_status"], "searching")
        branches = result.metadata["workspace"]["branches"]
        self.assertEqual(branches[0]["status"], "blocked")

        # Obligation-layer fact: still OPEN. Phase 7.0 freezes this gap;
        # Phase 7.3 capability-audit will flip the obligation to BLOCKED too.
        self.assertEqual(len(summary["open_obligations"]), 1)
        self.assertEqual(summary["open_obligations"][0]["obligation_id"], "sample")
        self.assertEqual(summary["open_obligations"][0]["status"], "open")
        self.assertEqual(summary["open_obligations"][0]["has_accepted_branch"], False)
        self.assertEqual(summary["blocked_obligations"], [])

        # The gap is surfaced explicitly.
        self.assertIn("sample", summary["blocked_branch_obligation_ids"])

        # Assembly never ran.
        self.assertEqual(summary["assembly"]["executed"], False)
        self.assertNotIn("assembly", result.metadata)

        # No solution selected; the single blocked branch is preserved.
        self.assertEqual(summary["selected_branches"], [])
        self.assertEqual(len(summary["preserved_alternatives"]), 1)
        self.assertEqual(summary["preserved_alternatives"][0]["status"], "blocked")

    # --- regression: assembly failure must surface its errors ----------------

    def test_assembly_failure_propagates_errors(self) -> None:
        # The candidate and the assembled file render to the same source, so we
        # distinguish them by check call count: the first check is the
        # candidate (accepted), the second is the assembly recheck (rejected).
        class AssemblyRejectAdapter(_FakeAdapter):
            def check(self, candidate_file, budget_slice):
                self.checked_files.append(candidate_file)
                if len(self.checked_files) > 1:
                    return CheckResult(
                        accepted=False,
                        category=DiagnosticCategory.TACTIC_FAILED,
                        raw_output="assembly rejected",
                        candidate_file=candidate_file,
                    )
                return _FakeAdapter.check(self, candidate_file, budget_slice)

        with tempfile.TemporaryDirectory() as tmp:
            result = self._controller(
                tmp, _QueueGenerator([["trivial"]]), adapter=AssemblyRejectAdapter()
            ).run(_task())

        self.assertFalse(result.accepted)
        self.assertEqual(result.stop_reason, "assembly_failed")

        # The assembly errors are no longer dropped.
        assembly = result.metadata["assembly"]
        self.assertFalse(assembly["accepted"])
        self.assertGreaterEqual(len(assembly["errors"]), 1)

        # The summary mirrors the assembly outcome.
        summary = result.metadata["result_summary"]
        self.assertEqual(summary["assembly"]["executed"], True)
        self.assertEqual(summary["assembly"]["accepted"], False)


if __name__ == "__main__":
    unittest.main()
