"""Generated candidate workspace utilities."""

from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..proof_system.base import CandidateEdit, ProofTask


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MaterializedCandidate:
    candidate_id: str
    path: Path
    source: str


class AttemptWorkspace:
    """Simple deterministic workspace for generated proof attempts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        logger.debug("Initialized attempt workspace: root=%s", self.root)

    def write_candidate(
        self,
        task: ProofTask,
        edit: CandidateEdit,
        source: str,
        *,
        extension: str = ".lean",
    ) -> MaterializedCandidate:
        candidate_id = self.candidate_id(task, edit)
        task_dir = self.root / _safe_name(task.task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / f"{candidate_id}{extension}"
        path.write_text(source, encoding="utf-8")
        logger.debug(
            "Wrote candidate: task_id=%s candidate_id=%s path=%s bytes=%d",
            task.task_id,
            candidate_id,
            path,
            len(source.encode("utf-8")),
        )
        return MaterializedCandidate(candidate_id=candidate_id, path=path, source=source)

    @staticmethod
    def candidate_id(task: ProofTask, edit: CandidateEdit) -> str:
        digest = hashlib.sha256()
        digest.update(task.task_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update((edit.parent_node_id or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(edit.action.encode("utf-8"))
        digest.update(b"\0")
        digest.update(edit.text.encode("utf-8"))
        return digest.hexdigest()[:16]


class EphemeralCheckWorkspace:
    """Temporary checker-side files for tools that need project-local sources."""

    def __init__(self, root: str | Path, *, keep_files: bool = False) -> None:
        self.root = Path(root).resolve()
        self.keep_files = keep_files
        self.root.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "Initialized ephemeral check workspace: root=%s keep_files=%s",
            self.root,
            self.keep_files,
        )

    @contextmanager
    def materialize_candidate(
        self,
        task: ProofTask,
        *,
        candidate_id: str,
        source: str,
        extension: str = ".lean",
    ) -> Iterator[MaterializedCandidate]:
        task_dir = self.root / _safe_name(task.task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / f"{candidate_id}{extension}"
        path.write_text(source, encoding="utf-8")
        logger.debug(
            "Wrote checker candidate: task_id=%s candidate_id=%s path=%s bytes=%d",
            task.task_id,
            candidate_id,
            path,
            len(source.encode("utf-8")),
        )
        try:
            yield MaterializedCandidate(candidate_id=candidate_id, path=path, source=source)
        finally:
            if not self.keep_files:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to remove checker candidate: path=%s", path, exc_info=True)
                try:
                    task_dir.rmdir()
                except OSError:
                    logger.debug("Checker task directory was not removed: path=%s", task_dir, exc_info=True)


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return cleaned or "task"
