"""Render the structured workspace projection into prompt text.

This module deliberately does **not** import ``agent.search.structured``; it
duck-types the projection dict via ``Mapping``/``Sequence`` so the same code
works for the plain dict produced by structured mode and is a no-op for
minimal mode (which never sets ``structured_projection``).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def append_structured_projection(
    parts: list[str], projection: Any, *, render_artifact: bool
) -> None:
    """Render the structured workspace context projection.

    Pure over the plain-dict projection produced by
    :func:`agent.search.structured.projection.build_context_projection`. Each
    section is guarded so empty sections add nothing; absent/minimal runs pass
    ``None`` and the whole block is skipped. ``render_artifact`` defers the
    previous proof body to the ``previous_attempt`` block when one exists.
    """
    if not isinstance(projection, Mapping):
        return

    current = projection.get("current_obligation")
    if isinstance(current, Mapping):
        obligation_id = current.get("obligation_id")
        version = current.get("version")
        if isinstance(obligation_id, str) and isinstance(version, int):
            parts.append(f"Current obligation: {obligation_id} v{version}")

    dependencies = projection.get("dependency_facts")
    if isinstance(dependencies, Sequence) and dependencies:
        verified = []
        open_ids = []
        for dep in dependencies:
            if not isinstance(dep, Mapping):
                continue
            if dep.get("has_accepted_fact"):
                statement = dep.get("statement")
                if isinstance(statement, str) and statement.strip():
                    declaration = dep.get("declaration_id")
                    if isinstance(declaration, str) and declaration.strip():
                        # Surface the helper's Lean name so the model can call it
                        # directly instead of re-deriving the declaration.
                        verified.append(
                            f"{declaration.strip()}: {statement.strip()}"
                        )
                    else:
                        verified.append(statement.strip())
            else:
                dep_id = dep.get("obligation_id")
                if isinstance(dep_id, str):
                    open_ids.append(dep_id)
        if verified:
            parts.append(
                "Dependency facts (verified conclusions this proof may rely on):"
            )
            for statement in verified:
                parts.append(f"- {statement}")
        if open_ids:
            parts.append(
                "Open dependencies without accepted proofs: "
                + ", ".join(open_ids)
            )

    argument_steps = projection.get("argument_steps")
    if isinstance(argument_steps, Sequence) and argument_steps:
        parts.append("Argument steps and Lean alignment:")
        for step in argument_steps:
            if not isinstance(step, Mapping):
                continue
            claim = step.get("claim")
            if not isinstance(claim, str) or not claim.strip():
                continue
            relation = step.get("alignment_relation")
            relation_label = relation if isinstance(relation, str) else "unaligned"
            line = f"- {claim.strip()} [{relation_label}]"
            declaration = step.get("aligned_declaration")
            if isinstance(declaration, str) and declaration.strip():
                line += f" → {declaration.strip()}"
            parts.append(line)

    if render_artifact:
        proof_body = projection.get("lean_artifact_proof_body")
        if isinstance(proof_body, str) and proof_body.strip():
            parts.extend(
                ["Previous proof body to revise:", "```lean", proof_body, "```"]
            )

    observations = projection.get("observations")
    if isinstance(observations, Sequence) and observations:
        parts.append("Observations (deduplicated):")
        for obs in observations:
            if not isinstance(obs, Mapping):
                continue
            message = obs.get("message")
            if not isinstance(message, str) or not message.strip():
                continue
            source = obs.get("source")
            category = obs.get("category")
            prefix_parts = []
            if isinstance(source, str) and source:
                prefix_parts.append(source)
            if isinstance(category, str) and category:
                prefix_parts.append(category)
            prefix = f"[{':'.join(prefix_parts)}] " if prefix_parts else ""
            parts.append(f"- {prefix}{message.strip()}")

    hypotheses = projection.get("failure_hypotheses")
    if isinstance(hypotheses, Sequence) and hypotheses:
        parts.append("Competing failure hypotheses:")
        for hyp in hypotheses:
            if not isinstance(hyp, Mapping):
                continue
            kind = hyp.get("kind")
            confidence = hyp.get("confidence")
            affected = hyp.get("affected_step_ids")
            affected_label = (
                ", ".join(str(s) for s in affected)
                if isinstance(affected, Sequence) and affected
                else "—"
            )
            kind_label = kind if isinstance(kind, str) and kind else "unknown"
            conf_label = (
                f"{float(confidence):.2f}"
                if isinstance(confidence, (int, float))
                else "?"
            )
            parts.append(
                f"- [{kind_label} conf={conf_label}] affects {affected_label}"
            )

    siblings = projection.get("sibling_branches")
    if isinstance(siblings, Sequence) and siblings:
        parts.append("Other strategies on this obligation:")
        for sibling in siblings:
            if not isinstance(sibling, Mapping):
                continue
            sibling_id = sibling.get("branch_id")
            if not isinstance(sibling_id, str) or not sibling_id.strip():
                continue
            status = sibling.get("status")
            status_label = status if isinstance(status, str) and status else "?"
            count = sibling.get("observation_count")
            count_label = (
                str(int(count)) if isinstance(count, int) else "0"
            )
            artifact_label = (
                "artifact" if sibling.get("has_artifact") else "no artifact"
            )
            parts.append(
                f"- {sibling_id.strip()}: {status_label} "
                f"({count_label} observations, {artifact_label})"
            )


__all__ = ["append_structured_projection"]
