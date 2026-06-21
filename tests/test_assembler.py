from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent.proof_system.assembler import (
    ArtifactAssembler,
    AssemblyResult,
    LeanArtifact,
)
from agent.proof_system.base import (
    BudgetSlice,
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
    ProgressSignal,
    ProofSystemAdapter,
    ProofTask,
)
from agent.proof_system.workspace import (
    ObligationGraph,
    ObligationStatus,
    ProofObligation,
    ProofWorkspace,
    WorkspaceStatus,
    initialize_from_task,
)


class _AcceptIfContainsAdapter(ProofSystemAdapter):
    """Accepts the rendered source iff it contains ``accepted_token``."""

    def __init__(self, accepted_token: str = "trivial") -> None:
        self.accepted_token = accepted_token
        self.checked_sources: list[str] = []

    def render_candidate(self, task: ProofTask, candidate_edit: CandidateEdit) -> str:
        return task.source_template.replace(task.hole_marker, candidate_edit.text)

    def check(self, candidate_file: Path, budget_slice: BudgetSlice) -> CheckResult:
        source = candidate_file.read_text(encoding="utf-8")
        self.checked_sources.append(source)
        if self.accepted_token in source:
            return CheckResult(
                accepted=True,
                category=DiagnosticCategory.PROOF_ACCEPTED,
                raw_output="",
                candidate_file=candidate_file,
                parsed_feedback=ParsedFeedback(
                    category=DiagnosticCategory.PROOF_ACCEPTED, message="accepted"
                ),
            )
        return CheckResult(
            accepted=False,
            category=DiagnosticCategory.UNSOLVED_GOALS,
            raw_output="unsolved goals",
            candidate_file=candidate_file,
            parsed_feedback=ParsedFeedback(
                category=DiagnosticCategory.UNSOLVED_GOALS, message="unsolved"
            ),
        )

    def parse_feedback(self, raw_output: str) -> ParsedFeedback:
        return ParsedFeedback(category=DiagnosticCategory.UNKNOWN, raw_output=raw_output)

    def extract_progress(self, parent_state: Any, check_result: CheckResult) -> ProgressSignal:
        return ProgressSignal(diagnostic_category=check_result.category)


def _accepted_root_workspace(task: ProofTask) -> ProofWorkspace:
    workspace = initialize_from_task(task)
    graph = workspace.obligation_graph
    accepted_root = ProofObligation(
        obligation_id=task.task_id,
        version=1,
        lean_statement=task.source_template,
        status=ObligationStatus.ACCEPTED,
    )
    accepted_graph = ObligationGraph(
        obligations=(accepted_root,),
        root_obligation_id=task.task_id,
    )
    from dataclasses import replace

    return replace(workspace, obligation_graph=accepted_graph, status=WorkspaceStatus.ASSEMBLING)


class ArtifactAssemblerTests(unittest.TestCase):
    def test_assembles_and_accepts_when_all_obligations_verified(self) -> None:
        task = ProofTask("demo", "theorem demo : True := by\n  {{proof}}\n")
        workspace = _accepted_root_workspace(task)
        artifacts = {task.task_id: LeanArtifact(source="trivial", obligation_id=task.task_id, obligation_version=1)}
        adapter = _AcceptIfContainsAdapter()

        with tempfile.TemporaryDirectory() as tmp:
            result = ArtifactAssembler().assemble(
                workspace,
                artifacts,
                adapter=adapter,
                task=task,
            )

        self.assertTrue(result.accepted)
        self.assertIsNotNone(result.check_result)
        self.assertIn("trivial", adapter.checked_sources[0])

    def test_blocks_when_obligation_not_accepted(self) -> None:
        task = ProofTask("demo", "theorem demo : True := by\n  {{proof}}\n")
        workspace = initialize_from_task(task)  # root still OPEN
        artifacts = {task.task_id: LeanArtifact(source="trivial", obligation_id=task.task_id, obligation_version=1)}
        adapter = _AcceptIfContainsAdapter()

        result = ArtifactAssembler().assemble(
            workspace,
            artifacts,
            adapter=adapter,
            task=task,
        )

        self.assertFalse(result.accepted)
        self.assertTrue(any("not accepted" in error for error in result.errors))
        self.assertEqual(adapter.checked_sources, [])

    def test_blocks_when_artifact_missing(self) -> None:
        task = ProofTask("demo", "theorem demo : True := by\n  {{proof}}\n")
        workspace = _accepted_root_workspace(task)
        adapter = _AcceptIfContainsAdapter()

        result = ArtifactAssembler().assemble(
            workspace,
            {},
            adapter=adapter,
            task=task,
        )

        self.assertFalse(result.accepted)
        self.assertTrue(any("no artifact" in error for error in result.errors))

    def test_blocks_when_artifact_version_mismatched(self) -> None:
        task = ProofTask("demo", "theorem demo : True := by\n  {{proof}}\n")
        workspace = _accepted_root_workspace(task)
        # Obligation is version 1; artifact pins version 2.
        artifacts = {
            task.task_id: LeanArtifact(
                source="trivial", obligation_id=task.task_id, obligation_version=2
            )
        }
        adapter = _AcceptIfContainsAdapter()

        result = ArtifactAssembler().assemble(
            workspace,
            artifacts,
            adapter=adapter,
            task=task,
        )

        self.assertFalse(result.accepted)
        self.assertTrue(any("pins version" in error for error in result.errors))

    def test_blocks_when_recheck_rejects(self) -> None:
        task = ProofTask("demo", "theorem demo : True := by\n  {{proof}}\n")
        workspace = _accepted_root_workspace(task)
        # Artifact source lacks the accepted token, so recheck fails.
        artifacts = {
            task.task_id: LeanArtifact(
                source="sorry", obligation_id=task.task_id, obligation_version=1
            )
        }
        adapter = _AcceptIfContainsAdapter(accepted_token="trivial")

        result = ArtifactAssembler().assemble(
            workspace,
            artifacts,
            adapter=adapter,
            task=task,
        )

        self.assertFalse(result.accepted)
        self.assertTrue(any("recheck rejected" in error for error in result.errors))


if __name__ == "__main__":
    unittest.main()
