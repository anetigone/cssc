from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.runtime.env_loader import load_dotenv


class EnvLoaderTests(unittest.TestCase):
    def test_loads_common_dotenv_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "\n".join(
                    [
                        "# comment",
                        "OPENAI_API_KEY=abc",
                        "OPENAI_BASE_URL='https://example.test/v1'",
                        'OPENAI_MODEL="model-name"',
                        "export EXTRA=value # inline comment",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                loaded = load_dotenv(path)

                self.assertEqual(os.environ["OPENAI_API_KEY"], "abc")
                self.assertEqual(os.environ["OPENAI_BASE_URL"], "https://example.test/v1")
                self.assertEqual(os.environ["OPENAI_MODEL"], "model-name")
                self.assertEqual(os.environ["EXTRA"], "value")
                self.assertEqual(loaded["OPENAI_API_KEY"], "abc")

    def test_existing_environment_wins_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("OPENAI_MODEL=from_file\n", encoding="utf-8")

            with patch.dict(os.environ, {"OPENAI_MODEL": "from_env"}, clear=True):
                load_dotenv(path)

                self.assertEqual(os.environ["OPENAI_MODEL"], "from_env")

    def test_override_can_replace_existing_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("OPENAI_MODEL=from_file\n", encoding="utf-8")

            with patch.dict(os.environ, {"OPENAI_MODEL": "from_env"}, clear=True):
                load_dotenv(path, override=True)

                self.assertEqual(os.environ["OPENAI_MODEL"], "from_file")

    def test_preserves_bare_values_with_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "\n".join(
                    [
                        "OPENAI_BASE_URL=http://x.com/path#anchor",
                        "WITH_COMMENT=value # inline comment",
                        "QUOTED='http://x.com/path#anchor' # inline comment",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                loaded = load_dotenv(path)

                self.assertEqual(loaded["OPENAI_BASE_URL"], "http://x.com/path#anchor")
                self.assertEqual(loaded["WITH_COMMENT"], "value")
                self.assertEqual(loaded["QUOTED"], "http://x.com/path#anchor")


if __name__ == "__main__":
    unittest.main()
