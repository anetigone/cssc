from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from agent.runtime.logging_config import configure_logging


class LoggingConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        logging.shutdown()
        logging.basicConfig(handlers=[], force=True)

    def test_configures_root_level(self) -> None:
        configure_logging(level="DEBUG")

        self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_writes_to_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "logs" / "run.log"
            configure_logging(level="INFO", log_file=str(path))

            logging.getLogger("agent.test").info("hello")
            logging.shutdown()

            self.assertIn("hello", path.read_text(encoding="utf-8"))

    def test_rejects_unknown_level(self) -> None:
        with self.assertRaises(ValueError):
            configure_logging(level="NOPE")


if __name__ == "__main__":
    unittest.main()
