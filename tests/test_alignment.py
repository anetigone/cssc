from __future__ import annotations

import unittest

from agent.proof_system.workspace.alignment import (
    AlignmentLink,
    AlignmentRelation,
    alignment_link_from_dict,
)


class AlignmentRelationTest(unittest.TestCase):
    def test_enum_values(self) -> None:
        self.assertEqual(AlignmentRelation.IMPLEMENTS.value, "implements")
        self.assertEqual(AlignmentRelation.PARTIAL.value, "partial")
        self.assertEqual(AlignmentRelation.UNALIGNED.value, "unaligned")


class AlignmentLinkSerializationTest(unittest.TestCase):
    def test_full_link_round_trip(self) -> None:
        link = AlignmentLink(
            argument_step_id="s3",
            lean_declaration_id="helper_nonzero",
            source_span=(7, 22),
            goal_fingerprint="a1b2c3d4e5f6",
            relation=AlignmentRelation.IMPLEMENTS,
        )
        restored = alignment_link_from_dict(link.to_dict())
        self.assertEqual(restored, link)

    def test_defaults_to_unaligned(self) -> None:
        link = alignment_link_from_dict({"argument_step_id": "s1"})
        self.assertIsNone(link.lean_declaration_id)
        self.assertIsNone(link.source_span)
        self.assertIsNone(link.goal_fingerprint)
        self.assertEqual(link.relation, AlignmentRelation.UNALIGNED)

    def test_span_round_trips_as_list(self) -> None:
        link = AlignmentLink(
            argument_step_id="s1",
            source_span=(3, 9),
            relation=AlignmentRelation.PARTIAL,
        )
        payload = link.to_dict()
        self.assertEqual(payload["source_span"], [3, 9])
        self.assertEqual(alignment_link_from_dict(payload), link)


if __name__ == "__main__":
    unittest.main()
