from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.benchmarks.minif2f import (
    MiniF2FError,
    extract_declarations,
    prepare_minif2f,
    validate_prepared_minif2f,
)
from agent.benchmarks.minif2f_eligibility import run_minif2f_eligibility
from agent.proof_system.base import CheckResult, DiagnosticCategory


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
