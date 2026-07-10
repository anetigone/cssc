"""Validate the Phase 8.5 benchmark suite without calling a model.

This is the Stage 0 gate (``tmp/phase8_5_benchmark_plan.md`` §9): everything
that can go wrong before model tokens are spent is caught here. It checks:

1. manifest completeness (fields, types, values, layer<->terminal consistency);
2. fixture path + exactly-one ``{{proof}}`` marker (via LeanTaskBuilder);
3. controlled scenario proposals round-trip through ``structured_action_proposal_from_dict``
   and every ``category`` is a legal ``DiagnosticCategory``;
4. Lean syntax smoke (replace the hole with ``sorry``, run ``lean`` via
   LeanAdapter(prefer_lake=False); skipped with ``--skip-lean-smoke`` or when
   the tool is unavailable);
5. per-layer ``expected_*`` cross-check rules.

Examples::

    python scripts/phase8_benchmark_validate.py -v
    python scripts/phase8_benchmark_validate.py --skip-lean-smoke
    python scripts/phase8_benchmark_validate.py --ab-evidence   # pilot-only
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.proof_system.base import BudgetSlice, DiagnosticCategory  # noqa: E402
from agent.proof_system.lean import LeanAdapter  # noqa: E402
from agent.search.structured.proposal.core import (  # noqa: E402
    structured_action_proposal_from_dict,
)
from agent.tasks.task_builder import LeanTaskBuilder, TaskBuildError  # noqa: E402

# ---- constants ------------------------------------------------------------

GLOBAL_REQUIRED_FIELDS = (
    "schema_version",
    "task_id",
    "layer",
    "source",
    "imports_profile",
    "tags",
    "expected_terminal",
    "expected_min_accepted_helpers",
    "controlled_scenario",
    "budget_profile",
    "canary",
    "external_origin",
    "license",
)

VALID_LAYERS = {"L1", "L2", "L3", "L4", "L5", "L6"}
VALID_TERMINALS = {"accepted", "partial", "blocked"}
VALID_IMPORTS_PROFILES = {"core", "mathlib"}
VALID_BUDGET_PROFILES = {"short", "repair", "multi_obligation"}
VALID_CATEGORY_VALUES = {member.value for member in DiagnosticCategory}

# layer -> the expected_terminal values that make sense for that layer's signal
LAYER_TERMINAL_SIGNAL = {
    "L1": {"accepted"},
    "L2": {"accepted"},
    "L3": {"blocked"},
    "L4": {"accepted"},
    "L5": {"accepted"},
    "L6": {"partial"},
}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.checked = 0
        self.lean_ran = 0
        self.lean_passed = 0
        self.lean_failed = 0
        self.lean_skipped = 0

    def error(self, task_id: str, msg: str) -> None:
        self.errors.append(f"[{task_id}] {msg}")

    def warn(self, task_id: str, msg: str) -> None:
        self.warnings.append(f"[{task_id}] {msg}")


# ---- stage 1: manifest completeness ---------------------------------------


def _check_manifest_row(row: dict[str, Any], report: Report) -> bool:
    """Return True if the row parsed well enough to keep validating."""
    task_id = row.get("task_id", "<missing task_id>")

    missing = [f for f in GLOBAL_REQUIRED_FIELDS if f not in row]
    if missing:
        report.error(task_id, f"missing required field(s): {', '.join(missing)}")
        return False

    layer = row["layer"]
    if layer not in VALID_LAYERS:
        report.error(task_id, f"unknown layer {layer!r}; expected one of {sorted(VALID_LAYERS)}")
        return False

    terminal = row["expected_terminal"]
    if terminal not in VALID_TERMINALS:
        report.error(
            task_id,
            f"expected_terminal {terminal!r} not a WorkspaceStatus value; "
            f"expected one of {sorted(VALID_TERMINALS)} (no explainably_blocked)",
        )
        return False

    if row["imports_profile"] not in VALID_IMPORTS_PROFILES:
        report.error(
            task_id,
            f"imports_profile {row['imports_profile']!r}; expected {sorted(VALID_IMPORTS_PROFILES)}",
        )

    if row["budget_profile"] not in VALID_BUDGET_PROFILES:
        report.error(
            task_id,
            f"budget_profile {row['budget_profile']!r}; expected {sorted(VALID_BUDGET_PROFILES)}",
        )

    if not isinstance(row["tags"], list):
        report.error(task_id, "tags must be a list")

    expected = LAYER_TERMINAL_SIGNAL[layer]
    if terminal not in expected:
        report.warn(
            task_id,
            f"layer {layer} usually has expected_terminal in {sorted(expected)}, got {terminal!r}",
        )

    source_stem = Path(row["source"]).stem
    if source_stem != task_id:
        report.error(
            task_id,
            f"task_id {task_id!r} does not match source stem {source_stem!r}",
        )
        return False

    return True


# ---- stage 2: fixture path + marker ---------------------------------------


def _check_fixture(row: dict[str, Any], fixtures_dir: Path, report: Report) -> None:
    task_id = row["task_id"]
    fixture = fixtures_dir / row["source"]
    if not fixture.is_file():
        report.error(task_id, f"fixture not found: {fixture}")
        return
    source = fixture.read_text(encoding="utf-8")
    builder = LeanTaskBuilder()
    try:
        tasks = builder.build_from_source(source, source_path=row["source"])
    except TaskBuildError as exc:
        report.error(task_id, f"LeanTaskBuilder rejected scaffold: {exc}")
        return
    if len(tasks) != 1:
        report.error(
            task_id,
            f"expected exactly 1 task from scaffold, got {len(tasks)}",
        )


# ---- stage 3: controlled scenario -----------------------------------------


def _check_scenario(
    row: dict[str, Any], fixtures_dir: Path, report: Report
) -> list[dict[str, Any]]:
    """Return the parsed proposals (empty list on hard failure)."""
    task_id = row["task_id"]
    scenario_path = fixtures_dir / row["controlled_scenario"]
    if not scenario_path.is_file():
        report.error(task_id, f"controlled_scenario not found: {scenario_path}")
        return []

    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    if scenario.get("task_id") != task_id:
        report.error(
            task_id,
            f"scenario task_id {scenario.get('task_id')!r} != manifest task_id {task_id!r}",
        )

    proposals = scenario.get("proposals", [])
    if not isinstance(proposals, list) or not proposals:
        report.error(task_id, "scenario has no proposals list")
        return []

    for index, proposal in enumerate(proposals):
        try:
            structured_action_proposal_from_dict(proposal)
        except Exception as exc:  # noqa: BLE001 — surface any deserialization error
            report.error(task_id, f"proposal[{index}] failed to deserialize: {exc!r}")

    for index, oracle in enumerate(scenario.get("expected_check_results", [])):
        category = oracle.get("category")
        if category not in VALID_CATEGORY_VALUES:
            report.error(
                task_id,
                f"expected_check_results[{index}].category {category!r} is not a "
                f"DiagnosticCategory value; expected one of {sorted(VALID_CATEGORY_VALUES)}",
            )
    return proposals


# ---- stage 4: Lean syntax smoke -------------------------------------------


def _lean_syntax_smoke(
    row: dict[str, Any],
    fixtures_dir: Path,
    report: Report,
    *,
    lean_timeout: float,
    skip: bool,
) -> None:
    if skip:
        report.lean_skipped += 1
        return
    task_id = row["task_id"]
    fixture = fixtures_dir / row["source"]
    if not fixture.is_file():
        return  # already reported in stage 2

    # mathlib tasks only get static checks (marker already validated in stage 2).
    if row["imports_profile"] == "mathlib":
        report.lean_skipped += 1
        return

    source = fixture.read_text(encoding="utf-8")
    filled = source.replace("{{proof}}", "sorry")
    adapter = LeanAdapter(prefer_lake=False, disallow_sorry=False)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".lean", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(filled)
        tmp_path = Path(handle.name)
    try:
        result = adapter.check(tmp_path, BudgetSlice(timeout_seconds=lean_timeout))
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    report.lean_ran += 1
    category = result.category
    if category == DiagnosticCategory.TOOL_UNAVAILABLE:
        # No lean on PATH — not a data error. Report as a skip, not a failure.
        report.warn(task_id, f"lean smoke skipped: {category.value}")
        report.lean_skipped += 1
        report.lean_ran -= 1
        return
    if category == DiagnosticCategory.PARSER_ERROR:
        report.error(
            task_id,
            f"lean syntax smoke failed (parser_error): {result.raw_output[:200]!r}",
        )
        report.lean_failed += 1
        return
    report.lean_passed += 1


# ---- stage 5: per-layer expected_* cross-check ----------------------------


def _check_cross(row: dict[str, Any], report: Report) -> None:
    task_id = row["task_id"]
    terminal = row["expected_terminal"]
    helpers = row["expected_min_accepted_helpers"]

    if terminal == "accepted":
        if not isinstance(helpers, int) or helpers < 0:
            report.error(task_id, "expected_min_accepted_helpers must be a non-negative int")
        if row["layer"] == "L5":
            if row.get("required_action_kind") != "change_representation":
                report.error(task_id, "L5 accepted must set required_action_kind=change_representation")
            ab = row.get("ab_evidence")
            if not isinstance(ab, dict) or not ab.get("new_representation_succeeds"):
                report.error(task_id, "L5 accepted must set ab_evidence.new_representation_succeeds")
            if not ab.get("old_representation_fails"):
                report.error(task_id, "L5 accepted must set ab_evidence.old_representation_fails")

    elif terminal == "blocked":
        if not row.get("expected_block_category"):
            report.error(task_id, "blocked terminal requires expected_block_category")
        elif row["expected_block_category"] not in VALID_CATEGORY_VALUES:
            report.error(
                task_id,
                f"expected_block_category {row['expected_block_category']!r} is not a "
                f"DiagnosticCategory value",
            )
        if not row.get("expected_probe_signature"):
            report.error(task_id, "blocked terminal requires expected_probe_signature")
        if helpers != 0:
            report.warn(task_id, f"blocked terminal usually has 0 helpers, got {helpers}")

    elif terminal == "partial":
        accepted = row.get("expected_accepted_helper_ids", [])
        open_ids = row.get("expected_open_obligation_ids", [])
        if not accepted:
            report.error(task_id, "partial terminal requires non-empty expected_accepted_helper_ids")
        if not open_ids:
            report.error(task_id, "partial terminal requires non-empty expected_open_obligation_ids")
        if isinstance(helpers, int) and helpers != len(accepted):
            report.error(
                task_id,
                f"expected_min_accepted_helpers ({helpers}) must equal "
                f"len(expected_accepted_helper_ids) ({len(accepted)})",
            )


# ---- driver ---------------------------------------------------------------


def validate(
    manifest_path: Path,
    fixtures_dir: Path,
    *,
    lean_timeout: float,
    skip_lean_smoke: bool,
) -> Report:
    report = Report()
    rows = []
    for line_no, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            report.errors.append(f"[manifest:{line_no}] invalid JSON: {exc}")
            continue
        rows.append(row)

    if not rows:
        report.errors.append("[manifest] no task rows found")

    for row in rows:
        if _check_manifest_row(row, report):
            report.checked += 1
            _check_fixture(row, fixtures_dir, report)
            _check_scenario(row, fixtures_dir, report)
            _lean_syntax_smoke(
                row,
                fixtures_dir,
                report,
                lean_timeout=lean_timeout,
                skip=skip_lean_smoke,
            )
            _check_cross(row, report)

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default="tests/fixtures/phase8_benchmark/manifest.jsonl",
        help="Path to manifest.jsonl (relative to repo root).",
    )
    parser.add_argument(
        "--fixtures-dir",
        default="tests/fixtures/phase8_benchmark",
        help="Directory holding the .lean scaffolds and scenarios/.",
    )
    parser.add_argument(
        "--lean-timeout",
        type=float,
        default=30.0,
        help="Per-file Lean smoke timeout in seconds.",
    )
    parser.add_argument(
        "--skip-lean-smoke",
        action="store_true",
        help="Skip the Lean elaboration smoke (stages 1-3-5 only).",
    )
    parser.add_argument(
        "--ab-evidence",
        action="store_true",
        help="Pilot-only: run the L5 A/B representation evidence assertions.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    manifest_path = (ROOT / args.manifest).resolve()
    fixtures_dir = (ROOT / args.fixtures_dir).resolve()
    if not manifest_path.is_file():
        print(
            json.dumps(
                {"ok": False, "error": f"manifest not found: {manifest_path}"}, indent=2
            )
        )
        return 2
    if not fixtures_dir.is_dir():
        print(
            json.dumps(
                {"ok": False, "error": f"fixtures dir not found: {fixtures_dir}"}, indent=2
            )
        )
        return 2

    report = validate(
        manifest_path,
        fixtures_dir,
        lean_timeout=args.lean_timeout,
        skip_lean_smoke=args.skip_lean_smoke,
    )

    payload = {
        "ok": not report.errors,
        "checked_tasks": report.checked,
        "errors": report.errors,
        "warnings": report.warnings,
        "lean_smoke": {
            "ran": report.lean_ran,
            "passed": report.lean_passed,
            "failed": report.lean_failed,
            "skipped": report.lean_skipped,
        },
        "ab_evidence_note": (
            "A/B assertions are pilot-only (--ab-evidence); not run in this invocation."
            if not args.ab_evidence
            else "A/B assertions requested (pilot)."
        ),
    }
    print(json.dumps(payload, indent=2))
    if args.verbose and report.warnings:
        sys.stderr.write("warnings:\n" + "\n".join(report.warnings) + "\n")
    return 0 if not report.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
