from __future__ import annotations

import tempfile
import unittest

from agent.runtime.workspace import AttemptWorkspace
from agent.search.controller import ControllerConfig, ProofController
from agent.search.execution import ExecutionMode
from agent.search.factory import StructuredModeUnavailableError, build_controller

from test_controller import FakeAdapter, QueueGenerator


class BuildControllerTests(unittest.TestCase):
    def _kwargs(self, tmp: str) -> dict:
        return {
            "adapter": FakeAdapter(),
            "action_generator": QueueGenerator([["trivial"]]),
            "workspace": AttemptWorkspace(tmp),
        }

    def test_minimal_returns_proof_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = build_controller(ExecutionMode.MINIMAL, **self._kwargs(tmp))

        self.assertIsInstance(controller, ProofController)
        # Default construction still yields minimal mode, observable on the
        # config so the trace records the common observation field.
        self.assertEqual(controller.config.execution_mode, ExecutionMode.MINIMAL)

    def test_minimal_propagates_explicit_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = build_controller(
                ExecutionMode.MINIMAL,
                **self._kwargs(tmp),
                config=ControllerConfig(execution_mode=ExecutionMode.MINIMAL),
            )

        self.assertIsInstance(controller, ProofController)
        self.assertEqual(controller.config.execution_mode, ExecutionMode.MINIMAL)

    def test_structured_raises_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(StructuredModeUnavailableError) as ctx:
                build_controller(ExecutionMode.STRUCTURED, **self._kwargs(tmp))

        self.assertIn("not implemented", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
