from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.runtime.trace_pretty import TraceReadError, format_trace, read_trace


def _failure_events() -> list[dict[str, object]]:
    return [
        {
            "event": "run_summary",
            "run_id": "run-1",
            "accepted": False,
            "stop_reason": "generation:provider_error",
            "attempt_count": 0,
            "task": {"task_id": "sample"},
            "budget": {
                "elapsed_seconds": 182.5,
                "model_calls_used": 1,
                "checks_used": 0,
            },
            "metadata": {
                "generation_failures": [
                    {
                        "reason": "provider_error",
                        "message": "read timed out",
                        "model": "model-x",
                        "provider_requests": [
                            {
                                "status": "failed",
                                "retry_index": 2,
                                "wall_time_ms": 60_000,
                                "error": "TimeoutError",
                                "http_status": 405,
                            }
                        ],
                        "tool_calls": [
                            {
                                "tool_kind": "check_lean_snippet",
                                "status": "completed",
                                "wall_time_ms": 59_900,
                            }
                        ],
                    }
                ]
            },
        }
    ]


def test_formats_provider_failure_before_verbose_events() -> None:
    rendered = format_trace(_failure_events(), source="trace.jsonl")

    assert "[FAILED] task=sample stop=generation:provider_error" in rendered
    assert "GENERATION ERROR" in rendered
    assert "read timed out" in rendered
    assert "status=failed retry=2 time=60.000s http=405 error=TimeoutError" in rendered
    assert "check_lean_snippet status=completed time=59.900s" in rendered
    assert "LEAN ATTEMPTS: none" in rendered


def test_formats_lean_feedback_and_optional_proof() -> None:
    events = [
        {
            "event": "run_summary",
            "run_id": "run-2",
            "accepted": False,
            "stop_reason": "budget:max_checks",
            "attempt_count": 1,
            "task": {"task_id": "lean-sample"},
            "budget": {"elapsed_seconds": 1, "model_calls_used": 1, "checks_used": 1},
            "metadata": {},
        },
        {
            "event": "attempt",
            "run_id": "run-2",
            "attempt": {
                "attempt_index": 0,
                "candidate_file": "Attempt.lean",
                "edit": {"text": "exact rfl", "metadata": {}},
                "check_result": {
                    "accepted": False,
                    "category": "type_mismatch",
                    "elapsed_seconds": 0.25,
                    "parsed_feedback": {
                        "message": "type mismatch",
                        "line": 7,
                        "column": 3,
                        "unsolved_goals": ["x : Nat\n|- x = x"],
                    },
                },
            },
        },
    ]

    rendered = format_trace(events, show_proof=True)
    assert "[0] FAIL type_mismatch at 7:3 elapsed=0.250s" in rendered
    assert "type mismatch" in rendered
    assert "x : Nat" in rendered
    assert "exact rfl" in rendered


def test_latest_selects_last_appended_run() -> None:
    events = _failure_events() + [
        {
            "event": "run_summary",
            "run_id": "run-3",
            "accepted": True,
            "stop_reason": "accepted",
            "attempt_count": 1,
            "task": {"task_id": "retry"},
            "budget": {},
            "metadata": {},
        }
    ]
    rendered = format_trace(events, latest=True)
    assert "task=retry" in rendered
    assert "task=sample" not in rendered


def test_read_trace_reports_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text(json.dumps({"event": "run_summary"}) + "\n{bad\n", encoding="utf-8")
    with pytest.raises(TraceReadError, match=r"trace\.jsonl:2:2: invalid JSON"):
        read_trace(path)
