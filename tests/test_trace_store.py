from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent.proof_system.base import (
    CandidateEdit,
    CheckResult,
    DiagnosticCategory,
    GoalState,
    ParsedFeedback,
    ProgressSignal,
    ProofTask,
)
from agent.runtime.trace_store import JsonlTraceStore, result_events
from agent.search.budget import BudgetSnapshot
from agent.search.cost_ledger import (
    CostLedger,
    CostLedgerEvent,
    CostLedgerEventKind,
    CostMeasurement,
    CostScope,
)
from agent.search.controller import AttemptRecord, ControllerResult
from agent.search.memory import MemoryProcessor, MemoryUpdate, empty_memory, memory_to_dict


class TraceStoreTests(unittest.TestCase):
    def test_result_events_include_summary_and_attempt_without_raw_output(self) -> None:
        result = _sample_result()

        events = list(result_events(result))

        self.assertEqual([event["event"] for event in events], ["run_summary", "attempt"])
        self.assertEqual(events[0]["task"]["task_id"], "sample")
        self.assertEqual(events[1]["attempt"]["check_result"]["category"], "unsolved_goals")
        self.assertNotIn("raw_output", events[1]["attempt"]["check_result"])
        self.assertNotIn("progress", events[1]["attempt"]["check_result"])

    def test_run_summary_carries_proof_memory_snapshot(self) -> None:
        result = _sample_result()

        events = list(result_events(result))

        memory = events[0]["metadata"]["proof_memory"]
        self.assertEqual(memory["source_attempt_ids"], [0])
        self.assertTrue(
            memory["failed_approaches"][0].startswith("static:unsolved_goals")
        )

    def test_minimal_run_summary_omits_workspace(self) -> None:
        result = _sample_result()

        events = list(result_events(result))

        self.assertNotIn("workspace", events[0])

    def test_structured_trace_writes_workspace_snapshot_event(self) -> None:
        from agent.proof_system.workspace import initialize_from_task

        task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
        workspace = initialize_from_task(task)
        result = _sample_result()
        result = _with_metadata(result, {"workspace": workspace.to_dict()})

        events = list(result_events(result))

        self.assertNotIn("workspace", events[0])
        self.assertNotIn("workspace", events[0]["metadata"])
        self.assertEqual(events[0]["workspace_event"], "workspace_snapshot")
        self.assertEqual(events[1]["event"], "workspace_snapshot")
        self.assertEqual(events[1]["workspace"]["workspace_id"], "sample")
        self.assertEqual(events[1]["workspace"]["version"], 1)
        self.assertEqual(
            events[1]["workspace"]["obligation_graph"]["root_obligation_id"],
            "sample",
        )
        # The serialized dict also survives a JSON round-trip.
        json.loads(json.dumps(events[1]["workspace"]))

    def test_cost_ledger_is_written_as_a_separate_snapshot(self) -> None:
        result = _sample_result()
        ledger = CostLedger((
            CostLedgerEvent(
                event_id="usage-1",
                kind=CostLedgerEventKind.PROVIDER_USAGE,
                scope=CostScope.PROPOSAL_GENERATION,
                status="completed",
                request_id="request-1",
                input_tokens=CostMeasurement.observed(0),
            ),
        ))
        result = _with_metadata(result, {"cost_ledger": ledger})

        events = list(result_events(result))

        self.assertEqual([event["event"] for event in events], [
            "run_summary", "cost_ledger_snapshot", "attempt",
        ])
        self.assertEqual(events[0]["cost_ledger_event"], "cost_ledger_snapshot")
        self.assertNotIn("cost_ledger", events[0]["metadata"])
        snapshot = events[1]["cost_ledger"]
        self.assertEqual(snapshot["events"][0]["input_tokens"]["value"], 0)
        self.assertEqual(
            snapshot["reconciliation"]["totals"]["input_tokens"]["measurement_status"],
            "observed",
        )

    def test_attempt_trace_carries_structured_goal_state(self) -> None:
        result = _sample_result()

        events = list(result_events(result))

        goal_state = events[1]["attempt"]["check_result"]["parsed_feedback"][
            "goal_state"
        ]
        self.assertEqual(goal_state[0]["text"], "⊢ True")
        self.assertEqual(goal_state[0]["source_span"], [1, 2])
        self.assertFalse(goal_state[0]["is_sorry_goal"])

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


def _with_metadata(result: ControllerResult, extra: dict) -> ControllerResult:
    """Return a copy of ``result`` with ``extra`` merged into its metadata."""
    merged = {**result.metadata, **extra}
    return replace(result, metadata=merged)


def _sample_result() -> ControllerResult:
    task = ProofTask("sample", "theorem sample : True := by\n  {{proof}}\n")
    feedback = ParsedFeedback(
        category=DiagnosticCategory.UNSOLVED_GOALS,
        message="unsolved goals",
        line=1,
        column=2,
        unsolved_goals=("⊢ True",),
        goal_state=(
            GoalState(
                text="⊢ True",
                goal_fingerprint="abc123",
                declaration_id="sample",
                source_span=(1, 2),
            ),
        ),
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
    memory = MemoryProcessor().update(
        empty_memory(),
        MemoryUpdate(
            task=task,
            attempt_index=attempt.attempt_index,
            proof_text=attempt.edit.text,
            action=attempt.edit.action,
            check_result=check_result,
            feedback=feedback,
        ),
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
        metadata={"proof_memory": memory_to_dict(memory)},
    )


if __name__ == "__main__":
    unittest.main()
