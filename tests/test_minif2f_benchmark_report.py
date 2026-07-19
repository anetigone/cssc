from __future__ import annotations

import json
from pathlib import Path

import scripts.minif2f_benchmark_report as report_cli

from agent.benchmarks.minif2f_usage_report import (
    build_minif2f_usage_report,
    render_minif2f_usage_markdown,
)


def _usage(
    *,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> dict[str, int]:
    completion = output_tokens + reasoning_tokens
    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "provider_completion_tokens": completion,
        "provider_total_tokens": input_tokens + completion,
    }


def _summary_event(
    task_id: str,
    run_id: str,
    *,
    accepted: bool,
    stop_reason: str,
    model_calls: int,
    usages: list[dict[str, int]],
) -> dict[str, object]:
    return {
        "event": "run_summary",
        "run_id": run_id,
        "task": {"task_id": task_id},
        "accepted": accepted,
        "stop_reason": stop_reason,
        "attempt_count": int(accepted),
        "budget": {
            "checks_used": int(accepted),
            "model_calls_used": model_calls,
        },
        "metadata": {"model_usage": usages},
    }


def _ledger_event(
    task_id: str,
    run_id: str,
    *,
    status: str,
    request_statuses: tuple[str, ...],
) -> dict[str, object]:
    measurement = {
        "measurement_status": status,
        "value": 1 if status == "observed" else None,
    }
    return {
        "event": "cost_ledger_snapshot",
        "run_id": run_id,
        "task_id": task_id,
        "cost_ledger": {
            "events": [
                {"kind": "provider_request", "status": request_status}
                for request_status in request_statuses
            ],
            "reconciliation": {
                "totals": {
                    field: dict(measurement)
                    for field in (
                        "input_tokens",
                        "cached_tokens",
                        "output_tokens",
                        "reasoning_tokens",
                        "billed_tokens",
                        "api_cost_usd",
                    )
                }
            },
        },
    }


def _write_run(root: Path) -> None:
    root.mkdir()
    (root / "run.json").write_text(
        json.dumps(
            {
                "run_id": "synthetic-valid",
                "split": "valid",
                "benchmark_revision": "abc",
                "config_sha256": "def",
                "proof_args": ["--use-model"],
                "task_ids": ["task_a", "task_b"],
            }
        ),
        encoding="utf-8",
    )
    (root / "summary.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "selected": 2,
                "completed": 2,
                "accepted": 2,
            }
        ),
        encoding="utf-8",
    )
    for task_id, calls in (("task_a", 1), ("task_b", 2)):
        task_root = root / "tasks" / task_id
        task_root.mkdir(parents=True)
        (task_root / "result.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "stop_reason": "accepted",
                    "attempts": 1,
                    "checks_used": 1,
                    "model_calls_used": calls,
                }
            ),
            encoding="utf-8",
        )

    task_a_events = [
        _summary_event(
            "task_a",
            "failed-a",
            accepted=False,
            stop_reason="generation:provider_error",
            model_calls=1,
            usages=[],
        ),
        _ledger_event(
            "task_a",
            "failed-a",
            status="unavailable",
            request_statuses=("failed",),
        ),
        _summary_event(
            "task_a",
            "accepted-a",
            accepted=True,
            stop_reason="accepted",
            model_calls=1,
            usages=[
                _usage(
                    input_tokens=10,
                    cached_tokens=2,
                    output_tokens=3,
                    reasoning_tokens=7,
                )
            ],
        ),
        _ledger_event(
            "task_a",
            "accepted-a",
            status="observed",
            request_statuses=("completed",),
        ),
    ]
    task_b_events = [
        _summary_event(
            "task_b",
            "accepted-b",
            accepted=True,
            stop_reason="accepted",
            model_calls=2,
            usages=[
                _usage(
                    input_tokens=5,
                    cached_tokens=0,
                    output_tokens=2,
                    reasoning_tokens=3,
                ),
                _usage(
                    input_tokens=6,
                    cached_tokens=1,
                    output_tokens=1,
                    reasoning_tokens=4,
                ),
            ],
        ),
        # This models an older trace whose summary has usage but whose unified
        # ledger reconciliation did not observe it.
        _ledger_event(
            "task_b",
            "accepted-b",
            status="unavailable",
            request_statuses=("completed", "completed"),
        ),
    ]
    for task_id, events in (("task_a", task_a_events), ("task_b", task_b_events)):
        (root / "tasks" / task_id / "trace.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )


def test_usage_report_separates_current_success_from_resume_history(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    _write_run(run_root)

    report = build_minif2f_usage_report(run_root)

    assert report["outcomes"]["accepted"] == 2
    history = report["outcomes"]["trace_history"]
    assert history["sessions"] == 3
    assert history["failed_sessions"] == 1
    assert history["tasks_with_retries"] == 1
    assert history["first_session"]["accepted"] == 1
    assert history["first_session"]["raw_accepted_rate"] == 0.5

    current = report["usage"]["current_accepted_sessions"]
    assert current["sessions"] == 2
    assert current["coverage"]["complete_model_calls"] == 3
    assert current["coverage"]["expected_model_calls"] == 3
    assert current["tokens"]["input_tokens"]["observed_value"] == 21
    assert current["tokens"]["cached_tokens"]["observed_value"] == 3
    assert current["tokens"]["output_tokens"]["observed_value"] == 6
    assert current["tokens"]["reasoning_tokens"]["observed_value"] == 14
    assert current["tokens"]["provider_total_tokens"]["observed_value"] == 41

    all_history = report["usage"]["all_trace_sessions"]
    assert all_history["coverage"]["complete_model_calls"] == 3
    assert all_history["coverage"]["expected_model_calls"] == 4
    assert all_history["coverage"]["tasks_with_incomplete_usage"] == ["task_a"]
    assert all_history["tokens"]["provider_total_tokens"] == {
        "observed_value": 41,
        "observed_model_calls": 3,
        "expected_model_calls": 4,
        "complete": False,
    }
    assert report["usage"]["ledger_coverage"]["billed_tokens"] == {
        "observed": 1,
        "unavailable": 2,
    }
    assert report["usage"]["provider_request_events"] == {
        "completed": 3,
        "failed": 1,
    }


def test_usage_report_cli_writes_json_and_markdown(
    tmp_path: Path,
    capsys,
) -> None:
    run_root = tmp_path / "run"
    json_output = tmp_path / "report.json"
    markdown_output = tmp_path / "report.md"
    _write_run(run_root)

    exit_code = report_cli.main(
        [
            str(run_root),
            "--format",
            "markdown",
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
        ]
    )

    assert exit_code == 0
    assert json.loads(json_output.read_text(encoding="utf-8"))["run_id"] == (
        "synthetic-valid"
    )
    markdown = markdown_output.read_text(encoding="utf-8")
    assert "Current accepted: 2 / 2 (100.00%)" in markdown
    assert "All trace history" in markdown
    assert capsys.readouterr().out == markdown
    assert render_minif2f_usage_markdown(
        build_minif2f_usage_report(run_root)
    ).startswith("# miniF2F usage report")
