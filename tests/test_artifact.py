from __future__ import annotations

import unittest

from agent.proof_system.workspace.artifact import (
    LeanArtifact,
    lean_artifact_from_dict,
)


class LeanArtifactSerializationTest(unittest.TestCase):
    def test_minimal_fields_round_trip(self) -> None:
        artifact = LeanArtifact(
            source="theorem t : True := by trivial",
            obligation_id="root",
            obligation_version=1,
        )
        restored = lean_artifact_from_dict(artifact.to_dict())
        self.assertEqual(restored, artifact)
        self.assertIsNone(restored.declaration_id)
        self.assertIsNone(restored.source_span)
        self.assertEqual(restored.proof_body, "")

    def test_full_fields_round_trip(self) -> None:
        artifact = LeanArtifact(
            source="theorem helper : ... := by sorry",
            obligation_id="helper",
            obligation_version=2,
            declaration_id="helper",
            source_span=(4, 17),
            proof_body="sorry",
        )
        restored = lean_artifact_from_dict(artifact.to_dict())
        self.assertEqual(restored, artifact)


if __name__ == "__main__":
    unittest.main()
