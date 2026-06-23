"""Unit tests for the structured workspace context projection (Phase 7.1).

These build :class:`ProofWorkspace` instances directly from the frozen
dataclasses (the same style as ``test_workspace`` / ``test_branch``) so every
projection code path is exercised without a controller or Lean checker.
"""

from __future__ import annotations

import unittest

from agent.proof_system.base import DiagnosticCategory
from agent.proof_system.workspace import (
    AlignmentLink,
    AlignmentRelation,
    ArgumentGraph,
    ArgumentStep,
    BranchStatus,
    FailureHypothesis,
    FailureKind,
    LeanArtifact,
    ObligationGraph,
    ObligationStatus,
    Observation,
    ObservationSource,
    ProofBranch,
    ProofObligation,
    ProofWorkspace,
    VerifiedFact,
    WorkspaceStatus,
)
from agent.proof_system.workspace.spec import FormalSpecification
from agent.search.structured.projection import (
    MAX_PROJECTED_OBSERVATIONS,
    MAX_SIBLING_BRANCHES,
    build_context_projection,
    context_projection_from_dict,
)


def _obligation(
    obligation_id: str,
    *,
    version: int = 1,
    statement_nl: str = "",
    lean_statement: str = "",
    dependency_ids: tuple[str, ...] = (),
    status: ObligationStatus = ObligationStatus.OPEN,
) -> ProofObligation:
    return ProofObligation(
        obligation_id=obligation_id,
        version=version,
        statement_nl=statement_nl,
        lean_statement=lean_statement,
        dependency_ids=dependency_ids,
        status=status,
    )


def _workspace(
    *,
    obligations: tuple[ProofObligation, ...],
    root_obligation_id: str,
    branches: tuple[ProofBranch, ...] = (),
    accepted_facts: tuple[VerifiedFact, ...] = (),
) -> ProofWorkspace:
    return ProofWorkspace(
        workspace_id="ws",
        version=1,
        specification=FormalSpecification(),
        obligation_graph=ObligationGraph(
            obligations=obligations,
            root_obligation_id=root_obligation_id,
        ),
        accepted_facts=accepted_facts,
        branches=branches,
        root_obligation_ids=(root_obligation_id,),
        status=WorkspaceStatus.SEARCHING,
    )


def _branch(
    branch_id: str,
    obligation_id: str,
    *,
    obligation_version: int = 1,
    argument: ArgumentGraph | None = None,
    lean_artifact: LeanArtifact | None = None,
    alignment: tuple[AlignmentLink, ...] = (),
    observations: tuple[Observation, ...] = (),
    failure_hypotheses: tuple[FailureHypothesis, ...] = (),
    status: BranchStatus = BranchStatus.ACTIVE,
) -> ProofBranch:
    return ProofBranch(
        branch_id=branch_id,
        obligation_id=obligation_id,
        obligation_version=obligation_version,
        argument=argument if argument is not None else ArgumentGraph(),
        lean_artifact=lean_artifact,
        alignment=alignment,
        observations=observations,
        failure_hypotheses=failure_hypotheses,
        status=status,
    )


def _observation(
    observation_id: str,
    *,
    message: str = "unsolved",
    goal_fingerprint: str | None = "fp",
    source: ObservationSource = ObservationSource.CHECKER,
    category: str = DiagnosticCategory.UNSOLVED_GOALS.value,
) -> Observation:
    return Observation(
        observation_id=observation_id,
        source=source,
        category=category,
        message=message,
        goal_fingerprint=goal_fingerprint,
        raw_evidence_ref=observation_id,
    )


class SingleRootProjectionTest(unittest.TestCase):
    def test_single_root_no_dependencies(self) -> None:
        root = _obligation("root", statement_nl="prove root", lean_statement="t : T")
        workspace = _workspace(
            obligations=(root,),
            root_obligation_id="root",
            branches=(_branch("root:0", "root"),),
        )

        projection = build_context_projection(workspace, "root:0")

        self.assertEqual(projection.branch_id, "root:0")
        self.assertIsNotNone(projection.root)
        self.assertIsNotNone(projection.current_obligation)
        self.assertEqual(projection.root, projection.current_obligation)
        self.assertTrue(projection.current_obligation.is_root)
        self.assertEqual(projection.current_obligation.obligation_id, "root")
        self.assertEqual(projection.current_obligation.version, 1)
        self.assertEqual(projection.dependency_facts, ())
        self.assertEqual(projection.argument_steps, ())
        self.assertEqual(projection.sibling_branches, ())
        self.assertEqual(projection.observations, ())


class DependencyClosureTest(unittest.TestCase):
    def test_closure_walks_transitively_and_matches_facts(self) -> None:
        root = _obligation("root", dependency_ids=("helper1",))
        helper1 = _obligation("helper1", dependency_ids=("helper2",))
        helper2 = _obligation("helper2")
        # Only helper1 has an accepted fact (version-matched).
        fact = VerifiedFact(
            obligation_id="helper1",
            obligation_version=1,
            statement="lemma helper1 : True := rfl",
            source_attempt_index=0,
            checker_category=DiagnosticCategory.PROOF_ACCEPTED.value,
            safety_accepted=True,
        )
        workspace = _workspace(
            obligations=(root, helper1, helper2),
            root_obligation_id="root",
            branches=(_branch("root:0", "root"),),
            accepted_facts=(fact,),
        )

        projection = build_context_projection(workspace, "root:0")

        by_id = {dep.obligation_id: dep for dep in projection.dependency_facts}
        self.assertEqual(set(by_id), {"helper1", "helper2"})
        self.assertTrue(by_id["helper1"].has_accepted_fact)
        self.assertFalse(by_id["helper2"].has_accepted_fact)
        self.assertEqual(
            by_id["helper1"].statement, "lemma helper1 : True := rfl"
        )
        # accepted_facts slots mirror workspace.accepted_facts verbatim.
        self.assertEqual(len(projection.accepted_facts), 1)
        self.assertEqual(projection.accepted_facts[0].obligation_id, "helper1")

    def test_stale_fact_version_is_rejected(self) -> None:
        root = _obligation("root", dependency_ids=("helper",))
        # Graph now at version 2 (e.g. after a decomposition new_version).
        helper = _obligation("helper", version=2)
        # Fact was registered against version 1 — stale.
        stale_fact = VerifiedFact(
            obligation_id="helper",
            obligation_version=1,
            statement="old",
            source_attempt_index=0,
            checker_category=DiagnosticCategory.PROOF_ACCEPTED.value,
            safety_accepted=True,
        )
        workspace = _workspace(
            obligations=(root, helper),
            root_obligation_id="root",
            branches=(_branch("root:0", "root"),),
            accepted_facts=(stale_fact,),
        )

        projection = build_context_projection(workspace, "root:0")

        self.assertEqual(len(projection.dependency_facts), 1)
        self.assertFalse(projection.dependency_facts[0].has_accepted_fact)
        self.assertEqual(projection.dependency_facts[0].obligation_version, 2)


class ArgumentAlignmentTest(unittest.TestCase):
    def test_steps_carry_alignment_relation_and_declaration(self) -> None:
        branch = _branch(
            "root:0",
            "root",
            argument=ArgumentGraph(
                steps=(
                    ArgumentStep(step_id="s1", claim="apply lemma A"),
                    ArgumentStep(step_id="s2", claim="close by omega"),
                )
            ),
            alignment=(
                AlignmentLink(
                    argument_step_id="s1",
                    lean_declaration_id="helper1",
                    relation=AlignmentRelation.IMPLEMENTS,
                ),
                AlignmentLink(
                    argument_step_id="s2", relation=AlignmentRelation.UNALIGNED
                ),
            ),
        )
        workspace = _workspace(
            obligations=(_obligation("root"),),
            root_obligation_id="root",
            branches=(branch,),
        )

        projection = build_context_projection(workspace, "root:0")

        by_step = {step.step_id: step for step in projection.argument_steps}
        self.assertEqual(by_step["s1"].alignment_relation, "implements")
        self.assertEqual(by_step["s1"].aligned_declaration, "helper1")
        self.assertEqual(by_step["s2"].alignment_relation, "unaligned")
        self.assertIsNone(by_step["s2"].aligned_declaration)


class ObservationDedupTest(unittest.TestCase):
    def test_duplicates_collapse_and_tail_is_capped(self) -> None:
        observations = (
            _observation("o1", message="m1", goal_fingerprint="fp1"),
            _observation("o2", message="m1", goal_fingerprint="fp1"),  # dup of o1
            _observation("o3", message="m2", goal_fingerprint="fp2"),
            _observation("o4", message="m3", goal_fingerprint="fp3"),
        )
        branch = _branch("root:0", "root", observations=observations)
        workspace = _workspace(
            obligations=(_obligation("root"),),
            root_obligation_id="root",
            branches=(branch,),
        )

        projection = build_context_projection(workspace, "root:0")

        ids = [obs.observation_id for obs in projection.observations]
        # o2 collapses into o1's key; first occurrence kept.
        self.assertEqual(ids, ["o1", "o3", "o4"])

    def test_long_observation_list_is_truncated_to_tail(self) -> None:
        observations = tuple(
            _observation(f"o{i}", message=f"m{i}", goal_fingerprint=f"fp{i}")
            for i in range(MAX_PROJECTED_OBSERVATIONS + 5)
        )
        branch = _branch("root:0", "root", observations=observations)
        workspace = _workspace(
            obligations=(_obligation("root"),),
            root_obligation_id="root",
            branches=(branch,),
        )

        projection = build_context_projection(workspace, "root:0")

        self.assertEqual(len(projection.observations), MAX_PROJECTED_OBSERVATIONS)
        # Tail retained: the oldest entries are dropped.
        kept = {obs.observation_id for obs in projection.observations}
        self.assertNotIn("o0", kept)
        self.assertIn(f"o{MAX_PROJECTED_OBSERVATIONS + 4}", kept)


class SiblingBranchTest(unittest.TestCase):
    def test_sibling_strategies_on_same_obligation_listed(self) -> None:
        primary = _branch("root:0", "root")
        sibling = _branch(
            "root:1",
            "root",
            lean_artifact=LeanArtifact(
                source="theorem root : True := by trivial",
                obligation_id="root",
                obligation_version=1,
                declaration_id="root",
                proof_body="trivial",
            ),
            observations=(_observation("x"),),
            status=BranchStatus.DORMANT,
        )
        unrelated = _branch("helper:0", "helper")  # different obligation
        workspace = _workspace(
            obligations=(_obligation("root"), _obligation("helper")),
            root_obligation_id="root",
            branches=(primary, sibling, unrelated),
        )

        projection = build_context_projection(workspace, "root:0")

        self.assertEqual(len(projection.sibling_branches), 1)
        only = projection.sibling_branches[0]
        self.assertEqual(only.branch_id, "root:1")
        self.assertEqual(only.status, "dormant")
        self.assertTrue(only.has_artifact)
        self.assertEqual(only.observation_count, 1)

    def test_sibling_list_is_capped(self) -> None:
        branches = tuple(_branch(f"root:{i}", "root") for i in range(20))
        branches = (_branch("root:primary", "root"),) + branches
        workspace = _workspace(
            obligations=(_obligation("root"),),
            root_obligation_id="root",
            branches=branches,
        )

        projection = build_context_projection(workspace, "root:primary")

        self.assertEqual(len(projection.sibling_branches), MAX_SIBLING_BRANCHES)


class FailureHypothesisTest(unittest.TestCase):
    def test_hypotheses_project_to_slots(self) -> None:
        branch = _branch(
            "root:0",
            "root",
            failure_hypotheses=(
                FailureHypothesis(
                    hypothesis_id="h1",
                    kind=FailureKind.THEOREM_MISUSE,
                    confidence=0.7,
                    evidence_ids=("o1",),
                    affected_step_ids=("s1",),
                ),
                FailureHypothesis(
                    hypothesis_id="h2",
                    kind=FailureKind.ARGUMENT_GAP,
                    confidence=0.4,
                    evidence_ids=("o2",),
                ),
            ),
        )
        workspace = _workspace(
            obligations=(_obligation("root"),),
            root_obligation_id="root",
            branches=(branch,),
        )

        projection = build_context_projection(workspace, "root:0")

        self.assertEqual(len(projection.failure_hypotheses), 2)
        first = projection.failure_hypotheses[0]
        self.assertEqual(first.hypothesis_id, "h1")
        self.assertEqual(first.kind, "theorem_misuse")
        self.assertEqual(first.confidence, 0.7)


class MissingBranchTest(unittest.TestCase):
    def test_unknown_branch_id_yields_empty_projection_without_raising(self) -> None:
        workspace = _workspace(
            obligations=(_obligation("root", statement_nl="prove root"),),
            root_obligation_id="root",
            branches=(_branch("root:0", "root"),),
        )

        projection = build_context_projection(workspace, "nope")

        self.assertIsNone(projection.branch_id)
        # Best-effort: root is still surfaced.
        self.assertIsNotNone(projection.root)
        self.assertEqual(projection.root.obligation_id, "root")
        # All per-branch sections empty.
        self.assertIsNone(projection.current_obligation)
        self.assertEqual(projection.dependency_facts, ())
        self.assertEqual(projection.argument_steps, ())
        self.assertEqual(projection.observations, ())
        self.assertEqual(projection.sibling_branches, ())


class RoundTripTest(unittest.TestCase):
    def test_to_dict_from_dict_is_value_equal(self) -> None:
        branch = _branch(
            "root:0",
            "root",
            argument=ArgumentGraph(
                steps=(ArgumentStep(step_id="s1", claim="claim"),)
            ),
            alignment=(
                AlignmentLink(
                    argument_step_id="s1",
                    lean_declaration_id="root",
                    relation=AlignmentRelation.PARTIAL,
                ),
            ),
            lean_artifact=LeanArtifact(
                source="theorem root : True := by trivial",
                obligation_id="root",
                obligation_version=1,
                proof_body="trivial",
            ),
            observations=(
                _observation("o1", message="unsolved", goal_fingerprint="fp1"),
            ),
            failure_hypotheses=(
                FailureHypothesis(
                    hypothesis_id="h1",
                    kind=FailureKind.IMPLEMENTATION_DEFECT,
                    confidence=0.5,
                    evidence_ids=("o1",),
                    affected_step_ids=("s1",),
                ),
            ),
        )
        root = _obligation("root", dependency_ids=("helper",))
        helper = _obligation("helper", lean_statement="lemma helper : True")
        fact = VerifiedFact(
            obligation_id="helper",
            obligation_version=1,
            statement="lemma helper : True",
            source_attempt_index=0,
            checker_category=DiagnosticCategory.PROOF_ACCEPTED.value,
            safety_accepted=True,
        )
        workspace = _workspace(
            obligations=(root, helper),
            root_obligation_id="root",
            branches=(branch,),
            accepted_facts=(fact,),
        )

        projection = build_context_projection(workspace, "root:0")
        restored = context_projection_from_dict(projection.to_dict())

        self.assertEqual(restored, projection)


if __name__ == "__main__":
    unittest.main()
