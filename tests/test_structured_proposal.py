from __future__ import annotations

import unittest

from agent.proof_system.workspace.action import (
    DEFAULT_ALLOWED_MUTATIONS,
    MutationKind,
    SearchAction,
    SearchActionKind,
)
from agent.search.action import (
    ActionCandidate,
    ActionGenerationRequest,
    StaticActionGenerator,
)
from agent.search.structured.proposal import (
    LEGACY_ACTION_KEY,
    LEGACY_KIND_DEFERRED,
    PAYLOAD_KIND_CAPABILITY_TEST,
    PAYLOAD_KIND_CHANGE_REPRESENTATION,
    PAYLOAD_KIND_DECOMPOSE,
    PAYLOAD_KIND_IMPLEMENT,
    PAYLOAD_KIND_PROPOSE_ARGUMENT,
    PAYLOAD_KIND_REFINE_ARGUMENT,
    SUPPORTED_PROPOSAL_KINDS,
    AlignmentSpec,
    ArgumentStepSpec,
    CapabilityTestPayload,
    ChangeRepresentationPayload,
    DecomposeChildSpec,
    DecomposePayload,
    ImplementPayload,
    ProposeArgumentPayload,
    RefineArgumentPayload,
    StructuredActionProposal,
    _LegacyActionGeneratorAdapter,
    adapt_legacy_generator,
    alignment_spec_from_dict,
    argument_step_spec_from_dict,
    capability_test_payload_from_dict,
    change_representation_payload_from_dict,
    decompose_child_spec_from_dict,
    decompose_payload_from_dict,
    propose_argument_payload_from_dict,
    refine_argument_payload_from_dict,
    structured_action_proposal_from_dict,
)


def _implement_action(*, kind: SearchActionKind = SearchActionKind.IMPLEMENT) -> SearchAction:
    return SearchAction(
        kind=kind,
        target_branch_id="b1",
        allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[kind],
        rationale="realize obligation",
    )


class PayloadSerializationTest(unittest.TestCase):
    def test_implement_payload_round_trip(self) -> None:
        payload = ImplementPayload(proof_text="trivial", source="trivial")
        restored = ImplementPayload(
            proof_text=payload.to_dict()["proof_text"],
            source=payload.to_dict().get("source", ""),
        )
        self.assertEqual(restored, payload)
        self.assertEqual(payload.to_dict()["kind"], PAYLOAD_KIND_IMPLEMENT)

    def test_decompose_child_spec_round_trip(self) -> None:
        spec = DecomposeChildSpec(
            child_id="c1", statement="forall n, p n", dependency_ids=("c0",)
        )
        restored = decompose_child_spec_from_dict(spec.to_dict())
        self.assertEqual(restored, spec)

    def test_decompose_payload_round_trip(self) -> None:
        payload = DecomposePayload(
            children=(
                DecomposeChildSpec(child_id="c1", statement="helper"),
                DecomposeChildSpec(
                    child_id="c2", statement="main", dependency_ids=("c1",)
                ),
            ),
            strategy="split the induction step out",
        )
        restored = decompose_payload_from_dict(payload.to_dict())
        self.assertEqual(restored, payload)
        self.assertEqual(payload.to_dict()["kind"], PAYLOAD_KIND_DECOMPOSE)

    def test_capability_test_payload_round_trip(self) -> None:
        payload = CapabilityTestPayload(
            requirement="omega",
            signature="#check @Nat.le",
            expected_outcome="accepts",
        )
        restored = capability_test_payload_from_dict(payload.to_dict())
        self.assertEqual(restored, payload)
        self.assertEqual(payload.to_dict()["kind"], PAYLOAD_KIND_CAPABILITY_TEST)

    def test_argument_step_spec_round_trip(self) -> None:
        spec = ArgumentStepSpec(
            step_id="s1",
            claim="inductive step",
            justification="by IH",
            depends_on=("s0",),
            introduced_fact_ids=("f1",),
            confidence=0.8,
        )
        restored = argument_step_spec_from_dict(spec.to_dict())
        self.assertEqual(restored, spec)

    def test_alignment_spec_round_trip(self) -> None:
        spec = AlignmentSpec(
            argument_step_id="s1",
            relation="implements",
            lean_declaration_id="foo",
            goal_fingerprint="fp-a",
        )
        restored = alignment_spec_from_dict(spec.to_dict())
        self.assertEqual(restored, spec)

    def test_propose_argument_payload_round_trip(self) -> None:
        payload = ProposeArgumentPayload(
            steps=(
                ArgumentStepSpec(step_id="s1", claim="claim 1"),
                ArgumentStepSpec(step_id="s2", claim="claim 2", depends_on=("s1",)),
            ),
            alignments=(
                AlignmentSpec(argument_step_id="s1", relation="unaligned"),
                AlignmentSpec(
                    argument_step_id="s2",
                    relation="implements",
                    lean_declaration_id="bar",
                ),
            ),
            rationale="lay out the induction",
        )
        restored = propose_argument_payload_from_dict(payload.to_dict())
        self.assertEqual(restored, payload)
        self.assertEqual(payload.to_dict()["kind"], PAYLOAD_KIND_PROPOSE_ARGUMENT)

    def test_refine_argument_payload_round_trip(self) -> None:
        payload = RefineArgumentPayload(
            steps=(ArgumentStepSpec(step_id="s1", claim="revised claim"),),
            alignments=(
                AlignmentSpec(argument_step_id="s1", relation="implements", goal_fingerprint="fp"),
            ),
            rationale="tighten the claim",
        )
        restored = refine_argument_payload_from_dict(payload.to_dict())
        self.assertEqual(restored, payload)
        self.assertEqual(payload.to_dict()["kind"], PAYLOAD_KIND_REFINE_ARGUMENT)

    def test_change_representation_payload_round_trip(self) -> None:
        payload = ChangeRepresentationPayload(
            argument=(ArgumentStepSpec(step_id="r1", claim="by cases"),),
            alignments=(
                AlignmentSpec(argument_step_id="r1", relation="unaligned"),
            ),
            rationale="switch to case analysis",
        )
        restored = change_representation_payload_from_dict(payload.to_dict())
        self.assertEqual(restored, payload)
        self.assertEqual(
            payload.to_dict()["kind"], PAYLOAD_KIND_CHANGE_REPRESENTATION
        )


class ProposalSerializationTest(unittest.TestCase):
    def test_implement_proposal_round_trip(self) -> None:
        proposal = StructuredActionProposal(
            action=_implement_action(),
            payload=ImplementPayload(proof_text="trivial"),
            score=0.5,
            metadata={"choice_index": 0},
        )
        restored = structured_action_proposal_from_dict(proposal.to_dict())
        self.assertEqual(restored, proposal)

    def test_decompose_proposal_round_trip(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.DECOMPOSE,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[SearchActionKind.DECOMPOSE],
            rationale="split into helper + main",
        )
        proposal = StructuredActionProposal(
            action=action,
            payload=DecomposePayload(
                children=(DecomposeChildSpec(child_id="c1", statement="helper"),)
            ),
        )
        restored = structured_action_proposal_from_dict(proposal.to_dict())
        self.assertEqual(restored, proposal)

    def test_capability_test_proposal_round_trip(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.RUN_CAPABILITY_TEST,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                SearchActionKind.RUN_CAPABILITY_TEST
            ],
            rationale="probe omega availability",
        )
        proposal = StructuredActionProposal(
            action=action,
            payload=CapabilityTestPayload(requirement="omega", signature="omega"),
        )
        restored = structured_action_proposal_from_dict(proposal.to_dict())
        self.assertEqual(restored, proposal)

    def test_propose_argument_proposal_round_trip(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.PROPOSE_ARGUMENT,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                SearchActionKind.PROPOSE_ARGUMENT
            ],
            rationale="add an inductive step",
        )
        proposal = StructuredActionProposal(
            action=action,
            payload=ProposeArgumentPayload(
                steps=(ArgumentStepSpec(step_id="s1", claim="claim"),),
                alignments=(
                    AlignmentSpec(argument_step_id="s1", relation="unaligned"),
                ),
            ),
        )
        restored = structured_action_proposal_from_dict(proposal.to_dict())
        self.assertEqual(restored, proposal)

    def test_change_representation_proposal_round_trip(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.CHANGE_REPRESENTATION,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                SearchActionKind.CHANGE_REPRESENTATION
            ],
            rationale="switch to case analysis",
        )
        proposal = StructuredActionProposal(
            action=action,
            payload=ChangeRepresentationPayload(
                argument=(ArgumentStepSpec(step_id="r1", claim="by cases"),),
                alignments=(
                    AlignmentSpec(argument_step_id="r1", relation="unaligned"),
                ),
            ),
        )
        restored = structured_action_proposal_from_dict(proposal.to_dict())
        self.assertEqual(restored, proposal)


class ProposalValidateTest(unittest.TestCase):
    def test_implement_payload_with_implement_kind_is_valid(self) -> None:
        proposal = StructuredActionProposal(
            action=_implement_action(),
            payload=ImplementPayload(proof_text="trivial"),
        )
        ok, errors = proposal.validate()
        self.assertTrue(ok, msg=errors)

    def test_repair_kind_with_implement_payload_is_valid(self) -> None:
        proposal = StructuredActionProposal(
            action=_implement_action(kind=SearchActionKind.REPAIR_IMPLEMENTATION),
            payload=ImplementPayload(proof_text="trivial"),
        )
        ok, errors = proposal.validate()
        self.assertTrue(ok, msg=errors)

    def test_kind_payload_mismatch_is_invalid(self) -> None:
        # IMPLEMENT action paired with a DecomposePayload must fail.
        proposal = StructuredActionProposal(
            action=_implement_action(),
            payload=DecomposePayload(children=()),
        )
        ok, errors = proposal.validate()
        self.assertFalse(ok)
        self.assertTrue(
            any("ImplementPayload" in err for err in errors), msg=errors
        )

    def test_decompose_kind_payload_agreement(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.DECOMPOSE,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[SearchActionKind.DECOMPOSE],
            rationale="split",
        )
        ok, _ = StructuredActionProposal(
            action=action,
            payload=DecomposePayload(
                children=(DecomposeChildSpec(child_id="c1", statement="x"),)
            ),
        ).validate()
        self.assertTrue(ok)

    def test_capability_kind_payload_agreement(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.RUN_CAPABILITY_TEST,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                SearchActionKind.RUN_CAPABILITY_TEST
            ],
            rationale="probe",
        )
        ok, _ = StructuredActionProposal(
            action=action,
            payload=CapabilityTestPayload(requirement="omega", signature="omega"),
        ).validate()
        self.assertTrue(ok)

    def test_propose_argument_kind_payload_agreement(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.PROPOSE_ARGUMENT,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                SearchActionKind.PROPOSE_ARGUMENT
            ],
            rationale="add a step",
        )
        ok, _ = StructuredActionProposal(
            action=action,
            payload=ProposeArgumentPayload(
                steps=(ArgumentStepSpec(step_id="s1", claim="claim"),),
                alignments=(
                    AlignmentSpec(argument_step_id="s1", relation="unaligned"),
                ),
            ),
        ).validate()
        self.assertTrue(ok)

    def test_refine_argument_kind_payload_agreement(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.REFINE_ARGUMENT,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                SearchActionKind.REFINE_ARGUMENT
            ],
            rationale="refine a step",
        )
        ok, _ = StructuredActionProposal(
            action=action,
            payload=RefineArgumentPayload(
                steps=(ArgumentStepSpec(step_id="s1", claim="claim"),),
                alignments=(
                    AlignmentSpec(argument_step_id="s1", relation="unaligned"),
                ),
            ),
        ).validate()
        self.assertTrue(ok)

    def test_change_representation_kind_payload_agreement(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.CHANGE_REPRESENTATION,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                SearchActionKind.CHANGE_REPRESENTATION
            ],
            rationale="switch",
        )
        ok, _ = StructuredActionProposal(
            action=action,
            payload=ChangeRepresentationPayload(
                argument=(ArgumentStepSpec(step_id="r1", claim="claim"),),
                alignments=(
                    AlignmentSpec(argument_step_id="r1", relation="unaligned"),
                ),
            ),
        ).validate()
        self.assertTrue(ok)

    def test_propose_argument_payload_with_decompose_kind_is_invalid(self) -> None:
        # Cross-kind pairing: a DECOMPOSE action with an argument payload fails.
        action = SearchAction(
            kind=SearchActionKind.DECOMPOSE,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[SearchActionKind.DECOMPOSE],
            rationale="split",
        )
        ok, errors = StructuredActionProposal(
            action=action,
            payload=ProposeArgumentPayload(
                steps=(ArgumentStepSpec(step_id="s1", claim="claim"),),
                alignments=(
                    AlignmentSpec(argument_step_id="s1", relation="unaligned"),
                ),
            ),
        ).validate()
        self.assertFalse(ok)
        self.assertTrue(
            any("DecomposePayload" in err for err in errors), msg=errors
        )

    def test_argument_payload_with_unknown_alignment_relation_is_invalid(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.PROPOSE_ARGUMENT,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                SearchActionKind.PROPOSE_ARGUMENT
            ],
            rationale="add a step",
        )
        ok, errors = StructuredActionProposal(
            action=action,
            payload=ProposeArgumentPayload(
                steps=(ArgumentStepSpec(step_id="s1", claim="claim"),),
                alignments=(
                    AlignmentSpec(
                        argument_step_id="s1",
                        relation="not-a-relation",
                    ),
                ),
            ),
        ).validate()

        self.assertFalse(ok)
        self.assertTrue(
            any("unknown alignment relation" in err for err in errors),
            msg=errors,
        )

    def test_non_unaligned_relation_requires_target(self) -> None:
        action = SearchAction(
            kind=SearchActionKind.CHANGE_REPRESENTATION,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[
                SearchActionKind.CHANGE_REPRESENTATION
            ],
            rationale="switch",
        )
        ok, errors = StructuredActionProposal(
            action=action,
            payload=ChangeRepresentationPayload(
                argument=(ArgumentStepSpec(step_id="s1", claim="claim"),),
                alignments=(
                    AlignmentSpec(argument_step_id="s1", relation="implements"),
                ),
            ),
        ).validate()

        self.assertFalse(ok)
        self.assertTrue(
            any("requires a Lean target" in err for err in errors),
            msg=errors,
        )

    def test_unsupported_kind_is_invalid(self) -> None:
        # FORMALIZE is a real SearchActionKind but not in SUPPORTED_PROPOSAL_KINDS.
        self.assertNotIn(SearchActionKind.FORMALIZE, SUPPORTED_PROPOSAL_KINDS)
        action = SearchAction(
            kind=SearchActionKind.FORMALIZE,
            target_branch_id="b1",
            allowed_mutations=DEFAULT_ALLOWED_MUTATIONS[SearchActionKind.FORMALIZE],
            rationale="formalize",
        )
        ok, errors = StructuredActionProposal(
            action=action,
            payload=ImplementPayload(proof_text="x"),
        ).validate()
        self.assertFalse(ok)
        self.assertTrue(
            any("unsupported proposal kind" in err for err in errors), msg=errors
        )

    def test_broadened_mutation_scope_is_invalid(self) -> None:
        # The action carries a mutation outside its kind default; the proposal
        # delegates to SearchAction.validate, which must reject it.
        action = SearchAction(
            kind=SearchActionKind.REPAIR_IMPLEMENTATION,
            target_branch_id="b1",
            allowed_mutations=(
                MutationKind.LEAN_ARTIFACT,
                MutationKind.ARGUMENT_STEP,
            ),
            rationale="repair",
        )
        ok, errors = StructuredActionProposal(
            action=action,
            payload=ImplementPayload(proof_text="trivial"),
        ).validate()
        self.assertFalse(ok)
        self.assertTrue(
            any("argument_step" in err for err in errors), msg=errors
        )


def _request(*, max_candidates: int = 1) -> ActionGenerationRequest:
    return ActionGenerationRequest(
        task=None,  # type: ignore[arg-type]
        attempt_index=0,
        max_candidates=max_candidates,
        metadata={"branch_id": "b1"},
    )


class LegacyAdapterTest(unittest.TestCase):
    def test_wraps_each_candidate_as_implement_proposal(self) -> None:
        gen = adapt_legacy_generator(StaticActionGenerator(["trivial"]))
        proposals = list(gen.generate(_request()))
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal.action.kind, SearchActionKind.IMPLEMENT)
        self.assertIsInstance(proposal.payload, ImplementPayload)
        assert isinstance(proposal.payload, ImplementPayload)
        self.assertEqual(proposal.payload.proof_text, "trivial")
        self.assertEqual(proposal.payload.source, "trivial")
        self.assertTrue(proposal.metadata[LEGACY_KIND_DEFERRED])
        self.assertEqual(proposal.metadata[LEGACY_ACTION_KEY], "static")

    def test_propagates_score_and_preserves_order(self) -> None:
        gen = adapt_legacy_generator(
            StaticActionGenerator(
                [
                    ActionCandidate(proof_text="a", action="queued", score=0.1),
                    ActionCandidate(proof_text="b", action="queued", score=0.9),
                ]
            )
        )
        proposals = list(gen.generate(_request(max_candidates=4)))
        self.assertEqual([p.payload.proof_text for p in proposals], ["a", "b"])  # type: ignore[union-attr]
        self.assertEqual([p.score for p in proposals], [0.1, 0.9])
        self.assertEqual(
            {p.metadata[LEGACY_ACTION_KEY] for p in proposals}, {"queued"}
        )

    def test_adapter_proposal_round_trip(self) -> None:
        gen = adapt_legacy_generator(StaticActionGenerator(["trivial"]))
        proposal = list(gen.generate(_request()))[0]
        # Rationale/target are finalized by the controller, so fill them before
        # the round trip is well-formed (validate would otherwise reject the
        # empty placeholder rationale).
        from dataclasses import replace

        proposal = replace(
            proposal, action=replace(proposal.action, rationale="realize")
        )
        restored = structured_action_proposal_from_dict(proposal.to_dict())
        self.assertEqual(restored, proposal)

    def test_adapt_is_idempotent(self) -> None:
        once = adapt_legacy_generator(StaticActionGenerator(["trivial"]))
        twice = adapt_legacy_generator(once)
        self.assertIs(once, twice)

    def test_already_native_generator_passes_through(self) -> None:
        class NativeGen:
            _is_structured_generator = True

            def generate(self, request):  # pragma: no cover - not exercised
                return ()

        native = NativeGen()
        self.assertIs(adapt_legacy_generator(native), native)


if __name__ == "__main__":
    unittest.main()
