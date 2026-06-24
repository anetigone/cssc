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
    PAYLOAD_KIND_DECOMPOSE,
    PAYLOAD_KIND_IMPLEMENT,
    SUPPORTED_PROPOSAL_KINDS,
    CapabilityTestPayload,
    DecomposeChildSpec,
    DecomposePayload,
    ImplementPayload,
    StructuredActionProposal,
    _LegacyActionGeneratorAdapter,
    adapt_legacy_generator,
    capability_test_payload_from_dict,
    decompose_child_spec_from_dict,
    decompose_payload_from_dict,
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
