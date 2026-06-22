from __future__ import annotations

import tempfile
import unittest

from agent.runtime.workspace import AttemptWorkspace
from agent.search.controller import ControllerConfig, ProofController
from agent.search.execution import ExecutionMode
from agent.search.factory import StructuredModeUnavailableError, build_controller
from agent.search.structured import StructuredController

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

    def test_rejects_mode_mismatch_between_factory_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "does not match"):
                build_controller(
                    ExecutionMode.MINIMAL,
                    **self._kwargs(tmp),
                    config=ControllerConfig(execution_mode=ExecutionMode.STRUCTURED),
                )

    def test_structured_returns_structured_controller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = build_controller(
                ExecutionMode.STRUCTURED,
                **self._kwargs(tmp),
                config=ControllerConfig(execution_mode=ExecutionMode.STRUCTURED),
            )

        self.assertIsInstance(controller, StructuredController)
        self.assertEqual(controller.config.execution_mode, ExecutionMode.STRUCTURED)

    def test_structured_rejects_minimal_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "does not match"):
                build_controller(
                    ExecutionMode.STRUCTURED,
                    **self._kwargs(tmp),
                    config=ControllerConfig(execution_mode=ExecutionMode.MINIMAL),
                )

    def test_structured_mode_unavailable_error_class_retained(self) -> None:
        # The class is kept for backward-compatible imports (CLI defensive
        # handler, external callers) even though structured is now implemented.
        self.assertTrue(issubclass(StructuredModeUnavailableError, NotImplementedError))


if __name__ == "__main__":
    unittest.main()
