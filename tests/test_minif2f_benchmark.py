from __future__ import annotations

import json
from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import scripts.minif2f_benchmark_run as benchmark_cli

from agent.benchmarks.minif2f import (
    MiniF2FError,
    extract_declarations,
    prepare_minif2f,
    validate_prepared_minif2f,
)
from agent.benchmarks.minif2f_eligibility import run_minif2f_eligibility
from agent.proof_system.base import CheckResult, DiagnosticCategory
from agent.proof_system.base import CandidateEdit, ParsedFeedback
from agent.search.budget import BudgetSnapshot
from agent.search.controller.types import AttemptRecord, ControllerResult
from agent.benchmarks.minif2f_runner import (
    _classify_infrastructure_failure,
    run_minif2f_benchmark,
)


class _AcceptingChecker:
    def __init__(self) -> None:
        self.checked: list[Path] = []

    def check(self, candidate_file: Path, budget_slice: object) -> CheckResult:
        self.checked.append(candidate_file)
        assert "{{proof}}" not in candidate_file.read_text(encoding="utf-8")
        assert "sorry" in candidate_file.read_text(encoding="utf-8")
        return CheckResult(
            accepted=True,
            category=DiagnosticCategory.PROOF_ACCEPTED,
            raw_output="",
            candidate_file=candidate_file,
            command=("fake-lean", str(candidate_file)),
            exit_code=0,
            elapsed_seconds=0.01,
        )

    def close(self) -> None:
        return None


def _write_checkout(root: Path, *, valid: str, test: str) -> None:
    source_dir = root / "MiniF2F"
    source_dir.mkdir(parents=True)
    (source_dir / "Valid.lean").write_text(valid, encoding="utf-8")
    (source_dir / "Test.lean").write_text(test, encoding="utf-8")
    (source_dir / "ProblemImports.lean").write_text("import Mathlib\n", encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:test\n", encoding="utf-8")
    (root / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
    (root / "lake-manifest.json").write_text(
        json.dumps(
            {
                "packages": [
                    {
                        "name": "mathlib",
                        "url": "https://example.invalid/mathlib4",
                        "rev": "abc123",
                        "inputRev": "test",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (root / "LICENSE").write_text("test license\n", encoding="utf-8")


def test_extracts_main_tasks_and_drops_variants_and_proofs(tmp_path: Path) -> None:
    root = tmp_path / "miniF2F"
    _write_checkout(
        root,
        valid="""import MiniF2F.ProblemImports
open scoped Nat

/-- Synthetic validation statement. -/
theorem valid_main (n : Nat) : n = n := by
  sorry

-- A commented-out experiment must not become part of valid_main.
/- theorem hidden_experiment : True := by
  trivial -/

theorem valid_main.variants.rfl (n : Nat) : n = n := by
  rfl
""",
        test="""import MiniF2F.ProblemImports
open scoped Nat

/-- Synthetic test statement. -/
theorem test_main : 194 % 11 = 7 :=
  rfl
""",
    )

    tasks = extract_declarations(root, expected_split_counts={"valid": 1, "test": 1})

    assert [task.task_id for task in tasks] == ["valid_main", "test_main"]
    assert all(task.scaffold.count("{{proof}}") == 1 for task in tasks)
    assert all("sorry" not in task.scaffold for task in tasks)
    assert "hidden_experiment" not in tasks[0].scaffold
    assert "rfl" not in tasks[1].scaffold
    assert "open scoped Nat" in tasks[0].scaffold


def test_prepare_writes_hash_checked_single_hole_suite(tmp_path: Path) -> None:
    root = tmp_path / "miniF2F"
    output = tmp_path / "prepared"
    _write_checkout(
        root,
        valid="""import MiniF2F.ProblemImports
theorem valid_main (x : Nat) : x = x := by
  sorry
""",
        test="""import MiniF2F.ProblemImports
theorem test_main (x : Nat) : x = x := by
  exact rfl
""",
    )

    suite = prepare_minif2f(
        root,
        output,
        expected_split_counts={"valid": 1, "test": 1},
        source_revision="0123456789abcdef",
        source_url="https://example.invalid/miniF2F",
    )

    assert suite.split_counts == {"valid": 1, "test": 1}
    assert validate_prepared_minif2f(
        output, expected_split_counts={"valid": 1, "test": 1}
    ) == {"valid": 1, "test": 1}
    rows = [json.loads(line) for line in suite.manifest_path.read_text().splitlines()]
    assert {row["eligibility"] for row in rows} == {"not_checked"}
    assert {row["ground_truth_hidden"] for row in rows} == {True}
    provenance = json.loads(suite.provenance_path.read_text())
    assert provenance["lean_toolchain"] == "leanprover/lean4:test"
    assert provenance["dependencies"]["mathlib"]["rev"] == "abc123"


def test_rejects_task_name_overlap_between_splits(tmp_path: Path) -> None:
    root = tmp_path / "miniF2F"
    shared = """import MiniF2F.ProblemImports
theorem duplicate_name : True := by
  sorry
"""
    _write_checkout(root, valid=shared, test=shared)

    with pytest.raises(MiniF2FError, match="both valid and test"):
        extract_declarations(root, expected_split_counts={"valid": 1, "test": 1})


def test_validation_detects_modified_generated_fixture(tmp_path: Path) -> None:
    root = tmp_path / "miniF2F"
    output = tmp_path / "prepared"
    source = """import MiniF2F.ProblemImports
theorem synthetic_task : True := by
  sorry
"""
    _write_checkout(
        root,
        valid=source.replace("synthetic_task", "valid_task"),
        test=source.replace("synthetic_task", "test_task"),
    )
    prepare_minif2f(
        root,
        output,
        expected_split_counts={"valid": 1, "test": 1},
        source_revision="test-revision",
    )
    fixture = output / "fixtures" / "test" / "test_task.lean"
    fixture.write_text(fixture.read_text() + "\n-- modified\n", encoding="utf-8")

    with pytest.raises(MiniF2FError, match="hash mismatch"):
        validate_prepared_minif2f(
            output, expected_split_counts={"valid": 1, "test": 1}
        )


def test_eligibility_checks_independent_files_and_updates_evidence(tmp_path: Path) -> None:
    root = tmp_path / "miniF2F"
    output = tmp_path / "prepared"
    source = """import MiniF2F.ProblemImports
theorem synthetic_task : True := by
  sorry
"""
    _write_checkout(
        root,
        valid=source.replace("synthetic_task", "valid_task"),
        test=source.replace("synthetic_task", "test_task"),
    )
    prepare_minif2f(
        root,
        output,
        expected_split_counts={"valid": 1, "test": 1},
        source_revision="test-revision",
    )
    checker = _AcceptingChecker()

    summary = run_minif2f_eligibility(
        output,
        root,
        checker=checker,
        expected_split_counts={"valid": 1, "test": 1},
    )

    assert summary.total == summary.eligible == 2
    assert summary.ineligible == summary.infrastructure_failure == 0
    assert len(checker.checked) == 2
    assert checker.checked[0] != checker.checked[1]
    rows = [json.loads(line) for line in (output / "manifest.jsonl").read_text().splitlines()]
    assert {row["eligibility"] for row in rows} == {"eligible"}
    provenance = json.loads((output / "provenance.json").read_text())
    assert provenance["eligibility"]["eligible"] == 2


def test_benchmark_runner_reuses_one_adapter_and_writes_per_task_results(tmp_path: Path) -> None:
    root = tmp_path / "miniF2F"
    output = tmp_path / "prepared"
    source = """import MiniF2F.ProblemImports
theorem synthetic_task : True := by
  sorry
"""
    _write_checkout(
        root,
        valid=source.replace("synthetic_task", "valid_task"),
        test=source.replace("synthetic_task", "test_task"),
    )
    prepare_minif2f(
        root,
        output,
        expected_split_counts={"valid": 1, "test": 1},
        source_revision="test-revision",
    )
    rows = [json.loads(line) for line in (output / "manifest.jsonl").read_text().splitlines()]
    for row in rows:
        row["eligibility"] = "eligible"
    (output / "manifest.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    def accepted_result(args, task, services, work_dir, check_workspace, project_root):
        check = CheckResult(
            accepted=True,
            category=DiagnosticCategory.PROOF_ACCEPTED,
            raw_output="",
            parsed_feedback=ParsedFeedback(DiagnosticCategory.PROOF_ACCEPTED),
            command=("lean", "--server"),
            exit_code=0,
        )
        attempt = AttemptRecord(0, "candidate", CandidateEdit("trivial"), Path(work_dir) / "x.lean", check)
        return ControllerResult(
            task=task,
            accepted=True,
            attempts=(attempt,),
            accepted_attempt=attempt,
            budget=BudgetSnapshot(1, 0, 0.1, 0, 1),
            stop_reason="accepted",
        )

    adapter = MagicMock()
    run_root = tmp_path / "run"
    with (
        patch("agent.benchmarks.minif2f_runner.LeanAdapter", return_value=adapter) as adapter_class,
        patch("agent.benchmarks.minif2f_runner._prewarm") as prewarm,
        patch("agent.benchmarks.minif2f_runner._run_controller", side_effect=accepted_result) as run,
    ):
        summary = run_minif2f_benchmark(
            output,
            root,
            run_root,
            split="valid",
            proof_args=("--candidate", "trivial"),
        )

    assert summary.completed == summary.accepted == 1
    adapter_class.assert_called_once()
    assert adapter_class.call_args.kwargs["require_server"] is True
    prewarm.assert_called_once()
    run.assert_called_once()
    adapter.close.assert_called_once()
    result = json.loads((run_root / "tasks" / "valid_task" / "result.json").read_text())
    assert result["ok"] is True
    assert (run_root / "tasks" / "valid_task" / "trace.jsonl").is_file()
    assert json.loads((run_root / "summary.json").read_text())["execution_mode"] == (
        "minimal"
    )

    with (
        patch("agent.benchmarks.minif2f_runner.LeanAdapter", return_value=adapter),
        patch("agent.benchmarks.minif2f_runner._prewarm"),
        patch("agent.benchmarks.minif2f_runner._run_controller") as resumed_run,
    ):
        resumed = run_minif2f_benchmark(
            output,
            root,
            run_root,
            split="valid",
            proof_args=("--candidate", "trivial"),
            resume=True,
        )
    assert resumed.skipped == 1
    resumed_run.assert_not_called()

    # Pre-fix results have the decisive stop_reason even though the old runner
    # incorrectly wrote infrastructure_failure=false.
    result_path = run_root / "tasks" / "valid_task" / "result.json"
    saved = json.loads(result_path.read_text())
    saved.update(
        {
            "ok": False,
            "stop_reason": "generation:provider_error",
            "infrastructure_failure": False,
        }
    )
    saved.pop("last_category", None)
    saved.pop("last_message", None)
    result_path.write_text(json.dumps(saved))
    recounted = run_minif2f_benchmark(
        output,
        root,
        run_root,
        split="valid",
        proof_args=("--candidate", "trivial"),
        resume=True,
        retry_infrastructure_failures=False,
    )
    assert recounted.failed == 0
    assert recounted.infrastructure_failures == 1
    recounted_payload = json.loads((run_root / "summary.json").read_text())
    assert recounted_payload["failed_tasks"] == []
    assert recounted_payload["infrastructure_failure_tasks"] == [
        {
            "kind": None,
            "stop_reason": "generation:provider_error",
            "task_id": "valid_task",
        }
    ]
    assert recounted_payload["error_history"] == [
        {
            "classification": "infrastructure",
            "kind": None,
            "stop_reason": "generation:provider_error",
            "task_id": "valid_task",
        }
    ]

    with (
        patch("agent.benchmarks.minif2f_runner.LeanAdapter", return_value=adapter),
        patch("agent.benchmarks.minif2f_runner._prewarm"),
        patch("agent.benchmarks.minif2f_runner._run_controller", side_effect=accepted_result) as retry,
    ):
        retried = run_minif2f_benchmark(
            output,
            root,
            run_root,
            split="valid",
            proof_args=("--candidate", "trivial"),
            resume=True,
        )
    assert retried.accepted == 1
    assert retried.infrastructure_failures == 0
    assert retried.skipped == 0
    retry.assert_called_once()
    retried_summary = json.loads((run_root / "summary.json").read_text())
    assert retried_summary["infrastructure_failure_tasks"] == []
    assert retried_summary["error_history"][0]["stop_reason"] == (
        "generation:provider_error"
    )

    saved = json.loads(result_path.read_text())
    saved.update(
        {
            "ok": False,
            "stop_reason": "generation:model_output_truncated",
            "infrastructure_failure": False,
            "generation_failures": [
                {
                    "reason": "model_output_truncated",
                    "message": "Model produced no usable proof candidate.",
                }
            ],
        }
    )
    saved.pop("last_category", None)
    saved.pop("last_message", None)
    result_path.write_text(json.dumps(saved))
    ordinary_failure = run_minif2f_benchmark(
        output,
        root,
        run_root,
        split="valid",
        proof_args=("--candidate", "trivial"),
        resume=True,
        retry_transient_generation_failures=False,
    )
    assert ordinary_failure.failed == 1
    failure_summary = json.loads((run_root / "summary.json").read_text())
    assert failure_summary["failed_tasks"] == [
        {
            "message": "Model produced no usable proof candidate.",
            "stop_reason": "generation:model_output_truncated",
            "task_id": "valid_task",
        }
    ]
    assert [item["stop_reason"] for item in failure_summary["error_history"]] == [
        "generation:provider_error",
        "generation:model_output_truncated",
    ]

    with (
        patch("agent.benchmarks.minif2f_runner.LeanAdapter", return_value=adapter),
        patch("agent.benchmarks.minif2f_runner._prewarm"),
        patch(
            "agent.benchmarks.minif2f_runner._run_controller",
            side_effect=accepted_result,
        ) as retry_truncated,
    ):
        recovered = run_minif2f_benchmark(
            output,
            root,
            run_root,
            split="valid",
            proof_args=("--candidate", "trivial"),
            resume=True,
        )
    assert recovered.accepted == 1
    assert recovered.failed == 0
    retry_truncated.assert_called_once()
    recovered_summary = json.loads((run_root / "summary.json").read_text())
    assert recovered_summary["failed_tasks"] == []
    assert [item["stop_reason"] for item in recovered_summary["error_history"]] == [
        "generation:provider_error",
        "generation:model_output_truncated",
    ]


def test_provider_generation_error_is_infrastructure_without_checker_attempts() -> None:
    result = MagicMock(stop_reason="generation:provider_error", attempts=())
    assert _classify_infrastructure_failure(result) == (
        True,
        "generation:provider_error",
    )


def test_benchmark_cli_exposes_and_forwards_execution_mode(tmp_path: Path) -> None:
    # Omission preserves the historical proof_args hash on resume; cssc prove
    # itself supplies the minimal default.
    assert benchmark_cli.build_parser().parse_args(
        ["--split", "valid"]
    ).execution_mode is None
    summary = SimpleNamespace(
        infrastructure_failures=0,
        completed=1,
        selected=1,
        run_id="mode-test",
        run_root=tmp_path / "run",
        accepted=1,
        failed=0,
        skipped=0,
    )
    with patch.object(
        benchmark_cli, "run_minif2f_benchmark", return_value=summary
    ) as run:
        exit_code = benchmark_cli.main(
            [
                "--split", "valid",
                "--run-name", "mode-test",
                "--execution-mode", "structured",
                "--",
                "--candidate", "trivial",
            ]
        )

    assert exit_code == 0
    assert run.call_args.kwargs["proof_args"] == [
        "--execution-mode", "structured", "--candidate", "trivial"
    ]
