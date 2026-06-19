from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.proof_system.base import (
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    ParsedFeedback,
    ProgressSignal,
    ProofTask,
)
from agent.runtime.trace_store import JsonlTraceStore, result_events
from agent.search.budget import BudgetSnapshot
from agent.search.controller import AttemptRecord, ControllerResult


class TraceStoreTests(unittest.TestCase):
    def test_result_events_include_summary_and_attempt_without_raw_output(self) -> None:
        result = _sample_result()

        events = list(result_events(result))

        self.assertEqual([event["event"] for event in events], ["run_summary", "attempt"])
        self.assertEqual(events[0]["task"]["task_id"], "sample")
        self.assertEqual(events[1]["attempt"]["check_result"]["category"], "unsolved_goals")
        self.assertNotIn("raw_output", events[1]["attempt"]["check_result"])

    def test_can_include_raw_output_when_requested(self) -> None:
        result = _sample_result()

        events = list(result_events(result, include_raw_output=True))

        self.assertEqual(events[1]["attempt"]["check_result"]["raw_output"], "raw lean output")

    def test_jsonl_trace_store_appends_events(self) -> None:
        result = _sample_result()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "traces" / "run.jsonl"
            store = JsonlTraceStore(path)

            store.append_result(result)
            store.append_result(result)

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["event"], "run_summary")
        self.assertEqual(rows[1]["event"], "attempt")

    def test_atomic_append_preserves_old_file_when_replace_fails(self) -> None:
        result = _sample_result()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.jsonl"
            path.write_text('{"existing": true}\n', encoding="utf-8")
            store = JsonlTraceStore(path)

            with patch(
                "agent.runtime.trace_store.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    store.append_result(result)

            self.assertEqual(path.read_text(encoding="utf-8"), '{"existing": true}\n')
            self.assertEqual(list(path.parent.glob(".run.jsonl.*.tmp")), [])


def _sample_result() -> ControllerResult:
    task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
    feedback = ParsedFeedback(
        category=DiagnosticCategory.UNSOLVED_GOALS,
        message="unsolved goals",
        line=1,
        column=2,
        raw_output="raw lean output",
    )
    check_result = CheckResult(
        accepted=False,
        category=DiagnosticCategory.UNSOLVED_GOALS,
        raw_output="raw lean output",
        candidate_file=Path("candidate.lean"),
        command=("lean", "candidate.lean"),
        exit_code=1,
        elapsed_seconds=0.1,
        parsed_feedback=feedback,
        progress=ProgressSignal(diagnostic_category=DiagnosticCategory.UNSOLVED_GOALS),
    )
    attempt = AttemptRecord(
        attempt_index=0,
        candidate_id="candidate-id",
        edit=CandidateEdit("trivial", action="static"),
        candidate_file=Path("candidate.lean"),
        check_result=check_result,
    )
    return ControllerResult(
        task=task,
        accepted=False,
        attempts=(attempt,),
        budget=BudgetSnapshot(
            checks_used=1,
            model_calls_used=1,
            elapsed_seconds=0.2,
            remaining_checks=0,
            remaining_model_calls=0,
            exhausted_reason="checks",
        ),
        stop_reason="budget:checks",
    )


if __name__ == "__main__":
    unittest.main()
