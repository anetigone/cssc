"""Suite-neutral entry points for controlled/live proof-search benchmarks.

Phase-specific scripts provide immutable configuration only.  The mature
Phase 8 implementation remains the compatibility backend, but callers do not
mutate its globals or inherit Phase 8 paths, arms, titles, or help text.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from scripts.phase8 import phase8_benchmark_report as report_backend
from scripts.phase8 import phase8_benchmark_run as run_backend
from scripts.phase8.phase8_benchmark_replay import build_replay_controller
from scripts.phase8.phase8_benchmark_validate import Report, validate

ReplayBuilder = Callable[..., Any]


@dataclass(frozen=True)
class BenchmarkSuiteConfig:
    name: str
    manifest: str
    fixtures_dir: str
    runs_root: str
    suite_version: str
    arms: dict[str, tuple[str, str]]
    default_arm: str
    report_title: str
    report_footer: str
    routed_arms: frozenset[str] = frozenset()
    single_cheap_arms: frozenset[str] = frozenset()
    arm_features: dict[str, dict[str, object]] | None = None
    controlled_arm_blocks: dict[str, str] | None = None


def run_suite(
    config: BenchmarkSuiteConfig,
    argv: list[str] | None = None,
    *,
    replay_builder: ReplayBuilder = build_replay_controller,
) -> int:
    return run_backend.main(
        argv,
        arm_table=config.arms,
        default_manifest=config.manifest,
        default_fixtures_dir=config.fixtures_dir,
        default_runs_root=config.runs_root,
        default_suite_version=config.suite_version,
        default_arm=config.default_arm,
        description=f"Run the {config.name} benchmark for one or more tasks.",
        replay_builder=replay_builder,
        routed_arms=config.routed_arms,
        single_cheap_arms=config.single_cheap_arms,
        arm_features=config.arm_features,
        controlled_arm_blocks=config.controlled_arm_blocks,
    )


def report_suite(config: BenchmarkSuiteConfig, argv: list[str] | None = None) -> int:
    return report_backend.main(
        argv,
        default_runs_dir=config.runs_root,
        default_manifest=config.manifest,
        title=config.report_title,
        footer=config.report_footer,
    )


def validate_suite_base(
    manifest: Path,
    fixtures: Path,
    *,
    lean_timeout: float,
    skip_lean_smoke: bool,
) -> Report:
    return validate(
        manifest,
        fixtures,
        lean_timeout=lean_timeout,
        skip_lean_smoke=skip_lean_smoke,
    )
