from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import phase8_benchmark_report as benchmark_report
from scripts import phase8_benchmark_run as benchmark_run


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "fixtures" / "phase8_benchmark" / "manifest.jsonl"
FIXTURES = ROOT / "tests" / "fixtures" / "phase8_benchmark"


def _summary(*, accepted: bool, mode: str = "minimal", workspace_status: str | None = None):
    metadata: dict[str, object] = {}
    if workspace_status is not None:
        metadata["result_summary"] = {
            "workspace_status": workspace_status,
            "accepted_obligations": [],
            "open_obligations": [],
            "blocked_obligations": [],
        }
    return {
        "event": "run_summary",
        "accepted": accepted,
        "stop_reason": "accepted" if accepted else "no_actions",
        "metrics": {
            "execution_mode": mode,
            "budget_model_calls_used": 1,
            "budget_checks_used": 1,
            "model_input_tokens": 10,
            "model_output_tokens": 2,
        },
        "metadata": metadata,
    }


class Phase8BenchmarkRunnerTests(unittest.TestCase):
    def test_minimal_dry_stub_does_not_invent_structured_workspace_outcome(self):
        row = {
            "task_id": "missing-capability",
            "expected_terminal": "blocked",
        }
        event = benchmark_run._dry_run_stub(row, "A0", "minimal", "legacy")
        self.assertEqual(event["metadata"], {})

    def test_dry_run_writes_provenance_refuses_collision_and_overwrites_explicitly(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            runs = Path(tmp) / "runs"
            args = [
                "--manifest",
                str(MANIFEST),
                "--fixtures-dir",
                str(FIXTURES),
                "--runs-root",
                str(runs),
                "--suite-version",
                "test-suite",
                "--task",
                "l1_canary_true",
                "--arm",
                "A0",
                "--dry-run",
            ]
            self.assertEqual(benchmark_run.main(args), 0)
            trace = runs / "test-suite" / "A0" / "l1_canary_true" / "1.jsonl"
            meta = trace.with_suffix(".meta.json")
            self.assertTrue(trace.is_file())
            provenance = json.loads(meta.read_text(encoding="utf-8"))
            self.assertEqual(provenance["status"], "completed")
            self.assertEqual(provenance["task_id"], "l1_canary_true")
            self.assertIn("git_commit", provenance)
            self.assertIn("lean_toolchain", provenance)
            self.assertIn("mathlib_rev", provenance)

            self.assertEqual(benchmark_run.main(args), 2)
            self.assertEqual(benchmark_run.main([*args, "--overwrite"]), 0)
            self.assertEqual(
                len(
                    [
                        json.loads(line)
                        for line in trace.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                        and json.loads(line).get("event") == "run_summary"
                    ]
                ),
                1,
            )

    def test_live_requires_explicit_model_for_reproducibility(self):
        self.assertEqual(
            benchmark_run.main(
                [
                    "--manifest",
                    str(MANIFEST),
                    "--task",
                    "l1_canary_true",
                    "--suite-version",
                    "model-required",
                ]
            ),
            2,
        )

    def test_cli_return_one_is_completed_unaccepted_run(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            runs = Path(tmp) / "runs"

            def fake_run_live(*args, out: Path, **kwargs):
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(_summary(accepted=False)) + "\n", encoding="utf-8")
                return 1

            with patch.object(benchmark_run, "_run_live", side_effect=fake_run_live):
                rc = benchmark_run.main(
                    [
                        "--manifest",
                        str(MANIFEST),
                        "--fixtures-dir",
                        str(FIXTURES),
                        "--runs-root",
                        str(runs),
                        "--suite-version",
                        "unaccepted-is-outcome",
                        "--task",
                        "l1_canary_true",
                        "--proof-model",
                        "test-model",
                    ]
                )
            self.assertEqual(rc, 0)
            provenance = json.loads(
                (
                    runs
                    / "unaccepted-is-outcome"
                    / "A0"
                    / "l1_canary_true"
                    / "1.meta.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(provenance["status"], "completed")
            self.assertEqual(provenance["cli_returncode"], 1)


class Phase8BenchmarkReportTests(unittest.TestCase):
    def test_blocked_goal_requires_matching_capability_evidence(self):
        event = {"accepted": False}
        manifest_row = {
            "expected_terminal": "blocked",
            "expected_min_accepted_helpers": 0,
            "expected_block_category": "unknown_identifier",
            "expected_probe_signature": "widgetGood target",
        }
        result_summary = {"workspace_status": "blocked"}
        correct_attempt = {
            "edit": {"action": "capability_test", "text": "widgetGood target"},
            "check_result": {"category": "unknown_identifier"},
        }
        self.assertFalse(
            benchmark_report._goal_attained(event, manifest_row, result_summary, [])
        )
        self.assertTrue(
            benchmark_report._goal_attained(
                event, manifest_row, result_summary, [correct_attempt]
            )
        )
        wrong = {**correct_attempt, "check_result": {"category": "type_mismatch"}}
        self.assertFalse(
            benchmark_report._goal_attained(
                event, manifest_row, result_summary, [wrong]
            )
        )

    def test_report_joins_manifest_and_scores_blocked_goal(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            base = Path(tmp)
            runs = base / "runs"
            runner_args = [
                "--manifest",
                str(MANIFEST),
                "--fixtures-dir",
                str(FIXTURES),
                "--runs-root",
                str(runs),
                "--suite-version",
                "blocked-suite",
                "--task",
                "l3_canary_capability_gap",
                "--arm",
                "A4",
                "--dry-run",
            ]
            self.assertEqual(benchmark_run.main(runner_args), 0)
            trace = (
                runs
                / "blocked-suite"
                / "A4"
                / "l3_canary_capability_gap"
                / "1.jsonl"
            )
            with trace.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "event": "attempt",
                            "attempt": {
                                "edit": {
                                    "action": "capability_test",
                                    "text": "widgetGood target",
                                },
                                "check_result": {"category": "unknown_identifier"},
                            },
                        }
                    )
                    + "\n"
                )
            output = base / "report.md"
            self.assertEqual(
                benchmark_report.main(
                    [
                        "--runs-dir",
                        str(runs),
                        "--manifest",
                        str(MANIFEST),
                        "--suite-version",
                        "blocked-suite",
                        "--output",
                        str(output),
                    ]
                ),
                0,
            )
            markdown = output.read_text(encoding="utf-8")
            self.assertIn("| accepted | expected | attained |", markdown)
            self.assertIn("| False | blocked | True |", markdown)
            self.assertIn("blocked_obl", markdown)

    def test_report_rejects_duplicate_run_summaries(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            runs = Path(tmp) / "runs"
            trace = runs / "duplicate-suite" / "A0" / "l1_canary_true" / "1.jsonl"
            trace.parent.mkdir(parents=True)
            event = json.dumps(_summary(accepted=True))
            trace.write_text(event + "\n" + event + "\n", encoding="utf-8")
            self.assertEqual(
                benchmark_report.main(
                    [
                        "--runs-dir",
                        str(runs),
                        "--manifest",
                        str(MANIFEST),
                        "--suite-version",
                        "duplicate-suite",
                        "--allow-missing-provenance",
                    ]
                ),
                1,
            )

    def test_report_requires_provenance_unless_legacy_escape_hatch_is_set(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as tmp:
            runs = Path(tmp) / "runs"
            trace = runs / "legacy-suite" / "A0" / "l1_canary_true" / "1.jsonl"
            trace.parent.mkdir(parents=True)
            trace.write_text(json.dumps(_summary(accepted=True)) + "\n", encoding="utf-8")
            common = [
                "--runs-dir",
                str(runs),
                "--manifest",
                str(MANIFEST),
                "--suite-version",
                "legacy-suite",
            ]
            self.assertEqual(benchmark_report.main(common), 1)
            self.assertEqual(
                benchmark_report.main([*common, "--allow-missing-provenance"]), 0
            )


if __name__ == "__main__":
    unittest.main()
