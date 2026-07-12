"""Final-assembly whole-recheck for a structured proof workspace.

Once a workspace's active obligation subtree is fully accepted, the
:class:`ArtifactAssembler` rebuilds a single self-contained Lean source from
the per-obligation artifacts, asks the checker to accept the whole thing one
final time, and applies the shared safety review.

It is deterministic and never raises on a blocked workspace: structural
problems (missing artifacts, un-accepted obligations, an invalid DAG) produce
an :class:`AssemblyResult` with ``accepted=False`` and a reason list, leaving
the caller free to keep searching or report a partial result.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..search.safety import SafetyReviewer, SafetyVerdict, StatementSafetyReviewer
from .base import BudgetSlice, CandidateEdit, CheckResult, ProofSystemAdapter, ProofTask
from .workspace import (
    ObligationStatus,
    ProofWorkspace,
)
from .workspace.artifact import LeanArtifact

# Re-export LeanArtifact from this module's historical location so existing
# ``from agent.proof_system.assembler import LeanArtifact`` imports keep
# working; the single source of truth lives in ``workspace.artifact``.
__all__ = [
    "LeanArtifact",
    "AssemblyResult",
    "ArtifactAssembler",
]


@dataclass(frozen=True)
class AssemblyResult:
    """Outcome of a final assembly + whole-source recheck."""

    accepted: bool
    source: str
    check_result: CheckResult | None = None
    safety_verdict: SafetyVerdict | None = None
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "source": self.source,
            "errors": list(self.errors),
            "check_result": self.check_result is not None,
            "safety_accepted": (
                self.safety_verdict.accepted
                if self.safety_verdict is not None
                else None
            ),
        }


class ArtifactAssembler:
    """Rebuild and whole-check a workspace whose obligations are accepted.

    ``assemble`` requires:

    * the workspace's obligation graph to satisfy its DAG invariant;
    * every active (non-superseded) obligation to be ``ACCEPTED``;
    * an artifact supplied for every active obligation, pinned to the same id
      and version.

    When those hold it concatenates the artifacts in dependency order
    (dependencies first), wraps them in the workspace's root Lean statement so
    the checker sees one coherent file, runs ``adapter.check``, and accepts only
    if the safety reviewer also approves the result. Any precondition failure
    short-circuits to a blocked result.
    """

    def assemble(
        self,
        workspace: ProofWorkspace,
        artifacts: Mapping[str, LeanArtifact],
        *,
        adapter: ProofSystemAdapter,
        task: ProofTask,
        check_workspace: Any = None,
        budget_slice: BudgetSlice | None = None,
        safety_reviewer: SafetyReviewer | None = None,
    ) -> AssemblyResult:
        report = workspace.validate()
        if not report.ok:
            return AssemblyResult(
                accepted=False,
                source="",
                errors=tuple(f"invalid workspace: {error}" for error in report.errors),
            )

        active = [
            obligation
            for obligation in workspace.obligation_graph.active()
        ]

        errors: list[str] = []
        for obligation in active:
            if obligation.status != ObligationStatus.ACCEPTED:
                errors.append(
                    f"obligation {obligation.obligation_id!r} "
                    f"(v{obligation.version}) is {obligation.status.value}, not accepted"
                )
                continue
            artifact = artifacts.get(obligation.obligation_id)
            if artifact is None:
                errors.append(
                    f"no artifact for accepted obligation "
                    f"{obligation.obligation_id!r}"
                )
            elif artifact.obligation_id != obligation.obligation_id:
                errors.append(
                    f"artifact mapped to {obligation.obligation_id!r} carries "
                    f"obligation id {artifact.obligation_id!r}"
                )
            elif artifact.obligation_version != obligation.version:
                errors.append(
                    f"artifact for {obligation.obligation_id!r} pins version "
                    f"{artifact.obligation_version}, current is {obligation.version}"
                )
        if errors:
            return AssemblyResult(accepted=False, source="", errors=tuple(errors))

        if len(workspace.root_obligation_ids) != 1:
            # Assumes exactly one root obligation (the one whose artifact fills
            # the task's proof hole). Multi-root assembly is a later concern;
            # fail loudly rather than silently mis-rendering.
            return AssemblyResult(
                accepted=False,
                source="",
                errors=(
                    "assembly requires exactly one root obligation, got "
                    f"{len(workspace.root_obligation_ids)}",
                ),
            )

        ordered = _topological_by_dependency(active)
        root_ids = set(workspace.root_obligation_ids)
        root_obligation = next(o for o in ordered if o.obligation_id in root_ids)
        helpers = [o for o in ordered if o.obligation_id not in root_ids]

        # The root artifact fills the task's single proof hole (PROOF_BODY);
        # helper artifacts are standalone DECLARATIONs the assembler emits as
        # their own top-level statements, spliced in before the root. With no
        # helpers (the single-root baseline) this is byte-for-byte the old path.
        root_body = artifacts[root_obligation.obligation_id].source
        edit = CandidateEdit(text=root_body, action="assemble")
        rendered = adapter.render_candidate(task, edit)
        if helpers:
            helper_text = "\n\n".join(
                artifacts[o.obligation_id].source for o in helpers
            )
            rendered = _inject_helpers(rendered, helper_text)
        check_result = self._run_check(
            adapter, task, edit, rendered, check_workspace, budget_slice
        )
        safety_verdict: SafetyVerdict | None = None
        if check_result.accepted:
            reviewer = safety_reviewer or StatementSafetyReviewer()
            safety_verdict = reviewer.accepts(task, rendered, check_result)
        accepted = bool(
            check_result.accepted
            and safety_verdict is not None
            and safety_verdict.accepted
        )
        if not check_result.accepted:
            errors = ("final whole-source recheck rejected the assembly",)
        elif safety_verdict is not None and not safety_verdict.accepted:
            errors = tuple(
                f"final assembly safety review rejected: {reason}"
                for reason in safety_verdict.reasons
            )
        else:
            errors = ()
        return AssemblyResult(
            accepted=accepted,
            source=rendered,
            check_result=check_result,
            safety_verdict=safety_verdict,
            errors=tuple(errors),
        )

    def _run_check(
        self,
        adapter: ProofSystemAdapter,
        task: ProofTask,
        edit: CandidateEdit,
        rendered: str,
        check_workspace: Any,
        budget_slice: BudgetSlice | None,
    ) -> CheckResult:
        slice_ = budget_slice or BudgetSlice()
        if check_workspace is None:
            import tempfile

            # Keep both the descriptor and file lifetime bounded. The returned
            # CheckResult may retain the path as provenance after cleanup.
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp = Path(tmp_dir) / "assembly.lean"
                tmp.write_text(rendered, encoding="utf-8")
                return adapter.check(tmp, slice_)
        with check_workspace.materialize_candidate(
            task,
            candidate_id="assembly",
            source=rendered,
            extension=".lean",
        ) as candidate:
            return adapter.check(candidate.path, slice_)


def _inject_helpers(rendered: str, helper_text: str) -> str:
    """Splice helper declarations into an already-rendered source.

    Helper artifacts are standalone ``def``/``lemma`` declarations a parent
    proof references by name, so they must appear as top-level statements
    *before* the root declaration. The injection point is the line after the
    last preamble directive (``import`` / ``open`` / ``set_option``); if there
    is no preamble the helpers lead the file. Only whole-line directives are
    treated as preamble, so a directive embedded inside the root declaration is
    never matched.
    """
    if not helper_text:
        return rendered
    lines = rendered.splitlines()
    insert_at = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if (
            stripped.startswith("import ")
            or stripped.startswith("open ")
            or stripped.startswith("set_option ")
        ):
            insert_at = index + 1
        else:
            break
    spliced = [*lines[:insert_at], helper_text, *lines[insert_at:]]
    return "\n".join(spliced)


def _topological_by_dependency(active: list) -> list:
    """Order obligations so each appears after its dependencies.

    Active obligations already form a validated DAG; this is a stable
    dependency-first emit (Kahn-style) over the active set.
    """
    by_id = {o.obligation_id: o for o in active}
    emitted: list = []
    emitted_ids: set[str] = set()

    def emit(obligation_id: str) -> None:
        obligation = by_id[obligation_id]
        for dependency_id in obligation.dependency_ids:
            if dependency_id in by_id and dependency_id not in emitted_ids:
                emit(dependency_id)
        if obligation_id not in emitted_ids:
            emitted_ids.add(obligation_id)
            emitted.append(obligation)

    for obligation in active:
        emit(obligation.obligation_id)
    return emitted
