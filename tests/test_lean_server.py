from __future__ import annotations

import io
import unittest

from agent.proof_system.lean_server import LeanServerError, _read_exact


class _ShortReadStream(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        return super().read(min(size, 3) if size >= 0 else size)


class LeanServerFramingTests(unittest.TestCase):
    def test_read_exact_collects_short_pipe_reads(self) -> None:
        stream = _ShortReadStream(b'123456789')
        self.assertEqual(_read_exact(stream, 9), b'123456789')

    def test_read_exact_rejects_truncated_message(self) -> None:
        with self.assertRaisesRegex(LeanServerError, r"3/5 bytes"):
            _read_exact(io.BytesIO(b'123'), 5)


if __name__ == "__main__":
    unittest.main()
