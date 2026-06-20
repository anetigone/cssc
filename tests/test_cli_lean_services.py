from __future__ import annotations

import unittest
from types import SimpleNamespace

from agent.cli.app import _lean_services


class LeanServicesTests(unittest.TestCase):
    def test_validation_adapter_uses_server_by_default(self) -> None:
        args = SimpleNamespace(no_lake=True, no_lean_server=False, allow_sorry=False)

        with _lean_services(args, project_root=None) as services:
            self.assertTrue(services.adapter.use_server)
            self.assertTrue(services.validation_adapter.use_server)
            self.assertFalse(services.validation_adapter.disallow_sorry)

    def test_no_lean_server_disables_both_adapters(self) -> None:
        args = SimpleNamespace(no_lake=True, no_lean_server=True, allow_sorry=False)

        with _lean_services(args, project_root=None) as services:
            self.assertFalse(services.adapter.use_server)
            self.assertFalse(services.validation_adapter.use_server)


if __name__ == "__main__":
    unittest.main()
