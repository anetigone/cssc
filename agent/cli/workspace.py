"""Workspace construction helpers for the Lean task-solving CLI."""

from __future__ import annotations

from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent import EphemeralCheckWorkspace

from .paths import ROOT, resolve_agent_path


def build_check_workspace(
    args: Namespace,
    *,
    agent_root: Path,
    project_root: Path | None,
) -> EphemeralCheckWorkspace | None:
    if args.no_lake or project_root is None:
        if args.check_work_dir:
            return EphemeralCheckWorkspace(
                resolve_agent_path(agent_root, args.check_work_dir),
                keep_files=args.keep_check_files,
            )
        return None
    if args.check_work_dir:
        check_root = Path(args.check_work_dir)
        if not check_root.is_absolute():
            check_root = project_root / check_root
    else:
        check_root = project_root / ".checks"
    return EphemeralCheckWorkspace(check_root, keep_files=args.keep_check_files)


@contextmanager
def _workspace_context(
    work_dir: str | None,
    *,
    agent_root: Path = ROOT,
) -> Iterator[Path]:
    if work_dir is None:
        path = (agent_root / ".runs").resolve()
        path.mkdir(parents=True, exist_ok=True)
        yield path
        return
    path = resolve_agent_path(agent_root, work_dir)
    path.mkdir(parents=True, exist_ok=True)
    yield path
