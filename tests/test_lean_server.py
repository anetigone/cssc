from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock

from agent.proof_system.lean_server import (
    LeanServerAmbiguousCompletion,
    LeanServerClient,
    LeanServerError,
    _read_exact,
)


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

    def test_close_does_not_leak_invalid_handle_error(self) -> None:
        client = LeanServerClient(["lean", "--server"], cwd=None, root=None)
        process = MagicMock()
        process.poll.return_value = None
        process.terminate.side_effect = OSError(22, "Invalid argument")
        process.kill.side_effect = OSError(22, "Invalid argument")
        client._process = process
        client._request = MagicMock(side_effect=OSError(22, "Invalid argument"))

        client.close()

        process.kill.assert_called_once()
        self.assertIsNone(client._process)


class LeanServerCompletionTests(unittest.TestCase):
    def test_initial_empty_diagnostics_do_not_complete_active_processing(self) -> None:
        client = LeanServerClient(
            ["lean", "--server"],
            cwd=None,
            root=None,
            diagnostics_fallback_seconds=0.01,
        )
        uri = "file:///Attempt.lean"

        client._handle_message(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": []},
            }
        )
        self.assertNotIn(uri, client._completed_documents)
        client._handle_message(
            {
                "method": "$/lean/fileProgress",
                "params": {"textDocument": {"uri": uri}, "processing": [{"kind": 1}]},
            }
        )
        self.assertNotIn(uri, client._completed_documents)

        diagnostic = {"severity": 1, "message": "type mismatch"}
        client._handle_message(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": [diagnostic]},
            }
        )
        client._handle_message(
            {
                "method": "$/lean/fileProgress",
                "params": {"textDocument": {"uri": uri}, "processing": []},
            }
        )

        self.assertEqual(
            client._wait_for_check_completion(uri, timeout_seconds=0.1),
            [diagnostic],
        )

    def test_second_diagnostics_without_progress_returns_diagnostics_via_fallback(self) -> None:
        client = LeanServerClient(
            ["lean", "--server"],
            cwd=None,
            root=None,
            diagnostics_fallback_seconds=0.01,
        )
        uri = "file:///Attempt.lean"
        client._handle_message(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": []},
            }
        )
        self.assertNotIn(uri, client._completed_documents)
        diagnostic = {"severity": 1, "message": "unknown identifier"}
        client._handle_message(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": [diagnostic]},
            }
        )

        result = client._wait_for_check_completion(uri, timeout_seconds=0.1)
        self.assertEqual(result, [diagnostic])

    def test_single_diagnostics_publication_times_out_without_fallback(self) -> None:
        client = LeanServerClient(["lean", "--server"], cwd=None, root=None)
        uri = "file:///Attempt.lean"
        client._handle_message(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": []},
            }
        )

        with self.assertRaises(LeanServerAmbiguousCompletion):
            client._wait_for_check_completion(uri, timeout_seconds=0.01)

    def test_empty_diagnostics_after_processing_are_accepted_via_fallback(self) -> None:
        client = LeanServerClient(
            ["lean", "--server"],
            cwd=None,
            root=None,
            diagnostics_fallback_seconds=0.01,
        )
        uri = "file:///Attempt.lean"
        client._handle_message(
            {
                "method": "$/lean/fileProgress",
                "params": {"textDocument": {"uri": uri}, "processing": [{"kind": 1}]},
            }
        )
        client._handle_message(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": []},
            }
        )
        client._handle_message(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": []},
            }
        )

        result = client._wait_for_check_completion(uri, timeout_seconds=0.1)
        self.assertEqual(result, [])

    def test_progress_clears_diagnostic_fallback_mid_elaboration(self) -> None:
        # Once the server reports active elaboration via fileProgress, a pending
        # diagnostic fallback deadline must be cleared so a slow elaboration is
        # not misreported as an ambiguous completion while still in progress.
        client = LeanServerClient(
            ["lean", "--server"],
            cwd=None,
            root=None,
            diagnostics_fallback_seconds=0.01,
        )
        uri = "file:///Attempt.lean"
        # Second publication (replacing the didOpen placeholder) arms a fallback.
        client._handle_message(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": []},
            }
        )
        client._handle_message(
            {
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": [{"severity": 1, "message": "x"}]},
            }
        )
        self.assertIn(uri, client._diagnostic_fallback_deadlines)
        # Active elaboration supersedes the fallback: it is removed and not
        # re-armed by a later diagnostic publication.
        client._handle_message(
            {
                "method": "$/lean/fileProgress",
                "params": {"textDocument": {"uri": uri}, "processing": [{"kind": 1}]},
            }
        )
        self.assertNotIn(uri, client._diagnostic_fallback_deadlines)


if __name__ == "__main__":
    unittest.main()
